// Hover tooltip and the click-through detail panel.
//
// The tiles now carry a description, country, admin area, population,
// elevation, founding year and language-edition count alongside the name, so
// the hover can answer "what is this" without a network round trip. Adding
// descr_en cost about 20% on the tile pyramid and it is the single line that
// most often makes a label make sense.
//
// The one thing still fetched on demand is the article summary and its
// thumbnail, for the one item that was clicked - a paragraph of prose and a
// picture per item would be several gigabytes in the tiles, and nobody reads
// more than a handful per session.
//
// Which wiki the article is on, and which facts are worth a line, is the
// site's business (src/site.js): the same panels serve Wikipedia and
// Wikivoyage.

import { monthShares } from "./site.js";

const articleUrl = (site, lang, title) =>
  `${site.articleHost(lang)}/wiki/${encodeURIComponent(title.replace(/ /g, "_"))}`;
const summaryUrl = (site, lang, title) =>
  `${site.articleHost(lang)}/api/rest_v1/page/summary/${encodeURIComponent(
    title.replace(/ /g, "_")
  )}`;

const SVG = "http://www.w3.org/2000/svg";

/**
 * Twelve bars, one per month, scaled to the item's busiest month - the shape of
 * its year at a glance. The selected month is drawn in the accent colour, so
 * moving the month row shows where in that shape the map is looking.
 */
export function seasonChart(item, month, { width = 120, height = 22, labels = false } = {}) {
  const shares = monthShares(item);
  if (!shares) return null;
  const peak = Math.max(...shares);
  const labelRoom = labels ? 11 : 0;
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "season-chart");
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height + labelRoom));
  svg.setAttribute("viewBox", `0 0 ${width} ${height + labelRoom}`);
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "pageviews by month");
  const slot = width / 12;
  const letters = "JFMAMJJASOND";
  for (let m = 0; m < 12; m++) {
    const h = Math.max(1, (shares[m] / peak) * height);
    const bar = document.createElementNS(SVG, "rect");
    bar.setAttribute("x", (m * slot + 1).toFixed(1));
    bar.setAttribute("y", (height - h).toFixed(1));
    bar.setAttribute("width", Math.max(1, slot - 2).toFixed(1));
    bar.setAttribute("height", h.toFixed(1));
    bar.setAttribute("rx", "1");
    bar.setAttribute("class", m + 1 === month ? "bar on" : "bar");
    svg.append(bar);
    if (labels) {
      const t = document.createElementNS(SVG, "text");
      t.setAttribute("x", (m * slot + slot / 2).toFixed(1));
      t.setAttribute("y", String(height + 9));
      t.setAttribute("text-anchor", "middle");
      t.setAttribute("class", m + 1 === month ? "lbl on" : "lbl");
      t.textContent = letters[m];
      svg.append(t);
    }
  }
  return svg;
}

const nf = new Intl.NumberFormat();

/** Wikidata has populations claimed to the person for cities of ten million,
 *  which is false precision; round the big ones and print the small ones. */
export function formatPopulation(value) {
  if (value == null) return null;
  if (value >= 1e6) return `${(value / 1e6).toLocaleString(undefined, {
    maximumFractionDigits: 2,
  })} million`;
  return nf.format(value);
}

export function formatYear(year) {
  if (year == null) return null;
  return year < 0 ? `${nf.format(-year)} BC` : String(year);
}

/**
 * Country and admin area, without repeating a city-state's own name.
 *
 * For an item drawn at a coordinate it does not own - a person at their
 * birthplace, a painting at its museum - `admin` holds that place's name and
 * the phrase from the manifest says how it got there: "born in Ulm, Germany".
 * Without the preposition the line would read exactly like a place's own
 * address and quietly assert that Einstein is a point in southern Germany.
 */
export function formatPlace(item, manifest, site = null) {
  const parts = [];
  if (item.admin && item.admin !== item.title) parts.push(item.admin);
  if (item.country && item.country !== item.title && item.country !== item.admin) {
    parts.push(item.country);
  }
  const where = parts.join(", ");
  if (!where) return "";
  const phrase = item.derived
    ? manifest?.locSources?.[item.locSrc]
    : site?.placePhrase?.(item) ?? "";
  return phrase ? `${phrase} ${where}` : where;
}

function kindOf(categories, item) {
  const cat = categories.categories[item.cat];
  const sub = cat?.subcategories?.[item.sub];
  return sub && sub !== "Other" ? `${sub} · ${cat.name}` : cat?.name ?? "";
}

/** A year means different things to a person and to a building. */
function yearLabel(manifest, item) {
  return item.cat === manifest?.peopleCat ? "Born" : "Founded";
}

/**
 * The hover box, which on a touch screen is not a hover box.
 *
 * With no pointer there is no hover, so the two-step - look, then commit -
 * has to be built out of taps: the first tap opens this, a tap on this opens
 * the detail panel, a tap anywhere else dismisses it. That keeps the cheap
 * answer cheap; going straight to the panel on every tap would fetch a
 * Wikipedia summary for every dot anybody brushed with a thumb.
 *
 * `onActivate` is what the second tap calls. It is a click on this element
 * rather than a second pick from the map, because in touch mode the popup
 * takes the pointer and is usually sitting under the finger anyway.
 */
export class Tooltip {
  constructor(element, { categories, site, style, onActivate = null }) {
    this.el = element;
    this.categories = categories;
    this.site = site;
    this.style = style;
    this.onActivate = onActivate;
    this.item = null;
    this.touch = false;
    element.addEventListener("click", (event) => {
      if (!this.item) return;
      const item = this.item;
      const dismiss = event.target.closest?.(".t-dismiss");
      this.hide();
      if (!dismiss) this.onActivate?.(item);
    });
  }

  /** True when the popup is already open on this item - the state in which a
   *  second tap on the map means "open it properly" rather than "reopen". */
  isShowing(item) {
    return Boolean(item) && this.item === item;
  }

  show(item, x, y, { touch = false } = {}) {
    if (!item) return this.hide();
    this.el.textContent = "";
    this.item = item;
    this.touch = touch;
    this.el.classList.toggle("touch", touch);

    const name = document.createElement("div");
    name.className = "t-name";
    name.textContent = item.title + (item.hasImage ? " 📷" : "");

    const kind = document.createElement("div");
    kind.className = "t-meta";
    kind.textContent = kindOf(this.categories, item);
    this.el.append(name, kind);

    if (item.descr) {
      const descr = document.createElement("div");
      descr.className = "t-descr";
      descr.textContent = item.descr;
      this.el.append(descr);
    }

    // Facts worth reading at a glance, and only the ones this item has.
    const facts = [
      formatPlace(item, this.categories, this.site),
      item.pop != null ? `pop. ${formatPopulation(item.pop)}` : null,
      item.elev != null ? `${nf.format(item.elev)} m` : null,
      formatYear(item.year),
    ].filter(Boolean);
    if (facts.length) {
      const line = document.createElement("div");
      line.className = "t-meta";
      line.textContent = facts.join(" · ");
      this.el.append(line);
    }

    const rank = document.createElement("div");
    rank.className = "t-rank";
    rank.textContent = this.site.rankLine(item);
    if (this.site.hasSeasons) {
      const chart = seasonChart(item, this.style.month, { width: 96, height: 16 });
      if (chart) rank.prepend(chart);
    }
    this.el.append(rank);

    if (touch) {
      const more = document.createElement("div");
      more.className = "t-more";
      const open = document.createElement("span");
      open.textContent = item.wiki ? "Tap for the article →" : "Tap for details →";
      const dismiss = document.createElement("span");
      dismiss.className = "t-dismiss";
      dismiss.textContent = "close";
      more.append(open, dismiss);
      this.el.append(more);
    }

    this.el.style.display = "block";
    // Measured once here and kept for move(): the map reports every pointer
    // move, and reading the box on each one would put a forced layout on the
    // path of a mouse drag.
    this.box = this.el.getBoundingClientRect();
    this.move(x, y);
  }

  /** Reposition without rebuilding, keeping the tooltip inside the window. Same
   *  content, so the size measured by show() still holds. */
  move(x, y) {
    if (!this.box) return;
    const pad = 8;
    let left = x + 14;
    let top = y + 14;
    if (this.touch) {
      // A finger covers what it tapped, so the popup is centred above the tap
      // rather than below and right of it, and flips below only when the tap
      // was near the top of the screen.
      left = x - this.box.width / 2;
      top = y - this.box.height - 20;
      if (top < pad) top = y + 24;
    }
    const clamp = (v, extent) => Math.max(pad, Math.min(v, extent - pad));
    this.el.style.left = `${clamp(left, window.innerWidth - this.box.width)}px`;
    this.el.style.top = `${clamp(top, window.innerHeight - this.box.height)}px`;
  }

  hide() {
    this.el.style.display = "none";
    this.el.classList.remove("touch");
    this.box = null;
    this.item = null;
    this.touch = false;
  }
}

export class DetailPanel {
  constructor({ panel, body, closeButton, categories, site, style }) {
    this.panel = panel;
    this.body = body;
    this.categories = categories;
    this.site = site;
    this.style = style;
    this.controller = null;
    closeButton.addEventListener("click", () => this.close());
  }

  close() {
    this.panel.classList.remove("open");
    this.controller?.abort();
    this.controller = null;
  }

  open(item) {
    this.controller?.abort();
    this.controller = new AbortController();
    const { signal } = this.controller;

    const [lon, lat] = item.position;
    this.body.textContent = "";

    const title = document.createElement("h2");
    title.textContent = item.title;

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = [
      kindOf(this.categories, item),
      formatPlace(item, this.categories, this.site),
    ]
      .filter(Boolean)
      .join(" · ");

    const figure = document.createElement("img");
    figure.alt = "";
    figure.hidden = true;

    // The tile's own one-line description shows immediately; the article's
    // summary replaces it when it lands, and it stays if that request fails.
    const site = this.site;
    const fetchSummary = site.summaryFor(item);
    const extract = document.createElement("p");
    extract.className = "extract";
    extract.textContent = item.descr || (fetchSummary ? "Loading summary…" : "");

    const chart = site.hasSeasons
      ? seasonChart(item, this.style.month, { width: 288, height: 34, labels: true })
      : null;

    const facts = document.createElement("dl");
    const addFact = (key, value) => {
      if (value === null || value === undefined || value === "") return;
      const dt = document.createElement("dt");
      dt.textContent = key;
      const dd = document.createElement("dd");
      dd.textContent = value;
      facts.append(dt, dd);
    };
    addFact("Population", formatPopulation(item.pop));
    addFact("Elevation", item.elev != null ? `${nf.format(item.elev)} m` : null);
    addFact(yearLabel(this.categories, item), formatYear(item.year));
    addFact("Country", item.country);
    if (item.derived) {
      // The dot is not where this thing is - it is where the place it points
      // at is, nudged aside so the other people born there are reachable too.
      // Saying so costs one line and is the difference between a map and a
      // wrong map.
      const phrase = this.categories?.locSources?.[item.locSrc] || "at";
      addFact(
        phrase.charAt(0).toUpperCase() + phrase.slice(1),
        item.admin || "an unnamed place"
      );
      addFact("Position", "approximate — spread around that place");
    } else if (site.placePhrase(item)) {
      addFact("Listed in", item.admin);
    } else {
      addFact(site.hasSeasons ? "Part of" : "Admin area", item.admin);
    }
    site.facts(item, addFact);
    if (!item.derived) addFact("Coordinates", `${lat.toFixed(5)}, ${lon.toFixed(5)}`);
    const wikidata = site.wikidataOf(item);
    if (wikidata) addFact("Wikidata", `Q${wikidata}`);

    const links = document.createElement("div");
    links.className = "links";
    const addLink = (label, href) => {
      const a = document.createElement("a");
      a.textContent = label;
      a.href = href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
      links.append(a);
    };
    if (item.wiki) {
      const lang = item.wikiLang || "en";
      addLink(
        site.articleLabel(lang),
        articleUrl(site, lang, item.wiki) + site.articleAnchor(item)
      );
    }
    if (wikidata) addLink("Wikidata", `https://www.wikidata.org/wiki/Q${wikidata}`);
    // No map links for a borrowed, nudged coordinate: they would open a pin on
    // a spot that exists only because this map had to draw the dot somewhere.
    if (!item.derived) {
      addLink("OpenStreetMap", `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=15/${lat}/${lon}`);
      addLink("Google Maps", `https://www.google.com/maps/search/?api=1&query=${lat},${lon}`);
    }

    this.body.append(title, meta, figure, extract);
    if (chart) this.body.append(chart);
    this.body.append(facts, links);
    this.panel.classList.add("open");

    if (!fetchSummary) return;
    fetch(summaryUrl(site, item.wikiLang, item.wiki), { signal })
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => {
        if (!data) return;
        if (data.extract) extract.textContent = data.extract;
        const thumb = data.thumbnail?.source;
        if (thumb) {
          figure.src = thumb;
          figure.hidden = false;
        }
      })
      .catch(() => {
        /* the tile's description is already on screen */
      });
  }
}
