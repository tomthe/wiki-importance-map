// Load the site in a headless Chromium over the DevTools protocol and report
// what actually happened: console errors, failed requests, how many tiles were
// fetched, how many labels ended up on screen, plus a screenshot.
//
//   1. start a browser:
//      msedge --headless --disable-gpu --remote-debugging-port=9222 \
//             --user-data-dir=<tmp> --enable-unsafe-swiftshader about:blank
//   2. node tests/browser_check.mjs <url> [screenshot.png] [waitMs]
//
// Exits non-zero if the page reported an error, so it can gate a release.

import { writeFileSync } from "node:fs";

const url = process.argv[2] ?? "http://127.0.0.1:8765/";
// Pass "-" to skip the screenshot. Do not pass /dev/null: on Windows that
// resolves to the reserved device name "nul" and the write fails.
const shotArg = process.argv[3] ?? "-";
const shotPath = shotArg === "-" || shotArg === "/dev/null" ? null : shotArg;
const waitMs = Number(process.argv[4] ?? 12000);
const CDP = "http://127.0.0.1:9222";

const targets = await (await fetch(`${CDP}/json`)).json();
const page = targets.find((t) => t.type === "page");
if (!page) throw new Error("no page target; is the browser running?");

const socket = new WebSocket(page.webSocketDebuggerUrl);
let nextId = 1;
const waiting = new Map();
const consoleErrors = [];
const exceptions = [];
const failedRequests = [];
const abortedRequests = [];
const urlOf = new Map();
const requests = { tiles: 0, search: 0, basemap: 0, other: 0 };

function send(method, params = {}) {
  const id = nextId++;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve) => waiting.set(id, resolve));
}

socket.addEventListener("message", (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id && waiting.has(msg.id)) {
    waiting.get(msg.id)(msg.result);
    waiting.delete(msg.id);
    return;
  }
  switch (msg.method) {
    case "Runtime.consoleAPICalled":
      if (msg.params.type === "error" || msg.params.type === "warning") {
        consoleErrors.push(
          `${msg.params.type}: ` +
            msg.params.args.map((a) => a.description ?? a.value ?? a.type).join(" ")
        );
      }
      break;
    case "Runtime.exceptionThrown":
      exceptions.push(
        msg.params.exceptionDetails.exception?.description ??
          msg.params.exceptionDetails.text
      );
      break;
    case "Network.requestWillBeSent":
      urlOf.set(msg.params.requestId, msg.params.request.url);
      break;
    case "Network.responseReceived": {
      const u = msg.params.response.url;
      if (/\/tiles\.\d+\.bin/.test(u)) requests.tiles++;
      else if (/\/search\.(\d+\.bin|json)/.test(u)) requests.search++;
      else if (u.includes("openfreemap.org")) requests.basemap++;
      else requests.other++;
      if (msg.params.response.status >= 400) {
        failedRequests.push(`${msg.params.response.status} ${u}`);
      }
      break;
    }
    case "Network.loadingFailed": {
      const u = urlOf.get(msg.params.requestId) ?? `(${msg.params.type ?? "?"})`;
      // Aborting a tile that scrolled out of view is the design, not a fault
      // (see TileManager.update), so count those separately.
      if (msg.params.errorText === "net::ERR_ABORTED") abortedRequests.push(u);
      else failedRequests.push(`${msg.params.errorText} ${u}`);
      break;
    }
  }
});

await new Promise((resolve, reject) => {
  socket.addEventListener("open", resolve, { once: true });
  socket.addEventListener("error", reject, { once: true });
});

await send("Runtime.enable");
await send("Network.enable");
// Without this, an edited ES module keeps being served from the browser's cache
// and the check silently tests the previous version.
await send("Network.setCacheDisabled", { cacheDisabled: true });
await send("Page.enable");
await send("Page.navigate", { url });
await new Promise((r) => setTimeout(r, waitMs));

const probe = await send("Runtime.evaluate", {
  returnByValue: true,
  expression: `(() => {
    const text = (id) => document.getElementById(id)?.textContent ?? null;
    const canvas = document.querySelector("#map canvas");
    return {
      title: document.title,
      loadingHidden: document.getElementById("loading")?.classList.contains("hidden"),
      loadingText: text("loading"),
      status: text("status-text"),
      dataNote: text("data-note"),
      searchNote: text("search-note"),
      categoryRows: document.querySelectorAll("#categories .cat-row").length,
      subLabels: document.querySelectorAll("#categories .subs label").length,
      canvasSize: canvas ? [canvas.width, canvas.height] : null,
      webgl: (() => {
        try { return !!document.createElement("canvas").getContext("webgl2"); }
        catch { return false; }
      })(),
    };
  })()`,
});

if (shotPath) {
  const shot = await send("Page.captureScreenshot", { format: "png" });
  if (shot?.data) writeFileSync(shotPath, Buffer.from(shot.data, "base64"));
}

const report = {
  probe: probe.result?.value,
  requests,
  consoleErrors,
  exceptions,
  failedRequests: failedRequests.slice(0, 20),
  aborted: { count: abortedRequests.length, sample: abortedRequests.slice(0, 5) },
};
console.log(JSON.stringify(report, null, 2));
socket.close();

const bad = exceptions.length || failedRequests.length || !report.probe?.loadingHidden;
process.exit(bad ? 1 : 0);
