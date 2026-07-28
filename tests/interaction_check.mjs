// Drive the real UI over the DevTools protocol: category filtering, search,
// and the detail panel. Screenshots prove it renders; this proves it works.
//
//   node tests/interaction_check.mjs [url]
//
// Needs a headless Chromium on --remote-debugging-port=9222 (see
// browser_check.mjs for the launch line). Exits non-zero on any failed step.

const url = process.argv[2] ?? "http://127.0.0.1:8765/";
const CDP = "http://127.0.0.1:9222";

const targets = await (await fetch(`${CDP}/json`)).json();
const page = targets.find((t) => t.type === "page");
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
    // A CDP error reply carries no `result`. Resolving undefined here turns the
    // real cause into "cannot read properties of undefined" three lines later.
    if (msg.error) reject(new Error(`${msg.error.message} (${msg.error.code})`));
    else resolve(msg.result);
  } else if (msg.method === "Inspector.targetCrashed") {
    console.log("   EVENT renderer crashed");
    problems.push("the renderer crashed");
  } else if (msg.method === "Page.frameNavigated" && !msg.params.frame.parentId) {
    // A mid-test navigation destroys the JS context and every later step fails
    // with something unhelpful, so say plainly that it happened.
    console.log(`   EVENT navigated to ${msg.params.frame.url}`);
  } else if (msg.method === "Runtime.executionContextsCleared") {
    console.log("   EVENT execution contexts cleared");
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
await send("Network.enable");
await send("Network.setCacheDisabled", { cacheDisabled: true });
await send("Page.enable");

const evaluateOnce = async (expression) => {
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

// A destroyed context means the page went away underneath us - a reload, a
// crash, or another debugger client driving the same tab. Retrying once in the
// new context turns that from "the rest of the suite never ran" into one noisy
// step, and the EVENT lines above say which it was.
const evaluate = async (expression) => {
  try {
    return await evaluateOnce(expression);
  } catch (err) {
    if (!/Execution context was destroyed/i.test(err.message)) throw err;
    console.log("   EVENT execution context went away; retrying once");
    problems.push("the page context was destroyed mid-run");
    await new Promise((r) => setTimeout(r, 8000));
    return await evaluateOnce(expression);
  }
};

const step = (name, ok, detail = "") => {
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${ok ? "" : "  " + detail}`);
  if (!ok) problems.push(name);
};

await send("Page.navigate", { url });
await new Promise((r) => setTimeout(r, 16000));

const sleep = (ms) => `new Promise(r => setTimeout(r, ${ms}))`;

// --- 1. baseline ------------------------------------------------------------
const base = await evaluate(`(() => {
  const s = document.getElementById("status-text").textContent;
  const m = /^([\\d,]+) labels of ([\\d,]+) places/.exec(s);
  const n = (v) => Number(v.replace(/,/g, ""));
  return { labels: n(m[1]), places: n(m[2]), status: s };
})()`);
step("map has drawn labels", base.labels > 0, base.status);
step("map has more places than labels", base.places > base.labels, base.status);

// Checked here, before anything else touches the panel: later steps switch
// categories on deliberately, so this is the only point where the default
// selection is still the default.
const defaults = await evaluate(`(() => {
  const boxes = [...document.querySelectorAll('#categories input[data-cat]')]
    .filter(b => b.dataset.sub === undefined);
  const named = (b) => b.closest(".cat-row").querySelector(".name").textContent;
  return {
    total: boxes.length,
    all: boxes.map(named),
    off: boxes.filter(b => !b.checked).map(named),
  };
})()`);
step(
  // The `total` guard matters: on a slow load the panel may not have rendered
  // yet, and without it an empty panel reads as "the defaults are wrong".
  "some categories start switched off",
  defaults.total > 0 &&
    defaults.off.length > 0 &&
    defaults.off.length < defaults.total,
  defaults.total === 0
    ? "the category panel had not rendered yet"
    : `${defaults.total} categories, off: ${JSON.stringify(defaults.off)}`
);
step(
  "People is a category of its own",
  defaults.all.includes("People"),
  JSON.stringify(defaults.all)
);
step(
  // It is the layer the map did not have, and it duplicates nothing on the
  // basemap underneath, so it is the one new category that starts visible.
  "People starts switched on",
  defaults.all.includes("People") && !defaults.off.includes("People"),
  `off: ${JSON.stringify(defaults.off)}`
);

// --- 2. unchecking a category removes its items -----------------------------
const filtered = await evaluate(`(async () => {
  const rows = [...document.querySelectorAll("#categories .cat-row")];
  const other = rows.find(r => r.querySelector(".name")?.textContent === "Other");
  const box = other.querySelector('input[type="checkbox"]');
  const before = document.getElementById("status-text").textContent;
  box.click();
  await ${sleep(1200)};
  const after = document.getElementById("status-text").textContent;
  const n = (s) => Number(/of ([\\d,]+) places/.exec(s)[1].replace(/,/g, ""));
  return { before: n(before), after: n(after), checked: box.checked };
})()`);
step(
  "unchecking Other reduces the place count",
  filtered.after < filtered.before && filtered.checked === false,
  `${filtered.before} -> ${filtered.after}`
);

const restored = await evaluate(`(async () => {
  const rows = [...document.querySelectorAll("#categories .cat-row")];
  const other = rows.find(r => r.querySelector(".name")?.textContent === "Other");
  other.querySelector('input[type="checkbox"]').click();
  await ${sleep(1200)};
  return Number(/of ([\\d,]+) places/.exec(
    document.getElementById("status-text").textContent)[1].replace(/,/g, ""));
})()`);
step("re-checking restores it", restored === filtered.before, `${restored} vs ${filtered.before}`);

// --- 3. subcategories are listed and toggleable -----------------------------
const subs = await evaluate(`(async () => {
  const rows = [...document.querySelectorAll("#categories .cat-row")];
  const nature = rows.find(r => r.querySelector(".name")?.textContent === "Nature");
  nature.querySelector(".twisty").click();
  await ${sleep(300)};
  const panel = nature.nextElementSibling;
  return { open: panel.classList.contains("open"), count: panel.querySelectorAll("label").length };
})()`);
step("subcategories expand", subs.open && subs.count > 5, JSON.stringify(subs));

// --- 3b. the selection is in the URL ----------------------------------------
// A screenshot cannot show which categories are ticked, so the link has to. Both
// directions are checked, because either one alone is useless: the hash has to
// carry a change out, and a pasted hash has to bring one in.
const shared = await evaluate(`(async () => {
  const rows = () => [...document.querySelectorAll("#categories .cat-row")];
  const row = (name) => rows().find(r => r.querySelector(".name")?.textContent === name);
  const box = (name) => row(name).querySelector('input[type="checkbox"]');
  const catBoxes = () => [...document.querySelectorAll(
    '#categories input[data-cat]:not([data-sub])')];
  const checkedNames = () => catBoxes().filter(b => b.checked)
    .map(b => b.closest(".cat-row").querySelector(".name").textContent);

  const manifest = await (await fetch("data/manifest.json")).json();
  const nature = manifest.categories.find(c => c.name === "Nature").id;

  // out: unticking a category has to show up in the hash
  const defaultHash = location.hash;
  box("Nature").click();
  await ${sleep(900)};
  const afterToggle = location.hash;

  // in: a link somebody else made, with one subcategory of one category
  location.hash = "#4.00/48.0000/9.0000/cat=" + nature + "~1";
  await ${sleep(2500)};
  const applied = {
    hash: location.hash,
    cats: checkedNames(),
    subs: [...document.querySelectorAll(
      '#categories input[data-cat="' + nature + '"][data-sub]')]
      .filter(b => b.checked).map(b => Number(b.dataset.sub)),
    places: Number(/of ([\\d,]+) places/.exec(
      document.getElementById("status-text").textContent)[1].replace(/,/g, "")),
  };

  // a link with no selection means "however the map opens"
  location.hash = "#3.40/47.0000/8.0000";
  await ${sleep(2500)};
  const reset = checkedNames();
  // and once it is back to the default, the hash should stop mentioning it
  box("Nature").click();
  await ${sleep(500)};
  box("Nature").click();
  await ${sleep(900)};
  return { defaultHash, afterToggle, applied, reset, backToDefault: location.hash };
})()`);
step(
  "the default selection is not spelled out in the hash",
  !/cat=/.test(shared.defaultHash),
  shared.defaultHash
);
step(
  "unticking a category writes it into the hash",
  /cat=/.test(shared.afterToggle),
  shared.afterToggle
);
step(
  "a shared link applies its category selection",
  shared.applied.cats.length === 1 &&
    shared.applied.cats[0] === "Nature" &&
    JSON.stringify(shared.applied.subs) === "[1]",
  JSON.stringify(shared.applied)
);
step(
  "a shared link still moves the map",
  /^#4\.00\//.test(shared.applied.hash) && shared.applied.places > 0,
  JSON.stringify(shared.applied)
);
step(
  "a link with no selection restores the defaults",
  shared.reset.length > 1 && !shared.reset.includes("Settlements"),
  JSON.stringify(shared.reset)
);
step(
  "the hash drops the selection again once it is the default",
  !/cat=/.test(shared.backToDefault),
  shared.backToDefault
);

// --- 4. search finds a place and flies to it --------------------------------
const search = await evaluate(`(async () => {
  const input = document.getElementById("search");
  if (input.disabled) return { skipped: true };
  input.focus();
  input.value = "Rostock";
  input.dispatchEvent(new Event("input", { bubbles: true }));
  await ${sleep(2500)};
  const results = [...document.querySelectorAll("#results .result")];
  const names = results.slice(0, 3).map(r => r.querySelector(".name").textContent);
  const hashBefore = location.hash;
  if (results.length) {
    results[0].dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
  }
  await ${sleep(3000)};
  return { count: results.length, names, hashBefore, hashAfter: location.hash };
})()`);
if (search.skipped) {
  step("search index available", false, "input disabled");
} else {
  step("search returns results", search.count > 0, JSON.stringify(search.names));
  step(
    "picking a result moves the map",
    search.hashAfter !== search.hashBefore,
    `${search.hashBefore} -> ${search.hashAfter}`
  );
}

// --- 5. the detail panel opens with real content ----------------------------
const detail = await evaluate(`(async () => {
  // Reach into deck.gl's layer data rather than guessing pixel coordinates.
  const items = window.__probeItems?.() ?? null;
  return items;
})()`);
// No test hook in production code, so drive the panel the way a click would.
const panel = await evaluate(`(async () => {
  const mod = await import("./src/ui.js");
  const manifest = await (await fetch("data/manifest.json")).json();
  const p = new mod.DetailPanel({
    panel: document.getElementById("detail"),
    body: document.getElementById("detail-body"),
    closeButton: document.getElementById("detail-close"),
    categories: manifest,
  });
  p.open({
    qid: 1794, title: "Frankfurt", wiki: "Frankfurt", wikiLang: "en",
    position: [8.6821, 50.1109], score: 0.8, pr: 0.8, qr: 0.8, cat: 1, sub: 2,
    hasImage: true, descr: "city in Hesse, Germany", pop: 764104, elev: 112,
    year: 794, sitelinks: 199, country: "Germany", admin: "Darmstadt Government Region",
  });
  await ${sleep(4000)};
  const body = document.getElementById("detail-body");
  const links = [...body.querySelectorAll(".links a")].map(a => a.textContent);
  const dts = [...body.querySelectorAll("dt")].map(d => d.textContent);
  const dds = [...body.querySelectorAll("dd")].map(d => d.textContent);
  return {
    open: document.getElementById("detail").classList.contains("open"),
    heading: body.querySelector("h2")?.textContent,
    meta: body.querySelector(".meta")?.textContent,
    extractLength: (body.querySelector(".extract")?.textContent ?? "").length,
    hasImage: !!body.querySelector("img:not([hidden])"),
    facts: Object.fromEntries(dts.map((k, i) => [k, dds[i]])),
    links,
  };
})()`);
step("detail panel opens", panel.open && panel.heading === "Frankfurt", JSON.stringify(panel.heading));
step("wikipedia summary loads", panel.extractLength > 80, `${panel.extractLength} chars`);
step("thumbnail loads", panel.hasImage, "no img");
step(
  "all four links present",
  panel.links.length === 4 && panel.links[0].startsWith("Wikipedia"),
  JSON.stringify(panel.links)
);

// --- 5b. an item at a borrowed coordinate says so ---------------------------
// A person is drawn at their birthplace, nudged aside so the others born there
// are still reachable. Everything the panel says about position therefore has
// to be hedged - and the map links have to be gone, because they would drop a
// pin on a spot this map invented.
const borrowed = await evaluate(`(async () => {
  const mod = await import("./src/ui.js");
  const manifest = await (await fetch("data/manifest.json")).json();
  const p = new mod.DetailPanel({
    panel: document.getElementById("detail"),
    body: document.getElementById("detail-body"),
    closeButton: document.getElementById("detail-close"),
    categories: manifest,
  });
  const item = {
    qid: 937, title: "Albert Einstein", wiki: "Albert Einstein", wikiLang: "en",
    position: [9.9937, 48.4011], score: 0.9, pr: 0.9, qr: 0.9,
    cat: manifest.peopleCat, sub: 1, hasImage: true,
    descr: "German-born theoretical physicist",
    pop: null, elev: null, year: 1879, sitelinks: 255,
    country: "Germany", admin: "Ulm", locSrc: 1, derived: true,
  };
  p.open(item);
  await ${sleep(3500)};
  const body = document.getElementById("detail-body");
  const dts = [...body.querySelectorAll("dt")].map(d => d.textContent);
  const dds = [...body.querySelectorAll("dd")].map(d => d.textContent);
  return {
    meta: body.querySelector(".meta")?.textContent ?? "",
    facts: Object.fromEntries(dts.map((k, i) => [k, dds[i]])),
    links: [...body.querySelectorAll(".links a")].map(a => a.textContent),
    place: mod.formatPlace(item, manifest),
  };
})()`);
step(
  "a derived location reads as 'born in Ulm, Germany'",
  borrowed.place === "born in Ulm, Germany",
  JSON.stringify(borrowed.place)
);
step(
  "the panel names the place it borrowed from",
  (borrowed.facts["Born in"] ?? "") === "Ulm",
  JSON.stringify(borrowed.facts)
);
step(
  "the position is flagged as approximate",
  /approximate/i.test(borrowed.facts["Position"] ?? ""),
  JSON.stringify(borrowed.facts["Position"])
);
step(
  "no exact coordinates are claimed for a borrowed point",
  !("Coordinates" in borrowed.facts),
  JSON.stringify(borrowed.facts["Coordinates"])
);
step(
  "a person's year is labelled as a birth, not a founding",
  "Born" in borrowed.facts && !("Founded" in borrowed.facts),
  JSON.stringify(Object.keys(borrowed.facts))
);
step(
  "map links are withheld for an invented coordinate",
  borrowed.links.length === 2 &&
    !borrowed.links.some(l => /OpenStreetMap|Google/.test(l)),
  JSON.stringify(borrowed.links)
);
await evaluate(`document.getElementById("detail-close").click()`);
// The new tile columns have to survive all the way to the panel, or paying
// 20% on the pyramid for descr_en bought nothing.
step(
  "the new tile fields reach the panel",
  panel.facts.Population === "764,104" &&
    panel.facts.Elevation === "112 m" &&
    panel.facts.Founded === "794" &&
    panel.facts.Country === "Germany" &&
    panel.facts["Language editions"] === "199",
  JSON.stringify(panel.facts)
);
step(
  "admin area and country appear in the subtitle",
  (panel.meta ?? "").includes("Darmstadt") && (panel.meta ?? "").includes("Germany"),
  panel.meta
);

// --- 6. the density slider caps the label count -----------------------------
// Asserted as "the cap binds", not "more labels appear": how many labels fit is
// set by decluttering, so raising the cap past what fits changes nothing.
const slider = await evaluate(`(async () => {
  document.getElementById("detail-close").click();
  // Turn every category on first. The default view hides two of them, and with
  // those gone fewer labels fit than the smallest budget allows, so the cap
  // would never bind and this would test nothing.
  for (const box of document.querySelectorAll("#categories input[data-cat]:not([data-sub])")) {
    if (box.checked) continue;
    box.checked = true;
    box.dispatchEvent(new Event("change", { bubbles: true }));
  }
  await ${sleep(1500)};
  const el = document.getElementById("labelbudget");
  const n = () => Number(
    /^([\\d,]+) labels/.exec(document.getElementById("status-text").textContent)[1]
      .replace(/,/g, ""));
  el.value = "50";
  el.dispatchEvent(new Event("input", { bubbles: true }));
  await ${sleep(1500)};
  const low = n();
  el.value = "800";
  el.dispatchEvent(new Event("input", { bubbles: true }));
  await ${sleep(1500)};
  const high = n();
  return { low, high };
})()`);
step(
  "label density slider caps the label count",
  slider.low <= 50 && slider.high > slider.low,
  `budget 50 -> ${slider.low} labels; budget 800 -> ${slider.high}`
);

// --- 7. the three answers to crowding ---------------------------------------
// All measured from the same dense view the density step just set up.
const counts = `(() => {
  const s = document.getElementById("status-text").textContent;
  const m = /^([\\d,]+) labels of ([\\d,]+) places/.exec(s);
  const n = (v) => Number(v.replace(/,/g, ""));
  const moved = /([\\d,]+) moved aside/.exec(s);
  return { labels: n(m[1]), places: n(m[2]), moved: moved ? n(moved[1]) : 0 };
})()`;

const ceiling = await evaluate(`(async () => {
  const el = document.getElementById("maxscore");
  const before = ${counts};
  el.value = "60";                       // squared: importance <= 0.36
  el.dispatchEvent(new Event("input", { bubbles: true }));
  await ${sleep(1500)};
  const after = ${counts};
  const label = document.getElementById("maxscore-out").textContent;
  el.value = "100";
  el.dispatchEvent(new Event("input", { bubbles: true }));
  await ${sleep(1500)};
  return { before, after, label, restored: ${counts} };
})()`);
// ">=" on the way back, not "==": a tile can land between the two readings.
step(
  "maximum importance hides the loudest places",
  ceiling.after.places < ceiling.before.places &&
    ceiling.restored.places >= ceiling.before.places,
  `${ceiling.before.places} -> ${ceiling.after.places} (${ceiling.label}) -> ${ceiling.restored.places}`
);

const spread = await evaluate(`(async () => {
  const box = document.getElementById("spread");
  const on = ${counts};
  box.click();                            // off
  await ${sleep(1500)};
  const off = ${counts};
  box.click();                            // back on
  await ${sleep(1500)};
  return { on, off };
})()`);
step(
  "moving crowded labels aside fits more of them in",
  spread.on.labels > spread.off.labels && spread.on.moved > 0,
  `spread on: ${spread.on.labels} labels (${spread.on.moved} moved); off: ${spread.off.labels}`
);

const overlap = await evaluate(`(async () => {
  const budget = Number(document.getElementById("labelbudget").value);
  const box = document.getElementById("show-all");
  const before = ${counts};
  box.click();
  await ${sleep(1500)};
  const after = ${counts};
  const spreadDisabled = document.getElementById("spread").disabled;
  box.click();
  await ${sleep(1500)};
  return { budget, before, after, spreadDisabled };
})()`);
// Not "== budget": the budget counts labels that fit on screen, and `places`
// includes everything in the padded bounds, so the two need not meet.
step(
  "show-all draws more labels, up to the budget",
  overlap.after.labels > overlap.before.labels &&
    overlap.after.labels <= overlap.budget,
  `${overlap.before.labels} -> ${overlap.after.labels} of budget ${overlap.budget}`
);
step("show-all disables the spread option", overlap.spreadDisabled, "still enabled");

const bypop = await evaluate(`(async () => {
  const el = document.getElementById("population");
  const before = ${counts};
  el.value = "100";
  el.dispatchEvent(new Event("input", { bubbles: true }));
  await ${sleep(1500)};
  const after = ${counts};
  const out = document.getElementById("population-out").textContent;
  el.value = "0";
  el.dispatchEvent(new Event("input", { bubbles: true }));
  await ${sleep(1500)};
  return { before, after, out };
})()`);
step(
  "sizing by population changes which labels are drawn",
  bypop.out === "100%" && bypop.after.labels !== bypop.before.labels,
  `${bypop.before.labels} -> ${bypop.after.labels} labels at ${bypop.out}`
);

// --- 8. all / none ----------------------------------------------------------
const allNone = await evaluate(`(async () => {
  const boxes = () => [...document.querySelectorAll('#categories input[data-cat]')]
    .filter(b => b.dataset.sub === undefined);
  const checked = () => boxes().filter(b => b.checked).length;
  document.getElementById("cat-all").click();
  await ${sleep(1200)};
  const afterFirst = checked();
  document.getElementById("cat-all").click();
  await ${sleep(1200)};
  const afterSecond = checked();
  return { total: boxes().length, afterFirst, afterSecond };
})()`);
step(
  "all / none clears every category, and restores them",
  (allNone.afterFirst === allNone.total && allNone.afterSecond === 0) ||
    (allNone.afterFirst === 0 && allNone.afterSecond === allNone.total),
  JSON.stringify(allNone)
);

socket.close();
if (problems.length) {
  console.log(`\n${problems.length} problem(s): ${problems.join("; ")}`);
  process.exit(1);
}
console.log("\nall interactions work");
