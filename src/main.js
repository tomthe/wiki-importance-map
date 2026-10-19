// Wiring: load the manifests, create the deck.gl map, keep tiles, filters and
// panels in sync.
//
// Rendering and tile loading are deliberately decoupled. Every view change
// redraws immediately from whatever is already cached, and only *schedules*
// tile work. That is why the map never blanks while panning - the old version
// redrew from the result of a debounced fetch, so a slow tile meant an empty
// screen.

import { TileManager } from "./tiles.js";
import { CategoryFilter } from "./categories.js";
import { buildLayers, importanceOf, labelSize } from "./layers.js";
import { LabelCanvas } from "./labelcanvas.js";
import { selectLabels } from "./declutter.js";
import { Search } from "./search.js";
import { Tooltip, DetailPanel } from "./ui.js";
import { Pack } from "./pack.js";
import { basemapStyle } from "./basemap.js";
import { siteFor } from "./site.js";

// Relative to the page, so voyage/index.html reads voyage/data/ with the same
// code. Which map this is - and so what the importance number means, where
// articles live and which categories start off - comes from <html data-site>.
const DATA_URL = "data";
const SITE_NAME = document.documentElement.dataset.site || "wikipedia";
let site;

const $ = (id) => document.getElementById(id);

// Two different questions, and conflating them is the usual mobile bug. `coarse`
// is about the input device - how big a hit target has to be, whether hovering
// exists at all - and is answered by the pointer, so a small desktop window
// keeps its mouse behaviour and a large tablet does not. `narrow` is about how
// much room there is to draw panels in.
const coarsePointer = () =>
  window.matchMedia?.("(pointer: coarse)").matches ?? false;
const narrowScreen = () => window.matchMedia?.("(max-width: 640px)").matches ?? false;

// Which device the person is actually using, from the last pointer event of any
// kind. A hybrid laptop has both a mouse and a screen, so this cannot be decided
// once at startup - and it cannot be decided from the event deck hands over
// either, because a browser follows every tap with a *compatibility mouse*
// sequence: mousemove, mousedown, mouseup, click, none of them PointerEvents and
// none of them carrying a pointer type. Read that sequence literally and every
// tap looks like a mouse arriving out of nowhere, which is exactly what made the
// hover tooltip open behind the tap popup.
let lastPointerWasTouch = coarsePointer();
if (typeof window !== "undefined") {
  const note = (event) => {
    lastPointerWasTouch = event.pointerType === "touch" || event.pointerType === "pen";
  };
  for (const type of ["pointerdown", "pointermove", "pointerup"]) {
    window.addEventListener(type, note, { capture: true, passive: true });
  }
}

/** Whether a deck pointer event came from a finger. */
function fromTouch(event) {
  const type = event?.pointerType ?? event?.srcEvent?.pointerType ?? "";
  if (type === "touch" || type === "pen") return true;
  return lastPointerWasTouch;
}

const state = {
  theme: localStorage.getItem("wikimap-theme") ?? "light",
  viewState: { longitude: 8, latitude: 47, zoom: 3.4, pitch: 0, bearing: 0 },
  style: {
    qrankWeight: 0.5,
    // The travel map's two: which month (0 for the whole year), and how much
    // being *in season* counts against being popular in absolute terms.
    month: 0,
    seasonWeight: 0.5,
    // Everything the importance of an item depends on, as one string. Caches of
    // label sizes and atlases key on it, so it has to change whenever any of
    // the above does - refreshImportance() is the only writer.
    seasonKey: "",
    importance: null,
    populationWeight: 0,
    labelScale: 1.3,
    // Not a minimum. Lowering it hides the loudest items, which is the only
    // way to read what is underneath them when several important things share
    // a coordinate. 1 means everything.
    maxImportance: 1,
    labelBudget: 300,
    spread: true,
    ignoreCollisions: false,
    colorByCategory: true,
    palette: [],
  },
  hovered: null,
};

// Which renderer draws the label text. "canvas" is the browser's own text
// rasteriser, in labelcanvas.js, and the reason it is the default is written
// down there. "sdf" is deck.gl's TextLayer, kept because it is the only way to
// get the labels into the same WebGL canvas as the dots - and because switching
// back has to stay a one-liner: set window.labelRenderer.labels = "sdf" in the
// console and pan.
const renderer = { labels: "canvas" };
if (typeof window !== "undefined") window.labelRenderer = renderer;

let deckgl;
let labelCanvas;
let tiles;
let filter;
let tooltip;
let detail;
let manifest;
let scheduled = null;

function refreshImportance() {
  const { qrankWeight, month, seasonWeight } = state.style;
  state.style.seasonKey = site?.hasSeasons
    ? `${month}|${seasonWeight}`
    : `${qrankWeight}`;
}

// ---------------------------------------------------------------- view state

// The hash is `#zoom/lat/lon`, optionally followed by `/key=value` segments -
// `cat=`, the category selection (see src/categories.js for its grammar), and
// on the travel map `m=`, the month. Splitting on `/` and sorting segments into "number" and "key=value"
// means an old three-part link still works and an unknown key is ignored rather
// than throwing the view away.
function readHash() {
  const raw = location.hash.replace(/^#/, "");
  if (!raw) return {};
  const link = {};
  const numbers = [];
  for (const segment of raw.split("/")) {
    const pair = /^([a-z]+)=(.*)$/.exec(segment);
    if (pair) link[pair[1]] = pair[2];
    else numbers.push(Number(segment));
  }
  const [zoom, latitude, longitude] = numbers;
  if (
    numbers.length >= 3 &&
    numbers.slice(0, 3).every(Number.isFinite) &&
    Math.abs(latitude) <= 90
  ) {
    link.view = { zoom, latitude, longitude, pitch: 0, bearing: 0 };
  }
  return link;
}

function hashFor() {
  const { zoom, latitude, longitude } = state.viewState;
  const cats = filter?.urlValue();
  const month = site?.hasSeasons ? state.style.month : 0;
  return (
    `#${zoom.toFixed(2)}/${latitude.toFixed(4)}/${longitude.toFixed(4)}` +
    (cats ? `/cat=${cats}` : "") +
    (month ? `/m=${month}` : "")
  );
}

let hashTimer = null;
let ownHash = "";
function writeHash() {
  clearTimeout(hashTimer);
  hashTimer = setTimeout(() => {
    ownHash = hashFor();
    history.replaceState(null, "", ownHash);
  }, 300);
}

/** Someone pasted a link into a tab that is already open. `replaceState` does
 *  not fire `hashchange`, so anything arriving here came from outside, and the
 *  whole link is applied - including a missing `cat=`, which means the default
 *  selection, because that is what the sharer was looking at. */
function onHashChange() {
  if (location.hash === ownHash) return;
  const link = readHash();
  filter.apply(link.cat ?? filter.defaultEncoded);
  if (site.hasSeasons) setMonth(parseMonth(link.m), { quiet: true });
  if (link.view) state.viewState = { ...state.viewState, ...link.view };
  render({ reselect: true });
  scheduleTiles();
}

function currentViewport() {
  return new deck.WebMercatorViewport({
    ...state.viewState,
    width: window.innerWidth,
    height: window.innerHeight,
  });
}

// ------------------------------------------------------------------ rendering

// Which items carry a label. Recomputed when the view settles rather than every
// frame: the selection depends on screen positions, so redoing it mid-drag
// would make labels flicker in and out, and the chosen set stays correct while
// panning because labels are anchored to the map.
let labelled = [];
let labelTimer = null;

/** Screen rectangles the labels should keep out of: the panels sit on top of
 *  the map, and a label underneath one is a label nobody can read. MapLibre's
 *  attribution counts - it is required to be legible, and on a phone it is a bar
 *  across the whole bottom of the screen rather than a corner. */
function panelRects() {
  const rects = [];
  const elements = ["controls", "status", "detail"].map($);
  elements.push(document.querySelector(".maplibregl-ctrl-attrib"));
  for (const el of elements) {
    if (!el || el.offsetParent === null) continue;
    const b = el.getBoundingClientRect();
    if (!b.width || !b.height) continue;
    rects.push({ x0: b.left - 4, y0: b.top - 4, x1: b.right + 4, y1: b.bottom + 4 });
  }
  return rects;
}

function chooseLabels(items) {
  labelled = selectLabels({
    items,
    viewport: currentViewport(),
    importance: (item) => importanceOf(item, state.style),
    sizeOf: (item) => labelSize(item, state.style),
    budget: state.style.labelBudget,
    avoid: panelRects(),
    spread: state.style.spread,
    ignoreCollisions: state.style.ignoreCollisions,
  });
}

function scheduleLabels(items) {
  clearTimeout(labelTimer);
  labelTimer = setTimeout(() => {
    chooseLabels(items);
    render();
  }, 130);
}

/** Everything in view that passes the category filter and the importance
 *  ceiling. One list, used for the dots, the labels and the counts, so the
 *  three can never disagree about what is on the map. */
function visibleItems() {
  const { maxImportance } = state.style;
  const items = tiles.visibleItems().filter((item) => filter.accept(item));
  if (maxImportance >= 1) return items;
  return items.filter((item) => importanceOf(item, state.style) <= maxImportance);
}

function render({ reselect = false } = {}) {
  // The panel is live while the manifest is still downloading, so a click can
  // land here before there is anything to draw into.
  if (!deckgl) return;
  const items = visibleItems();
  if (reselect) chooseLabels(items);
  const { layers, visibleCount, displacedCount } = buildLayers({
    items,
    labelled,
    style: state.style,
    theme: state.theme,
    textLayers: renderer.labels !== "canvas",
  });
  deckgl.setProps({ layers, viewState: state.viewState });
  // The label canvas itself is redrawn from onAfterRender rather than here, so
  // that it tracks the camera deck actually drew with - including the frames of
  // a flyTo transition, which deck animates internally without coming back
  // through render().
  if (renderer.labels !== "canvas") labelCanvas?.clear();
  filter.updateCounts(items);
  updateStatus(visibleCount, labelled.length, displacedCount);
  return items;
}

/** Redraw the label overlay onto the camera deck.gl just drew with. */
function drawLabels() {
  if (!labelCanvas || renderer.labels !== "canvas") return;
  const viewport = deckgl?.getViewports?.()?.[0];
  if (!viewport) return;
  labelCanvas.prepare({ labelled, style: state.style, theme: state.theme });
  labelCanvas.draw(viewport);
}

function scheduleTiles() {
  if (scheduled) return;
  scheduled = requestAnimationFrame(() => {
    scheduled = null;
    tiles.update(currentViewport().getBounds(), state.viewState.zoom);
  });
}

function updateStatus(inView, labelCount, displaced) {
  const s = tiles.stats();
  const busy = s.pending + s.queued > 0;
  $("status").classList.toggle("busy", busy);
  $("status-text").textContent =
    `${labelCount.toLocaleString()} labels of ${inView.toLocaleString()} ${site.thingsName} · ` +
    (displaced ? `${displaced} moved aside · ` : "") +
    `${s.needed} tiles in view · ${s.cached.toLocaleString()} cached · ` +
    `${s.megabytes.toFixed(1)} MB in ${s.requests.toLocaleString()} requests` +
    (busy ? ` · loading ${s.pending + s.queued}` : "");
}

// ------------------------------------------------------------------ callbacks

/** The label layer's rows are placements wrapping an item; the dot layer's are
 *  items. Both should hover the same thing. */
const itemOf = (object) => (object && object.item ? object.item : object) ?? null;

/**
 * What is under the pointer. deck picks the dots (and, on the sdf path, the
 * labels); the canvas renderer keeps its own screen rectangles, and it is
 * asked first because its labels are drawn on top of everything deck draws.
 * Both are driven by deck's hover events, which fire on every pointer move
 * whether or not anything was picked.
 */
function pickAt(info, slop = 0) {
  return labelCanvas?.pick(info.x, info.y, slop) ?? itemOf(info.object);
}

// Read by getCursor. deck evaluates that when it handles a pointer event, so
// this is at worst one event stale - a frame, and invisible.
let hoveringItem = false;

function onHover(info, event) {
  // A finger dragging the map reports hover on everything it passes over. There
  // is no pointer to follow, so following it would flash a tooltip through the
  // middle of every pan; touch gets the tap popup in onClick instead.
  if (fromTouch(event)) return;
  const item = pickAt(info);
  hoveringItem = Boolean(item);
  // Every pointer move arrives here, not just entering and leaving an item, so
  // the tooltip is only rebuilt when it would actually say something different.
  if (item !== state.hovered) {
    state.hovered = item;
    tooltip.show(item, info.x, info.y);
  } else if (item) {
    tooltip.move(info.x, info.y);
  }
}

/** Open the full panel. On a phone that panel is a sheet across the bottom of
 *  the screen, so the control panel is folded away at the same time: two cards
 *  over a 390px map leaves no map. */
function openDetail(item) {
  tooltip.hide();
  detail.open(item);
  if (narrowScreen()) setCollapsed(true);
  // The sheet covers part of the map, and labels underneath it cannot be read.
  render({ reselect: true });
}

function onClick(info, event) {
  // A fingertip is far wider than a 3px dot, so touch gets a few pixels of slop
  // on the label boxes and a matching picking radius on the dots (set on the
  // deck instance below).
  const item = pickAt(info, fromTouch(event) ? 8 : 0);
  if (!item) {
    // A tap on empty map is how the popup is dismissed.
    tooltip.hide();
    return;
  }
  if (!fromTouch(event)) return openDetail(item);
  // First tap: the cheap answer, out of the tile, with no network round trip.
  // Second tap on the same thing (or on the popup, handled in ui.js): the
  // article. Tapping a *different* item just moves the popup there.
  if (tooltip.isShowing(item)) openDetail(item);
  else tooltip.show(item, info.x, info.y, { touch: true });
}

function flyTo(longitude, latitude, zoom) {
  state.viewState = {
    ...state.viewState,
    longitude,
    latitude,
    zoom,
    transitionDuration: 1200,
    transitionInterpolator: new deck.FlyToInterpolator({ speed: 1.6 }),
  };
  render();
  setTimeout(() => render({ reselect: true }), 1400);
  scheduleTiles();
}

// ---------------------------------------------------------------------- theme

async function applyTheme(theme) {
  state.theme = theme;
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("wikimap-theme", theme);
  state.style.palette = manifest.palette[theme];
  filter?.setPalette(state.style.palette);
  render();
  // The scripting bundle has changed how it exposes the basemap between
  // versions; if none of these work the labels still retheme, only the tiles
  // underneath stay put.
  const map =
    deckgl?.getMapboxMap?.() ?? deckgl?.getMapLibreMap?.() ?? deckgl?._map ?? null;
  if (map?.setStyle) map.setStyle(await basemapStyle(theme));
}

// ----------------------------------------------------------------- controls

function setCollapsed(collapsed) {
  $("controls").classList.toggle("collapsed", collapsed);
  $("collapse-btn").setAttribute("aria-expanded", String(!collapsed));
  // The panel's footprint changed, so labels may now be free to use the space it
  // used to cover (or need to avoid it again).
  render({ reselect: true });
}

function wireControls() {
  $("theme-btn").addEventListener("click", () =>
    applyTheme(state.theme === "dark" ? "light" : "dark")
  );

  const controls = $("controls");
  $("collapse-btn").addEventListener("click", () =>
    setCollapsed(!controls.classList.contains("collapsed"))
  );
  // Small screens: start collapsed so the panel is a compact bar rather than
  // a card that covers most of the map.
  setCollapsed(narrowScreen());

  const slider = (id, apply) => {
    const el = $(id);
    // Each page carries only the sliders that mean something on its map.
    if (!el) return;
    el.addEventListener("input", () => {
      apply(Number(el.value));
      render({ reselect: true });
    });
  };

  slider("weight", (raw) => {
    const v = raw / 100;
    state.style.qrankWeight = v;
    refreshImportance();
    $("weight-out").textContent =
      v < 0.35 ? "link structure" : v > 0.65 ? "pageviews" : "balanced";
  });

  slider("season", (raw) => {
    const v = raw / 100;
    state.style.seasonWeight = v;
    refreshImportance();
    $("season-out").textContent =
      v < 0.2 ? "most viewed" : v > 0.8 ? "in season" : "balanced";
  });

  if (site.hasSeasons) wireMonths();

  slider("population", (raw) => {
    const v = raw / 100;
    state.style.populationWeight = v;
    $("population-out").textContent = v === 0 ? "off" : `${(v * 100).toFixed(0)}%`;
  });

  slider("maxscore", (raw) => {
    // Squared, so the top of the slider - where the handful of items that
    // dominate a view actually live - gets most of the travel.
    const v = Math.pow(raw / 100, 2);
    state.style.maxImportance = raw >= 100 ? 1 : v;
    $("maxscore-out").textContent =
      raw >= 100 ? "all" : `≤ ${(v * 100).toFixed(1)}`;
  });

  slider("labelbudget", (raw) => {
    state.style.labelBudget = raw;
    $("labelbudget-out").textContent = String(raw);
  });

  slider("labelscale", (raw) => {
    const v = raw / 100;
    state.style.labelScale = v;
    $("labelscale-out").textContent = `${v.toFixed(1)}×`;
  });

  $("spread").addEventListener("change", (event) => {
    state.style.spread = event.target.checked;
    render({ reselect: true });
  });

  $("show-all").addEventListener("change", (event) => {
    state.style.ignoreCollisions = event.target.checked;
    // Spreading is meaningless when nothing is being avoided.
    $("spread").disabled = event.target.checked;
    render({ reselect: true });
  });

  $("color-by-cat").addEventListener("change", (event) => {
    state.style.colorByCategory = event.target.checked;
    render();
  });

  // False unless everything is already on, so the first click turns everything
  // on rather than being a no-op on a partial selection - which is what the
  // default two-categories-off state is, and what a shared link usually is too.
  let allOn = filter.encode() === "all";
  $("cat-all").addEventListener("click", () => {
    allOn = !allOn;
    filter.setAll(allOn);
  });

  // Label size and label density are both counted in pixels, and a phone has a
  // fifth of a laptop's - so the same two numbers put five times as many labels
  // on screen and make the largest of them nearly a screen wide. Moving the
  // slider is the whole of it: both keep their full range, so this changes where
  // a phone starts and takes nothing away.
  if (narrowScreen()) {
    setSlider("labelscale", 90);
    setSlider("labelbudget", 150);
  }
}

// ------------------------------------------------------------------- months

/** "3" -> 3; anything that is not a month -> 0, the whole year. */
function parseMonth(raw) {
  const m = Number(raw);
  return Number.isInteger(m) && m >= 1 && m <= 12 ? m : 0;
}

/** The month row on the travel map: the year, then January to December. */
function wireMonths() {
  const row = $("months");
  const labels = ["Year", ...site.months];
  labels.forEach((label, month) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "month";
    button.textContent = month ? label.charAt(0) : label;
    button.title = month ? label : "The whole year";
    button.dataset.month = String(month);
    button.addEventListener("click", () => setMonth(month));
    row.append(button);
  });
  // The arrow keys step through the months once the row has focus, which is
  // the quickest way to watch a season move across the map.
  row.addEventListener("keydown", (event) => {
    const step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
    if (!step) return;
    event.preventDefault();
    setMonth((state.style.month + step + 13) % 13);
    row.querySelector(`[data-month="${state.style.month}"]`)?.focus();
  });
}

function setMonth(month, { quiet = false } = {}) {
  state.style.month = month;
  refreshImportance();
  for (const button of document.querySelectorAll("#months .month")) {
    const on = Number(button.dataset.month) === month;
    button.classList.toggle("on", on);
    button.setAttribute("aria-pressed", String(on));
  }
  const out = $("month-out");
  if (out) out.textContent = month ? site.monthsLong[month - 1] : "the whole year";
  // "In season" means nothing without a season to be in.
  const season = $("season");
  if (season) season.disabled = !month;
  if (quiet) return;
  render({ reselect: true });
  writeHash();
}

/** Move a slider as if it had been dragged, so the value, the state and the
 *  readout beside it cannot disagree. */
function setSlider(id, value) {
  const el = $(id);
  if (!el) return;
  el.value = String(value);
  el.dispatchEvent(new Event("input"));
}

// ------------------------------------------------------------------- search

/** Not awaited by startup: the map should be usable before search is, and the
 *  root file is small enough that it usually lands first anyway. */
function startSearch() {
  $("search").placeholder = "loading search index…";
  fetch(`${DATA_URL}/search.json`)
    .then((r) => (r.ok ? r.json() : null))
    .then((root) => {
      if (!root) throw new Error("no search index");
      new Search({
        baseUrl: DATA_URL,
        root,
        // state.style is passed live so results restyle when the theme changes.
        categories: { categories: manifest.categories, style: state.style },
        countries: manifest.countries ?? [],
        input: $("search"),
        results: $("results"),
        onPick: ({ lon, lat }) => flyTo(lon, lat, Math.max(state.viewState.zoom, 11)),
      });
      $("search").placeholder = "Search places…";
      $("search-note").textContent = site.searchNote(root);
    })
    .catch(() => {
      $("search").placeholder = "search index not built";
      $("search").disabled = true;
    });
}

// -------------------------------------------------------------------- startup

async function start() {
  manifest = await fetch(`${DATA_URL}/manifest.json`).then((r) => {
    if (!r.ok) throw new Error("data/manifest.json not found - run the pipeline first");
    return r.json();
  });
  if (!manifest.pack) {
    throw new Error("this data was built by an older pipeline - rerun build_tiles");
  }

  const pack = new Pack({ baseUrl: DATA_URL, info: manifest.pack });
  // Fail here, loudly, rather than three tiles in with a 90 MB download under
  // way. Everything after this point assumes byte ranges work.
  await pack.probe();

  state.style.palette = manifest.palette[state.theme];
  document.documentElement.dataset.theme = state.theme;
  site = siteFor(SITE_NAME, manifest);
  state.style.importance = site.importance;
  refreshImportance();

  const link = readHash();

  filter = new CategoryFilter({
    manifest,
    container: $("categories"),
    defaultOff: site.defaultOff,
    // The selection is in the URL, so every change to it rewrites the hash -
    // that is the whole of "share what I can see".
    onChange: () => {
      render({ reselect: true });
      writeHash();
    },
  });
  filter.setPalette(state.style.palette);
  // A token this manifest cannot make sense of leaves the defaults alone rather
  // than opening a blank map.
  if (link.cat !== undefined && !filter.apply(link.cat)) {
    console.warn(`ignoring an unusable category selection: cat=${link.cat}`);
  }

  tiles = new TileManager({
    pack,
    manifest,
    onUpdate: () => render({ reselect: true }),
  });

  if (link.view) state.viewState = { ...state.viewState, ...link.view };

  const coarse = coarsePointer();
  deckgl = new deck.DeckGL({
    container: "map",
    mapStyle: await basemapStyle(state.theme),
    viewState: state.viewState,
    controller: {
      // Double-click zoom is off with a mouse because a click already means
      // "open this" and the second one should not also move the map. On touch
      // there is nothing else double-tap could mean, and every other map on a
      // phone zooms with it.
      doubleClickZoom: coarse,
      // Two fingers on a phone means zoom, and rotating by accident leaves the
      // map at a bearing the shareable URL does not carry.
      dragRotate: !coarse,
      touchRotate: false,
      inertia: 250,
    },
    // A dot is between 1.4 and 9 pixels across and a fingertip is about 40, so
    // an exact hit test is unusable with a thumb. Three pixels helps a mouse too.
    pickingRadius: coarse ? 10 : 3,
    onViewStateChange,
    layers: [],
    // At the deck level rather than per layer, because the canvas renderer's
    // labels are not deck objects and only the root callback fires on every
    // pointer move rather than on entering and leaving a layer's objects.
    onHover,
    onClick,
    onAfterRender: drawLabels,
    getCursor: ({ isHovering }) =>
      isHovering || hoveringItem ? "pointer" : "grab",
  });

  // Inside #map, above deck's canvas. #map does not create a stacking context,
  // so the overlay's z-index still sits below the panels at 10.
  labelCanvas = new LabelCanvas($("map"));
  // Where the labels ended up on screen, for the console and for
  // tests/mobile_check.mjs - which has to tap one and cannot know where it is.
  if (typeof window !== "undefined") window.labelBoxes = () => labelCanvas.boxes;

  tooltip = new Tooltip($("tooltip"), {
    categories: manifest,
    site,
    style: state.style,
    // The tap that turns the popup into the article. Mouse users never reach
    // this: the tooltip does not take the pointer unless it is in touch mode.
    onActivate: (item) => openDetail(item),
  });
  detail = new DetailPanel({
    panel: $("detail"),
    body: $("detail-body"),
    closeButton: $("detail-close"),
    categories: manifest,
    site,
    style: state.style,
  });

  wireControls();
  if (site.hasSeasons) setMonth(parseMonth(link.m), { quiet: true });
  startSearch();

  $("data-note").textContent = site.dataNote(manifest);

  window.addEventListener("resize", onResize);
  window.addEventListener("hashchange", onHashChange);
  $("loading").classList.add("hidden");

  scheduleTiles();
  render({ reselect: true });
}

function onViewStateChange({ viewState }) {
  state.viewState = viewState;
  // The tap popup is pinned to a screen point, not to a coordinate, so as soon
  // as the map moves under it it is pointing at the wrong thing. Hovered
  // tooltips follow the pointer and need none of this.
  if (tooltip?.touch) tooltip.hide();
  const items = render();      // immediate, from cache
  scheduleLabels(items ?? []); // re-declutter once the view settles
  scheduleTiles();             // fetch what is missing, without blocking the draw
  writeHash();
}

// Rotating a phone changes both the viewport and where the panels are, so the
// tiles in view and the labels that fit both have to be worked out again.
// Debounced, because a resize arrives as a stream of events.
let resizeTimer = null;
function onResize() {
  scheduleTiles();
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => render({ reselect: true }), 200);
}

start().catch((err) => {
  console.error(err);
  $("loading").textContent = String(err.message ?? err);
});
