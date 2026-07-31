// Drive the site as a phone: an emulated 390x844 screen, a coarse pointer, and
// real touch events rather than a mouse.
//
//   1. start a browser:
//      msedge --headless --disable-gpu --remote-debugging-port=9222 \
//             --user-data-dir=<tmp> --enable-unsafe-swiftshader about:blank
//   2. node tests/mobile_check.mjs <url> [screenshot.png]
//
// What it pins down is the part of the UI a desktop run cannot see: that the
// diagnostics box is gone, that the control panel fits the screen and scrolls
// rather than running off the bottom, that a tap opens the small popup and a
// tap on the popup opens the full panel, and that dragging the map does not
// leave a tooltip trailing behind the finger.
//
// Exits non-zero on any failed step.

import { writeFileSync } from "node:fs";

const url = process.argv[2] ?? "http://127.0.0.1:8000/";
const shotArg = process.argv[3] ?? "-";
const shotPath = shotArg === "-" || shotArg === "/dev/null" ? null : shotArg;
const CDP = "http://127.0.0.1:9222";

const targets = await (await fetch(`${CDP}/json`)).json();
const page = targets.find((t) => t.type === "page");
if (!page) throw new Error("no page target; is the browser running?");

const socket = new WebSocket(page.webSocketDebuggerUrl);
let nextId = 1;
const waiting = new Map();
const problems = [];

function send(method, params = {}) {
  const id = nextId++;
  socket.send(JSON.stringify({ id, method, params }));
  return new Promise((resolve, reject) => waiting.set(id, { resolve, reject }));
}
socket.addEventListener("message", (e) => {
  const msg = JSON.parse(e.data);
  if (msg.id && waiting.has(msg.id)) {
    const { resolve, reject } = waiting.get(msg.id);
    waiting.delete(msg.id);
    if (msg.error) reject(new Error(`${msg.error.message} (${msg.error.code})`));
    else resolve(msg.result);
  } else if (msg.method === "Runtime.exceptionThrown") {
    problems.push(
      "exception: " +
        (msg.params.exceptionDetails.exception?.description ??
          msg.params.exceptionDetails.text)
    );
  }
});
await new Promise((r) => socket.addEventListener("open", r, { once: true }));
await send("Runtime.enable");
await send("Page.enable");

const evaluate = async (expression) => {
  const res = await send("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (!res) throw new Error("no reply from the page (did it crash or navigate?)");
  if (res.exceptionDetails) {
    throw new Error(res.exceptionDetails.exception?.description ?? "eval failed");
  }
  return res.result?.value;
};

const step = (name, ok, detail = "") => {
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${ok ? "" : "  " + detail}`);
  if (!ok) problems.push(name);
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function shoot(path) {
  const shot = await send("Page.captureScreenshot", { format: "png" });
  writeFileSync(path, Buffer.from(shot.data, "base64"));
  console.log(`     screenshot -> ${path}`);
}

// A phone, and a screen that reports no hover and a coarse pointer - which is
// what the CSS and main.js key their touch behaviour on.
const WIDTH = 390;
const HEIGHT = 844;
await send("Emulation.setDeviceMetricsOverride", {
  width: WIDTH,
  height: HEIGHT,
  deviceScaleFactor: 3,
  mobile: true,
});
await send("Emulation.setTouchEmulationEnabled", { enabled: true, maxTouchPoints: 5 });

await send("Page.navigate", { url });
await sleep(16000);

/** One tap: touchStart then touchEnd, which is what Chrome turns into the
 *  pointer events deck.gl listens to - with pointerType "touch". */
async function tap(x, y) {
  await send("Input.dispatchTouchEvent", {
    type: "touchStart",
    touchPoints: [{ x, y }],
  });
  await sleep(60);
  await send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await sleep(500);
}

async function drag(from, to) {
  await send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [from] });
  for (let i = 1; i <= 6; i++) {
    await send("Input.dispatchTouchEvent", {
      type: "touchMove",
      touchPoints: [
        {
          x: from.x + ((to.x - from.x) * i) / 6,
          y: from.y + ((to.y - from.y) * i) / 6,
        },
      ],
    });
    await sleep(30);
  }
  await send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
  await sleep(600);
}

// --- 1. the media queries took effect --------------------------------------
const env = await evaluate(`(() => ({
  coarse: matchMedia("(pointer: coarse)").matches,
  noHover: matchMedia("(hover: none)").matches,
  width: innerWidth,
  searchFont: getComputedStyle(document.getElementById("search")).fontSize,
}))()`);
step("the page sees a touch screen", env.coarse && env.noHover, JSON.stringify(env));
step(
  "the search field is 16px, so iOS will not zoom the page",
  env.searchFont === "16px",
  env.searchFont
);

// --- 2. the diagnostics box is gone, the loading pulse is not ---------------
const status = await evaluate(`(() => {
  const el = document.getElementById("status");
  const s = getComputedStyle(el);
  const text = getComputedStyle(document.getElementById("status-text"));
  return {
    opacity: s.opacity,
    textDisplay: text.display,
    width: el.getBoundingClientRect().width,
    // Still computed, still readable by the desktop checks.
    says: document.getElementById("status-text").textContent,
  };
})()`);
step(
  "the status box is invisible when idle",
  status.opacity === "0" && status.textDisplay === "none",
  JSON.stringify(status)
);
step(
  "it is a pill, not a paragraph",
  status.width < 40,
  `${status.width.toFixed(0)}px wide`
);
step(
  "the status line is still computed for the desktop tests",
  /labels of/.test(status.says),
  status.says
);

// --- 3. the panel fits the screen and scrolls ------------------------------
const panel = await evaluate(`(async () => {
  const controls = document.getElementById("controls");
  const started = controls.classList.contains("collapsed");
  document.getElementById("collapse-btn").click();
  await new Promise(r => setTimeout(r, 400));
  const box = controls.getBoundingClientRect();
  const scroll = controls.querySelector(".scroll");
  const before = scroll.scrollTop;
  scroll.scrollTop = 9999;
  const after = scroll.scrollTop;
  return {
    startedCollapsed: started,
    fits: box.bottom <= innerHeight + 1 && box.right <= innerWidth + 1,
    bottom: box.bottom,
    innerHeight,
    overflows: scroll.scrollHeight > scroll.clientHeight + 1,
    scrolled: after > before,
    attributionInside: scroll.contains(document.querySelector(".attribution")),
  };
})()`);
step("the panel starts collapsed on a phone", panel.startedCollapsed);
step(
  "expanded, it still fits the screen",
  panel.fits,
  `bottom ${panel.bottom?.toFixed(0)} of ${panel.innerHeight}`
);
step("its contents overflow", panel.overflows);
step("and it scrolls", panel.scrolled);
step("the attribution scrolls with it", panel.attributionInside);

// --- 4. searching and picking a result, by tapping -------------------------
// The result rows are chosen on mousedown, which a browser also sends after a
// tap - but only when the tap did not turn into a scroll. Worth proving, because
// the alternative (pointerdown) would fly the map every time somebody scrolled
// the list.
const results = await evaluate(`(async () => {
  const input = document.getElementById("search");
  input.value = "Kyoto";
  input.dispatchEvent(new Event("input"));
  await new Promise(r => setTimeout(r, 1500));
  const rows = [...document.querySelectorAll("#results .result")];
  const list = document.getElementById("results").getBoundingClientRect();
  const first = rows[0]?.getBoundingClientRect();
  return {
    count: rows.length,
    name: rows[0]?.querySelector(".name")?.textContent ?? "",
    onScreen: list.bottom <= innerHeight + 1 && list.right <= innerWidth + 1,
    tapAt: first ? { x: (first.left + first.right) / 2, y: (first.top + first.bottom) / 2 } : null,
    hash: location.hash,
  };
})()`);
step("search finds something", results.count > 0, JSON.stringify(results));
step("the result list stays on screen", results.onScreen, JSON.stringify(results));

if (results.tapAt) {
  await tap(results.tapAt.x, results.tapAt.y);
  await sleep(2500);
  const flew = await evaluate(`location.hash`);
  step(
    "tapping a result flies the map there",
    flew !== results.hash,
    `${results.hash} -> ${flew}`
  );
}

// Fold it away again so it is not sitting over the labels we are about to tap.
await evaluate(`document.getElementById("collapse-btn").click()`);
await sleep(1200);

// --- 4. tap once for the popup, again for the panel ------------------------
// The labels are drawn on a canvas, so their screen rectangles are the only way
// to know where a tappable thing is. main.js exposes them for exactly this.
const target = await evaluate(`(() => {
  const boxes = window.labelBoxes?.() ?? [];
  // Away from the panels and fully on screen - labels are drawn up to 160px
  // beyond the viewport so that panning does not pop them in at the edge, and
  // a tap outside the window reaches nothing at all.
  const ok = boxes.filter(b =>
    b.y0 > 120 && b.y1 < innerHeight - 160 && b.x0 > 8 && b.x1 < innerWidth - 8);
  if (!ok.length) return null;
  const b = ok[Math.floor(ok.length / 2)];
  return { x: (b.x0 + b.x1) / 2, y: (b.y0 + b.y1) / 2, name: b.item.title };
})()`);
step("there are labels on screen to tap", Boolean(target), JSON.stringify(target));

if (target) {
  await tap(target.x, target.y);
  const popup = await evaluate(`(() => {
    const el = document.getElementById("tooltip");
    const box = el.getBoundingClientRect();
    return {
      open: getComputedStyle(el).display !== "none",
      touch: el.classList.contains("touch"),
      pointer: getComputedStyle(el).pointerEvents,
      name: el.querySelector(".t-name")?.textContent ?? "",
      more: el.querySelector(".t-more")?.textContent ?? "",
      inView: box.left >= 0 && box.top >= 0 &&
              box.right <= innerWidth + 1 && box.bottom <= innerHeight + 1,
      detailOpen: document.getElementById("detail").classList.contains("open"),
      box: { left: box.left, top: box.top, right: box.right, bottom: box.bottom },
    };
  })()`);
  step(
    "one tap opens the popup",
    popup.open && popup.touch && popup.name.startsWith(target.name),
    JSON.stringify(popup)
  );
  step("the popup takes taps of its own", popup.pointer === "auto", popup.pointer);
  step("it says what a second tap does", /Tap for/.test(popup.more), popup.more);
  step("it is fully on screen", popup.inView, JSON.stringify(popup.box));
  step("one tap does not open the big panel", popup.detailOpen === false);

  // The screenshot is taken here rather than at the end, because this - a phone
  // with the tap popup open over the map - is the state worth looking at.
  if (shotPath) await shoot(shotPath);

  // Tap the popup itself.
  const centre = popup.box;
  await tap((centre.left + centre.right) / 2, (centre.top + centre.bottom) / 2);
  const opened = await evaluate(`(() => {
    const el = document.getElementById("detail");
    const box = el.getBoundingClientRect();
    return {
      open: el.classList.contains("open"),
      heading: el.querySelector("h2")?.textContent ?? "",
      popupGone: getComputedStyle(document.getElementById("tooltip")).display === "none",
      collapsed: document.getElementById("controls").classList.contains("collapsed"),
      sheet: Math.abs(box.bottom - innerHeight) < 2 && box.width > innerWidth * 0.8,
      height: box.height,
      box: { top: box.top, bottom: box.bottom, width: box.width },
    };
  })()`);
  step(
    "a tap on the popup opens the article panel",
    opened.open && opened.heading.startsWith(target.name),
    JSON.stringify(opened)
  );
  step("the popup gets out of the way", opened.popupGone);
  step("the control panel folds away for it", opened.collapsed);
  step(
    "the panel is a bottom sheet, not a corner card",
    opened.sheet,
    JSON.stringify(opened.box)
  );
  step(
    "it leaves the map visible above it",
    opened.height < HEIGHT * 0.75,
    `${opened.height?.toFixed(0)}px of ${HEIGHT}`
  );
  if (shotPath) await shoot(shotPath.replace(/(\.png)?$/, "-sheet.png"));

  await evaluate(`document.getElementById("detail-close").click()`);
  await sleep(400);

  // --- 5. a tap on empty map dismisses, a drag never opens anything --------
  await tap(target.x, target.y);
  await tap(WIDTH / 2, HEIGHT - 60);
  const dismissed = await evaluate(
    `getComputedStyle(document.getElementById("tooltip")).display === "none"`
  );
  step("a tap on the map dismisses the popup", dismissed === true);

  await drag({ x: WIDTH / 2, y: HEIGHT / 2 }, { x: WIDTH / 2 - 90, y: HEIGHT / 2 - 140 });
  const afterDrag = await evaluate(`(() => ({
    tooltip: getComputedStyle(document.getElementById("tooltip")).display,
    detail: document.getElementById("detail").classList.contains("open"),
  }))()`);
  step(
    "panning leaves no tooltip trailing behind the finger",
    afterDrag.tooltip === "none" && afterDrag.detail === false,
    JSON.stringify(afterDrag)
  );
}

socket.close();
if (problems.length) {
  console.log(`\n${problems.length} problem(s):`);
  for (const p of problems) console.log(`  - ${p}`);
  process.exit(1);
}
console.log("\nall mobile checks passed");
