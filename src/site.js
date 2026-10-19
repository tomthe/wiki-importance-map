// The two maps this code draws, and everything that differs between them.
//
// index.html is the Wikipedia importance map; voyage/index.html is the travel
// map built from Wikivoyage. They share the tile format, the loader, the label
// placement, the search and the panels - a page says which one it is with
// <html data-site="...">, and this file is the only place that answers
// "where does the article live" or "what does the importance number mean".

const nf = new Intl.NumberFormat();

/** Round a count to two significant figures: pageviews are not exact, and
 *  "4,271 views" claims a precision the dumps do not have. */
function roughly(value) {
  if (value < 100) return nf.format(Math.round(value));
  const step = Math.pow(10, Math.floor(Math.log10(value)) - 1);
  return nf.format(Math.round(value / step) * step);
}

const wikipedia = {
  name: "wikipedia",
  articleHost: (lang) => `https://${lang || "en"}.wikipedia.org`,
  articleLabel: (lang) => (lang === "en" ? "Wikipedia" : `Wikipedia (${lang})`),
  // Settlements and Administrative are the two categories the importance score
  // already favours - they dominate every zoom and mostly restate what the
  // basemap says. Starting with them off makes the first view show the things
  // people came for, and both are one click away.
  //
  // People deliberately starts off. It is the layer this map did not have, and
  // unlike Settlements it duplicates nothing underneath it.
  defaultOff: ["Settlements", "Administrative", "People"],
  thingsName: "places",
  dataNote: (m) =>
    `${m.itemCount.toLocaleString()} geolocated articles in ` +
    `${m.tileCount.toLocaleString()} tiles · zoom 0–${m.maxZoom} · ` +
    `${(m.pack.bytes / 1e6).toFixed(0)} MB in ${m.pack.parts.length} file(s)`,
  searchNote: (root) =>
    `${root.items.toLocaleString()} places above importance ` +
    `${(root.minScore * 100).toFixed(0)} are findable`,
  hasSeasons: false,
  /** Mix the two importance signals the way the slider asks for. */
  importance: (item, style) =>
    item.pr * (1 - style.qrankWeight) + item.qr * style.qrankWeight,
  rankLine: (item) =>
    `importance ${(item.score * 100).toFixed(0)} · ` +
    `links ${(item.pr * 100).toFixed(0)} · views ${(item.qr * 100).toFixed(0)}` +
    (item.sitelinks ? ` · ${item.sitelinks} languages` : "") +
    (item.wiki ? "" : " · no article"),
  placePhrase: () => "",
  facts: (item, add) => {
    add(
      "Language editions",
      item.sitelinks ? (item.sitelinks >= 255 ? "255+" : String(item.sitelinks)) : null
    );
    add("Importance", `${(item.score * 100).toFixed(1)} / 100`);
    add("PageRank", `${(item.pr * 100).toFixed(1)} / 100`);
    add("Pageviews", `${(item.qr * 100).toFixed(1)} / 100`);
  },
  wikidataOf: (item) => item.qid,
  /** Whether the panel should fetch the article's own summary for this item. */
  summaryFor: (item) => Boolean(item.wiki),
  articleAnchor: () => "",
};

// --------------------------------------------------------------- the travel map

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July",
                     "August", "September", "October", "November", "December"];
const SYNTHETIC = 0x80000000; // ids from here up are not Wikidata items

// Where in a guide each kind of listing lives, so "Listed in Potsdam" can open
// the article at the right section. Keyed by category name.
const SECTIONS = {
  en: { See: "See", Do: "Do", Buy: "Buy", Eat: "Eat", Drink: "Drink", Sleep: "Sleep",
        Go: "Get_around", Practical: "Stay_safe" },
  de: { See: "Sehenswürdigkeiten", Do: "Aktivitäten", Buy: "Einkaufen", Eat: "Küche",
        Drink: "Nachtleben", Sleep: "Unterkunft", Go: "Mobilität",
        Practical: "Praktische_Hinweise" },
};

/**
 * How much of a year's traffic an item gets in each month, from the twelve
 * bytes the tile carries (relative to the item's own busiest month).
 * Null when it had no views at all.
 */
export function monthShares(item) {
  const extra = item.extra;
  if (!extra) return null;
  let sum = 0;
  for (let m = 0; m < 12; m++) sum += extra[m];
  if (!sum) return null;
  const shares = new Array(12);
  for (let m = 0; m < 12; m++) shares[m] = extra[m] / sum;
  return shares;
}

function voyage(manifest) {
  const scale = manifest.importanceScale || 1;
  const destinationCat = manifest.categories.findIndex((c) => c.name === "Destination");
  const isDestination = (item) => item.cat === destinationCat;
  // The pageviews are from before crawlers swamped Wikivoyage's "user" traffic
  // in late 2024 - see pipeline/voyage/extract_pageviews.py - so say when.
  const period = manifest.viewsFrom
    ? `${manifest.viewsFrom.slice(0, 4)}–${manifest.viewsTo.slice(0, 4)}`
    : "";
  /** Views over the twelve months. For a listing this is its article's traffic
   *  less the listing's penalty, which is only meaningful as a ranking. */
  const annualViews = (item) => Math.pow(10, item.pr * scale) - 1;

  /**
   * Importance in the selected month.
   *
   * The year's score is log views over twelve months, normalised. A month's is
   * the same thing for that month's views annualised - twelve times them - so
   * a place as busy in March as on average scores the same in March as for the
   * year. `seasonWeight` then adds how far this month is above or below the
   * item's own average month, in doublings: 0 shows where people are looking
   * in March, 1 shows what is *in season* in March, which is not the same map.
   * London is the first answer every month and the second answer never.
   */
  const importance = (item, style) => {
    const month = style.month;
    if (!month) return item.pr;
    if (item._season === style.seasonKey) return item._seasonValue;
    const shares = monthShares(item);
    let value = item.pr;
    if (shares) {
      const share = shares[month - 1];
      value = Math.log10(1 + 12 * annualViews(item) * share) / scale;
      if (style.seasonWeight) {
        value += style.seasonWeight * 0.1 * Math.log2(Math.max(12 * share, 1 / 64));
      }
      value = Math.max(0, Math.min(1, value));
    }
    item._season = style.seasonKey;
    item._seasonValue = value;
    return value;
  };

  const busiest = (item) => {
    const shares = monthShares(item);
    if (!shares) return null;
    let best = 0;
    for (let m = 1; m < 12; m++) if (shares[m] > shares[best]) best = m;
    // A flat year has no busiest month worth naming.
    return shares[best] > 1.25 / 12 ? best : null;
  };

  return {
    name: "voyage",
    articleHost: (lang) => `https://${lang || "en"}.wikivoyage.org`,
    articleLabel: (lang) => (lang === "en" ? "Wikivoyage" : `Wikivoyage (${lang})`),
    // The four sections that make a map dense without making it more useful for
    // deciding where to go. All a click away.
    defaultOff: ["Buy", "Eat", "Drink", "Sleep", "Go", "Practical"],
    thingsName: "places",
    dataNote: (m) =>
      `${m.destinationCount.toLocaleString()} destinations and ` +
      `${(m.itemCount - m.destinationCount).toLocaleString()} listings in ` +
      `${m.tileCount.toLocaleString()} tiles · ` +
      `${(m.pack.bytes / 1e6).toFixed(0)} MB`,
    searchNote: (root) =>
      `every destination and ${(root.items).toLocaleString()} places in all are findable`,
    hasSeasons: true,
    months: MONTHS,
    monthsLong: MONTHS_LONG,
    importance,
    isDestination,
    busiest,
    rankLine: (item) => {
      const peak = busiest(item);
      const peakText = peak === null ? "steady all year" : `busiest in ${MONTHS[peak]}`;
      if (isDestination(item)) {
        return (
          `~${roughly(annualViews(item))} views a year · ${peakText}` +
          (item.sitelinks > 1 ? ` · ${item.sitelinks} languages` : "")
        );
      }
      return `importance ${(item.pr * 100).toFixed(0)} · its guide is ${peakText}`;
    },
    placePhrase: (item) => (isDestination(item) ? "" : "in"),
    facts: (item, add) => {
      const status = manifest.statuses?.[item.extra?.[12] ?? 0];
      if (isDestination(item)) {
        add("Views a year", `~${roughly(annualViews(item))}, every edition, ${period}`);
        add("Wikivoyage editions", item.sitelinks ? String(item.sitelinks) : null);
        if (status) add("Guide status", `${status} (English)`);
      }
      const peak = busiest(item);
      add("Busiest month", peak === null ? "none — steady all year" : MONTHS_LONG[peak]);
      add("Importance", `${(item.pr * 100).toFixed(1)} / 100`);
    },
    wikidataOf: (item) => (item.qid < SYNTHETIC ? item.qid : null),
    // A listing's own words are already on screen; the article's summary would
    // be about the town, not about the restaurant.
    summaryFor: (item) => Boolean(item.wiki) && isDestination(item),
    articleAnchor: (item) => {
      if (isDestination(item)) return "";
      const section = SECTIONS[item.wikiLang]?.[manifest.categories[item.cat]?.name];
      return section ? `#${encodeURIComponent(section)}` : "";
    },
  };
}

export function siteFor(name, manifest) {
  return name === "voyage" ? voyage(manifest) : wikipedia;
}
