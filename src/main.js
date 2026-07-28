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

const DATA_URL = "data";
// Settlements and Administrative are the two categories the importance score
// already favours - they dominate every zoom and mostly restate what the
// basemap says. Starting with them off makes the first view show the things
// people came for, and both are one click away.
//
// People deliberately starts ON. It is the layer this map did not have, and
// unlike Settlements it duplicates nothing underneath it.
const DEFAULT_OFF = ["Settlements", "Administrative", "People"];

const $ = (id) => document.getElementById(id);

const state = {
  theme: localStorage.getItem("wikimap-theme") ?? "light",
  viewState: { longitude: 8, latitude: 47, zoom: 3.4, pitch: 0, bearing: 0 },
  style: {
    qrankWeight: 0.5,
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

// ---------------------------------------------------------------- view state

// The hash is `#zoom/lat/lon`, optionally followed by `/key=value` segments -
// today only `cat=`, the category selection (see src/categories.js for its
// grammar). Splitting on `/` and sorting segments into "number" and "key=value"
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
  return (
    `#${zoom.toFixed(2)}/${latitude.toFixed(4)}/${longitude.toFixed(4)}` +
    (cats ? `/cat=${cats}` : "")
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
 *  the map, and a label underneath one is a label nobody can read. */
function panelRects() {
  const rects = [];
  for (const id of ["controls", "status", "detail"]) {
    const el = $(id);
    if (!el || el.offsetParent === null) continue;
    const b = el.getBoundingClientRect();
    rects.push({ x0: b.left - 4, y0: b.top - 4, x1: b.right + 4, y1: b.bottom + 4 });
  }
  return rects;
}

function chooseLabels(items) {
  labelled = selectLabels({
    items,
    viewport: currentViewport(),
    importance: (item) => importanceOf(item, state.style.qrankWeight),
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
  const { qrankWeight, maxImportance } = state.style;
  const items = tiles.visibleItems().filter((item) => filter.accept(item));
  if (maxImportance >= 1) return items;
  return items.filter((item) => importanceOf(item, qrankWeight) <= maxImportance);
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
    `${labelCount.toLocaleString()} labels of ${inView.toLocaleString()} places · ` +
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
function pickAt(info) {
  return labelCanvas?.pick(info.x, info.y) ?? itemOf(info.object);
}

// Read by getCursor. deck evaluates that when it handles a pointer event, so
// this is at worst one event stale - a frame, and invisible.
let hoveringItem = false;

function onHover(info) {
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

function onClick(info) {
  const item = pickAt(info);
  if (item) detail.open(item);
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

function wireControls() {
  $("theme-btn").addEventListener("click", () =>
    applyTheme(state.theme === "dark" ? "light" : "dark")
  );

  const controls = $("controls");
  const collapseBtn = $("collapse-btn");
  const setCollapsed = (collapsed) => {
    controls.classList.toggle("collapsed", collapsed);
    collapseBtn.setAttribute("aria-expanded", String(!collapsed));
    // The panel's footprint changed, so labels may now be free to use the
    // space it used to cover (or need to avoid it again).
    render({ reselect: true });
  };
  collapseBtn.addEventListener("click", () =>
    setCollapsed(!controls.classList.contains("collapsed"))
  );
  // Small screens: start collapsed so the panel is a compact bar rather than
  // a card that covers most of the map.
  setCollapsed(window.matchMedia("(max-width: 640px)").matches);

  const slider = (id, apply) => {
    const el = $(id);
    el.addEventListener("input", () => {
      apply(Number(el.value));
      render({ reselect: true });
    });
  };

  slider("weight", (raw) => {
    const v = raw / 100;
    state.style.qrankWeight = v;
    $("weight-out").textContent =
      v < 0.35 ? "link structure" : v > 0.65 ? "pageviews" : "balanced";
  });

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
      $("search-note").textContent =
        `${root.items.toLocaleString()} places above importance ` +
        `${(root.minScore * 100).toFixed(0)} are findable`;
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

  const link = readHash();

  filter = new CategoryFilter({
    manifest,
    container: $("categories"),
    defaultOff: DEFAULT_OFF,
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

  deckgl = new deck.DeckGL({
    container: "map",
    mapStyle: await basemapStyle(state.theme),
    viewState: state.viewState,
    controller: { doubleClickZoom: false, inertia: 250 },
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

  tooltip = new Tooltip($("tooltip"), { categories: manifest });
  detail = new DetailPanel({
    panel: $("detail"),
    body: $("detail-body"),
    closeButton: $("detail-close"),
    categories: manifest,
  });

  wireControls();
  startSearch();

  $("data-note").textContent =
    `${manifest.itemCount.toLocaleString()} geolocated articles in ` +
    `${manifest.tileCount.toLocaleString()} tiles · zoom 0–${manifest.maxZoom} · ` +
    `${(manifest.pack.bytes / 1e6).toFixed(0)} MB in ${manifest.pack.parts.length} file(s)`;

  window.addEventListener("resize", () => scheduleTiles());
  window.addEventListener("hashchange", onHashChange);
  $("loading").classList.add("hidden");

  scheduleTiles();
  render({ reselect: true });
}

function onViewStateChange({ viewState }) {
  state.viewState = viewState;
  const items = render();      // immediate, from cache
  scheduleLabels(items ?? []); // re-declutter once the view settles
  scheduleTiles();             // fetch what is missing, without blocking the draw
  writeHash();
}

start().catch((err) => {
  console.error(err);
  $("loading").textContent = String(err.message ?? err);
});
