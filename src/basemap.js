// The basemap underneath the labels.
//
// OpenFreeMap rather than CARTO. CARTO's tiles work without a key, but their
// terms have no anonymous tier - free use is for "CARTO grantees" and
// commercial use needs an Enterprise licence - so the map was running on
// something it was not entitled to and could have been rate-limited without
// warning. OpenFreeMap states no limit on views or requests, allows commercial
// use, needs no key or registration, and can be self-hosted if the donated
// hosting ever goes away.
//
// The catch is that OpenFreeMap has no no-labels variant, and this map is
// nothing but labels - two sets of place names on top of each other is
// unreadable. So the style is fetched and every layer that draws text is
// dropped before MapLibre sees it. That is also why the style is an object
// rather than a URL, and why it has to be resolved before the map is created.
//
// Attribution is required and their style JSON does not carry it, so it is
// attached to the sources here, where MapLibre's attribution control finds it.
//
// The water is also faded - see fadeWater below.

const STYLE_URL = {
  light: "https://tiles.openfreemap.org/styles/positron",
  dark: "https://tiles.openfreemap.org/styles/dark",
};

const ATTRIBUTION =
  '<a href="https://openfreemap.org" target="_blank" rel="noopener">OpenFreeMap</a> ' +
  '&copy; <a href="https://www.openmaptiles.org/" target="_blank" rel="noopener">OpenMapTiles</a> ' +
  'Data from <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';

// How far the water is faded toward the land underneath it.
//
// A label's halo is only as good as the contrast between the halo colour and
// what is behind it, and water is the one large surface that sits well away
// from the land tone: positron paints it rgb(194,200,202) against rgb(242,243,
// 240) of land, so a white halo over the sea gives up most of its separation.
// Water is drawn straight onto the background - park is the only fill beneath
// it and that one is on land - so simply making it more transparent blends it
// toward the land colour, without having to parse and mix the style's own
// colours (which arrive as rgb(), hsl() and hex, sometimes as expressions).
//
// Dark fades less. Its water is *lighter* than its land, rgb(27,27,29) against
// rgb(12,12,12), so the problem is the same one mirrored - but it starts from
// 15 levels of contrast rather than 48, and fading it as hard as positron
// would erase the coastline to buy back very little halo.
const WATER_FADE = { light: 0.45, dark: 0.65 };

/**
 * Fade the water fills and the rivers drawn on top of them.
 *
 * Multiplies rather than assigns, so a style that already fades a layer (or
 * zoom-interpolates its opacity, in which case there is nothing sensible to
 * multiply and the fade is used as-is) is not overridden into being *more*
 * opaque than its author intended.
 */
function fadeWater(style, theme) {
  const fade = WATER_FADE[theme] ?? 1;
  for (const layer of style.layers) {
    const sourceLayer = layer["source-layer"];
    if (sourceLayer !== "water" && sourceLayer !== "waterway") continue;
    const key = { fill: "fill-opacity", line: "line-opacity" }[layer.type];
    if (!key) continue;
    const current = layer.paint?.[key];
    layer.paint = {
      ...layer.paint,
      [key]: typeof current === "number" ? current * fade : fade,
    };
  }
}

/** Land-coloured nothing, for when the style server cannot be reached. */
function fallbackStyle(theme) {
  return {
    version: 8,
    sources: {},
    layers: [
      {
        id: "background",
        type: "background",
        paint: { "background-color": theme === "dark" ? "#0c0c0c" : "#f2f3f0" },
      },
    ],
  };
}

const cache = new Map();

export async function basemapStyle(theme) {
  if (cache.has(theme)) return cache.get(theme);
  const promise = fetch(STYLE_URL[theme])
    .then((r) => {
      if (!r.ok) throw new Error(`${r.status} for ${STYLE_URL[theme]}`);
      return r.json();
    })
    .then((style) => {
      // Anything with a text-field: place names, road names, shields, water
      // names. Icon-only layers stay - they are small and do not compete.
      style.layers = style.layers.filter(
        (layer) => !(layer.layout && layer.layout["text-field"])
      );
      fadeWater(style, theme);
      for (const source of Object.values(style.sources ?? {})) {
        source.attribution = ATTRIBUTION;
      }
      return style;
    })
    .catch((err) => {
      console.warn("basemap style unavailable, drawing labels on plain ground", err);
      return fallbackStyle(theme);
    });
  cache.set(theme, promise);
  // A copy each time: MapLibre takes ownership of the object it is handed, and
  // toggling the theme twice would otherwise pass back one it has already
  // rewritten.
  return promise.then((style) => structuredClone(style));
}
