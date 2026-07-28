// deck.gl layers.
//
// Three layers, drawn bottom to top: a dot for every item in view, a leader
// line for labels that had to be moved off their anchor to fit, and the labels
// themselves for the ones that survive decluttering (see declutter.js - the
// selection happens in main.js so it can be cached across frames).
//
// The dots are never decluttered, so a place whose label lost a collision is
// still visibly there and still hoverable.

import { hexToRgb } from "./categories.js";

const { TextLayer, ScatterplotLayer, LineLayer } = deck;

// Exported because three places have to agree about the font: the renderer
// that draws the labels, the pass that measures them to reserve collision
// boxes, and whichever of the two renderers is not in use. A mismatch here is
// a label whose box is the wrong size, which shows up as text that overlaps
// or as gaps where nothing was allowed to go.
export const FONT_FAMILY = 'system-ui, -apple-system, "Segoe UI", sans-serif';
export const FONT_WEIGHT = 500;

// log10 bounds the population term is stretched across. Anchoring the low end
// at a real floor (a small town, not zero) instead of log10(1) means that
// range - where the vast majority of populated items actually live - gets
// most of the 0..1 span, so the slider visibly separates a village from a
// metropolis instead of bunching everything into a narrow middle band. A
// handful of country-scale entries exceed the top and simply clip at 1.
const POP_LOG_MIN = 3; // 1,000
const POP_LOG_MAX = 8; // 100,000,000

// Below this the label is close enough to its dot to be unambiguous.
const LEADER_MIN_PX = 12;

// Drawn label size is clamped to this range, and the buckets below are cut in
// the same units, so a label lands in the bucket it is actually drawn at.
export const SIZE_MIN = 9;
export const SIZE_MAX = 46;

/** The size a label is really drawn at, clamps included. */
export function drawnSize(item, style) {
  return Math.min(SIZE_MAX, Math.max(SIZE_MIN, labelSize(item, style)));
}

// Ink and halo, per theme. Opaque halo: its outer edge is a gradient already,
// so a colour below full alpha is faded twice and the tail stops separating the
// text from anything.
//
// A black halo in light was tried, on the theory that a white one at 10 to 14
// pixels is a fringe pushed into the counters of e, a and o and between the
// stems of m - thinning the letterforms it is there to protect - where a dark
// one would thicken them instead. It does thicken them, and it looks worse:
// the letters go muddy and the category colours go muddier. White it is.
export const INK = {
  light: [17, 17, 16],
  dark: [242, 242, 240],
};
export const HALO = {
  light: [255, 255, 255, 255],
  dark: [8, 8, 8, 255],
};

// Antialiasing and halo width, in device pixels.
//
// deck draws SDF text with two uniforms, and neither is scaled by the size a
// label is drawn at. `smoothing` is the half-width of the alpha ramp at the
// glyph edge; `outlineWidth` sets the threshold the halo runs out to. Both are
// in distance-field units, so in pixels they come out as
//
//   ramp = 2 * smoothing * (radius / fontSize) * drawnPx * dpr
//   halo = (0.75 - outlineBuffer - smoothing) * (radius / fontSize) * drawnPx * dpr
//
// which means exactly one label size gets a one-pixel edge. At deck's defaults
// that size is about 16 device pixels: below it there is less than a pixel of
// ramp left, the edge stops being antialiased at all and one-pixel stems snap
// on and off between pixels; above it the edge goes soft. Most labels here are
// 10 to 14 CSS pixels, so on a 1x display the whole map sat on the aliased
// side - a 12 pixel label had 0.44 pixels of ramp.
//
// So the pixel widths are the input here and the uniforms are solved for, per
// size bucket and for the display the page is actually on. Both are read every
// render and exposed as window.labelTuning, so they can be tried from the
// console - set one, pan, and the next frame uses it.
export const labelTuning = {
  ramp: 2.4, // device px of antialiasing at the glyph edge
  halo: 1.0, // device px of solid halo before it fades out; 0 for none at all
};
if (typeof window !== "undefined") window.labelTuning = labelTuning;

// One master per size range. A single atlas cannot serve 9 and 46 pixels: the
// small labels get minified past their stems, the large ones magnified, and
// the uniforms above can only be right about one size at a time anyway. Two
// keep every label within about a factor of two of its own master, and give
// the solve two ranges to be right about. `buffer` is the glyph's padding in
// the atlas and has to hold the whole field, which reaches 0.75 * radius;
// `tunedFor` is the size within the bucket the solve aims at.
const BUCKETS = [
  { name: "small", maxPx: 20, fontSize: 24, buffer: 6, radius: 8, tunedFor: 14 },
  { name: "large", maxPx: Infinity, fontSize: 48, buffer: 12, radius: 15, tunedFor: 30 },
];

/** deck's two SDF uniforms, solved backwards from the pixel widths wanted. */
function sdfProps({ fontSize, buffer, radius, tunedFor }, dpr) {
  // Field units per device pixel, at the size this bucket is tuned for.
  const unit = fontSize / (radius * tunedFor * dpr);
  const smoothing = Math.min(0.25, Math.max(0.01, (labelTuning.ramp * unit) / 2));
  const fontSettings = { sdf: true, fontSize, buffer, radius, smoothing };
  if (!(labelTuning.halo > 0)) return { fontSettings, outlineWidth: 0 };
  // The halo runs from the glyph edge at 0.75 down to the threshold plus one
  // ramp, clamped at `smoothing` - as far out as the field reaches.
  const outlineBuffer = Math.max(
    smoothing,
    0.75 - smoothing - labelTuning.halo * unit
  );
  // Inverted through deck's own max(smoothing, 0.75 * (1 - outlineWidth)).
  return { fontSettings, outlineWidth: 1 - outlineBuffer / 0.75 };
}

const bucketFor = (size) => BUCKETS.findIndex((bucket) => size <= bucket.maxPx);

// Which bucket each label is in changes only when the selection or one of the
// sliders feeding label size does. Recomputing the split per render would hand
// the text layers a new data array on every pan frame, and re-tessellating a
// few hundred titles into glyphs is not free - so cache it against the
// `labelled` array, which main.js keeps stable between selections.
let splitCache = { labelled: null, key: null, groups: null };

function splitBySize(labelled, style) {
  const key = `${style.qrankWeight} ${style.populationWeight} ${style.labelScale}`;
  if (splitCache.labelled === labelled && splitCache.key === key) {
    return splitCache.groups;
  }
  const groups = BUCKETS.map(() => []);
  for (const placement of labelled) {
    groups[bucketFor(drawnSize(placement.item, style))].push(placement);
  }
  splitCache = { labelled, key, groups };
  return groups;
}

/** Mix the two importance signals the way the slider asks for. */
export function importanceOf(item, qrankWeight) {
  return item.pr * (1 - qrankWeight) + item.qr * qrankWeight;
}

/**
 * What drives label size, and therefore who wins a collision.
 *
 * Importance alone makes a famous ruin outshout the city around it. Population
 * is the other thing people mean by "big", but only two thirds of a million
 * items have one, so it is blended in rather than substituted: items with no
 * population keep their importance and stay comparable.
 */
export function weightOf(item, qrankWeight, populationWeight) {
  const importance = importanceOf(item, qrankWeight);
  if (!populationWeight || item.pop == null) return importance;
  const scaled = Math.max(
    0,
    Math.min(1, (Math.log10(1 + item.pop) - POP_LOG_MIN) / (POP_LOG_MAX - POP_LOG_MIN))
  );
  return importance * (1 - populationWeight) + scaled * populationWeight;
}

/** Label pixel size. Shared with the declutter pass so the boxes it reserves
 *  match what actually gets drawn. */
export function labelSize(item, style) {
  const weight = weightOf(item, style.qrankWeight, style.populationWeight);
  return (10 + 30 * weight * weight) * style.labelScale;
}

/**
 * @param textLayers draw the label text here, from the distance field. False
 *        when labelcanvas.js is drawing it instead, in which case this builds
 *        only the dots and the leader lines. Hovering and clicking are handled
 *        at the deck level in main.js, so no layer here needs a callback: the
 *        dots stay pickable and the canvas does its own hit testing.
 */
export function buildLayers({ items, labelled, style, theme, textLayers = true }) {
  const colors = style.palette.map(hexToRgb);
  const ink = INK[theme] ?? INK.light;
  const halo = HALO[theme] ?? HALO.light;
  const leader = theme === "dark" ? [140, 140, 136, 150] : [110, 110, 105, 140];
  const dotAlpha = theme === "dark" ? 190 : 165;
  const { qrankWeight, labelScale, populationWeight, colorByCategory, labelBudget } =
    style;

  const triggers = {
    qrankWeight,
    labelScale,
    populationWeight,
    colorByCategory,
    theme,
    labelBudget,
  };

  // Hollow dots for items drawn at a coordinate they do not own - a person at
  // their birthplace, a painting at its museum. It is a non-colour channel, so
  // it stacks with the category hue instead of competing with it, and it says
  // the one thing colour cannot: this point is approximate. Filled means the
  // item really is there.
  const dots = new ScatterplotLayer({
    id: "dots",
    data: items,
    pickable: true,
    stroked: true,
    filled: true,
    lineWidthUnits: "pixels",
    radiusUnits: "pixels",
    radiusMinPixels: 1.2,
    radiusMaxPixels: 9,
    getPosition: (d) => d.position,
    getRadius: (d) =>
      1.4 + 7 * Math.pow(weightOf(d, qrankWeight, populationWeight), 1.6),
    getFillColor: (d) => (d.derived ? [0, 0, 0, 0] : [...colors[d.cat], dotAlpha]),
    getLineColor: (d) => [...colors[d.cat], d.derived ? dotAlpha : 0],
    getLineWidth: (d) => (d.derived ? 1.2 : 0),
    updateTriggers: {
      getRadius: triggers,
      getFillColor: triggers,
      getLineColor: triggers,
      getLineWidth: triggers,
    },
  });

  const displaced = labelled.filter((p) => p.offset > LEADER_MIN_PX);
  const leaders = new LineLayer({
    id: "leaders",
    data: displaced,
    getSourcePosition: (d) => d.item.position,
    getTargetPosition: (d) => d.position,
    getColor: leader,
    getWidth: 1,
    widthUnits: "pixels",
    updateTriggers: { getColor: triggers },
  });

  // One layer per size bucket. Same layer in every respect but the master the
  // glyphs come from and the two uniforms solved for it.
  const dpr = (typeof window !== "undefined" && window.devicePixelRatio) || 1;
  const groups = textLayers ? splitBySize(labelled, style) : null;
  const labels = !textLayers
    ? []
    : BUCKETS.map(
        (bucket, i) =>
          new TextLayer({
            id: `labels-${bucket.name}`,
            data: groups[i],
            pickable: true,
            getPosition: (d) => d.position,
            getText: (d) => d.item.title,
            getSize: (d) => labelSize(d.item, style),
            sizeUnits: "pixels",
            sizeMinPixels: SIZE_MIN,
            sizeMaxPixels: SIZE_MAX,
            getColor: (d) => (colorByCategory ? colors[d.item.cat] : ink),
            getTextAnchor: "middle",
            getAlignmentBaseline: "center",
            fontFamily: FONT_FAMILY,
            fontWeight: FONT_WEIGHT,
            characterSet: "auto",
            outlineColor: halo,
            ...sdfProps(bucket, dpr),
            updateTriggers: { getSize: triggers, getColor: triggers },
          })
      );

  return {
    layers: [dots, leaders, ...labels],
    visibleCount: items.length,
    labelledCount: labelled.length,
    displacedCount: displaced.length,
  };
}
