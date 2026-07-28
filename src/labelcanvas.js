// The labels, drawn with the browser's own text rasteriser.
//
// deck.gl's TextLayer draws from a signed-distance field: one master rendering
// of each glyph, sampled by the GPU at whatever size a label happens to be.
// That is the right trade for a hundred thousand labels and the wrong one for
// three hundred, because it gives up everything a text rasteriser does. The
// master is minified to a quarter of its size for a 12 pixel label; the alpha
// ramp is a single uniform that can only be correct at one size; and each glyph
// quad lands on a fractional device pixel with no hinting, so the stems within
// one word come out at different weights. The arithmetic is in layers.js, and
// the visible result is text that looks eroded rather than small.
//
// fillText has none of those problems: it hints stems onto the pixel grid,
// antialiases per size, and takes a halo width in real pixels rather than as a
// fraction of an em.
//
// What it costs is CPU. Rasterising text is orders of magnitude dearer than
// drawing a textured quad, and doing it for every label on every frame of a pan
// would not hold 60fps at the top of the density slider. So the labels are
// rasterised once into a single atlas canvas when the selection changes - which
// is exactly when the declutter pass runs anyway - and every frame after that
// is one drawImage per label out of that atlas. Panning rasterises nothing.
//
// One atlas rather than a canvas per label, deliberately: a few hundred small
// canvases are a few hundred textures for the compositor to track, and past a
// browser-specific limit they stop being GPU-backed at all, which turns every
// blit into an upload.

import { FONT_FAMILY, FONT_WEIGHT, HALO, INK, drawnSize } from "./layers.js";

export const canvasLabelTuning = {
  // CSS pixels of halo around the glyphs. A stroke is centred on the outline it
  // follows, so the stroke is set to twice this. 1.2 is about what a paper map
  // uses - and it is a real width at every label size, which is the whole point
  // of being here rather than in a distance field.
  halo: 1.2,
  // Round each label onto the device pixel grid, so the stems land where the
  // rasteriser hinted them. This is the difference between crisp and nearly
  // crisp. The cost is that labels then move in whole pixels while the basemap
  // under them moves in fractions, so they creep by up to half a pixel against
  // it during a slow pan. Worth it; turn it off to see why.
  snap: true,
};
if (typeof window !== "undefined") window.canvasLabelTuning = canvasLabelTuning;

// Atlas geometry. Wide rather than tall, because rows are packed left to right
// and a label is far wider than it is high. 1,500 labels - the top of the
// density slider - at 2x comes to roughly 4096x4700, inside the 8192 that even
// modest GPUs allow.
const ATLAS_WIDTH = 4096;
const ATLAS_MAX_HEIGHT = 8192;

// Labels are drawn a little beyond the viewport so panning does not pop them in
// at the edge, matching the slack the declutter pass already leaves.
const CULL_MARGIN = 160;

const rgba = ([r, g, b, a = 255]) => `rgba(${r},${g},${b},${a / 255})`;

export class LabelCanvas {
  constructor(container) {
    this.canvas = document.createElement("canvas");
    this.canvas.id = "label-canvas";
    Object.assign(this.canvas.style, {
      position: "absolute",
      inset: "0",
      // Drawn on top of deck's canvas but must never take the pointer from it:
      // deck still owns dragging, zooming and picking the dots. Hit testing for
      // the labels is `pick` below, driven by the hover events deck reports.
      pointerEvents: "none",
      // Above deck's own canvas and the basemap under it, which it stacks at 1
      // and 2, and below the panels at 10. #map does not create a stacking
      // context, so this number is in the same scale as theirs.
      zIndex: "5",
    });
    container.appendChild(this.canvas);
    this.ctx = this.canvas.getContext("2d");

    this.atlas = document.createElement("canvas");
    this.atlasCtx = this.atlas.getContext("2d");

    this.entries = []; // one per label: atlas rect, anchor, position, item
    this.boxes = []; // screen rectangles from the last draw, for picking
    this.key = null; // what the atlas was built from
    this.labelled = null;
    this.dpr = 1;
    this.dropped = 0;
  }

  /**
   * Rasterise the label set into the atlas, if anything that changes how the
   * labels look has changed. Cheap to call every frame: the test is a string
   * compare plus an array identity, the same way layers.js caches its split.
   */
  prepare({ labelled, style, theme }) {
    const dpr = window.devicePixelRatio || 1;
    const key = [
      dpr,
      theme,
      style.colorByCategory,
      style.qrankWeight,
      style.populationWeight,
      style.labelScale,
      canvasLabelTuning.halo,
      style.palette.join(),
    ].join("|");
    if (this.labelled === labelled && this.key === key) return;
    this.labelled = labelled;
    this.key = key;
    this.dpr = dpr;
    this.build(labelled, style, theme);
  }

  /** Measure, pack and rasterise every label into the atlas. */
  build(labelled, style, theme) {
    const ctx = this.atlasCtx;
    const dpr = this.dpr;
    const pad = Math.ceil(canvasLabelTuning.halo * dpr) + 1;
    const ink = rgba(INK[theme] ?? INK.light);
    const halo = rgba(HALO[theme] ?? HALO.light);

    // Pass one: measure and pack. Sizing a canvas clears it, so nothing can be
    // drawn until the whole layout is known.
    const planned = [];
    let x = 0;
    let rowTop = 0;
    let rowHeight = 0;
    this.dropped = 0;
    for (const placement of labelled) {
      const size = drawnSize(placement.item, style) * dpr;
      const text = placement.item.title;
      ctx.font = `${FONT_WEIGHT} ${size}px ${FONT_FAMILY}`;
      const m = ctx.measureText(text);

      // Ink bounds size the sprite, so the atlas stays small. Font bounds place
      // the baseline, so a name with a descender sits on the same line as one
      // without - which is what deck's "center" alignment does too, and what
      // keeps a row of labels from looking wavy.
      const inkLeft = m.actualBoundingBoxLeft || 0;
      const inkRight = m.actualBoundingBoxRight || m.width;
      const ascent = m.actualBoundingBoxAscent || size * 0.8;
      const descent = m.actualBoundingBoxDescent || size * 0.2;
      const fontAscent = m.fontBoundingBoxAscent ?? size * 0.8;
      const fontDescent = m.fontBoundingBoxDescent ?? size * 0.2;

      // Where the text origin goes inside the sprite, and how big the sprite
      // has to be to hold both the ink and the advance width.
      const originX = pad + Math.max(0, inkLeft);
      const originY = pad + Math.ceil(ascent);
      const sw = Math.ceil(originX + Math.max(m.width, inkRight)) + pad;
      const sh = originY + Math.ceil(descent) + pad;

      if (x + sw > ATLAS_WIDTH) {
        x = 0;
        rowTop += rowHeight;
        rowHeight = 0;
      }
      if (rowTop + sh > ATLAS_MAX_HEIGHT) {
        this.dropped = labelled.length - planned.length;
        break;
      }

      planned.push({
        placement,
        text,
        size,
        sx: x,
        sy: rowTop,
        sw,
        sh,
        originX,
        originY,
        // Where in the sprite the label's own coordinate lands: the middle of
        // the advance box across, the middle of the font box down.
        ax: originX + m.width / 2,
        ay: originY - (fontAscent - fontDescent) / 2,
      });
      x += sw;
      rowHeight = Math.max(rowHeight, sh);
    }
    const height = Math.max(1, rowTop + rowHeight);

    if (this.atlas.width !== ATLAS_WIDTH || this.atlas.height !== height) {
      this.atlas.width = ATLAS_WIDTH;
      this.atlas.height = height;
    } else {
      ctx.clearRect(0, 0, ATLAS_WIDTH, height);
    }

    // Pass two: draw. Halo first, as a stroke under the fill - the order that
    // `paint-order: stroke fill` gives in SVG, and the reason the halo sits
    // outside the glyph instead of eating into it.
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    ctx.lineJoin = "round";
    // Without this the sharp inner corners of A, V and W throw spikes several
    // times the width of the stroke.
    ctx.miterLimit = 2;
    ctx.strokeStyle = halo;
    ctx.lineWidth = canvasLabelTuning.halo * 2 * dpr;
    const stroked = canvasLabelTuning.halo > 0;

    this.entries = [];
    for (const p of planned) {
      ctx.font = `${FONT_WEIGHT} ${p.size}px ${FONT_FAMILY}`;
      ctx.fillStyle = style.colorByCategory
        ? style.palette[p.placement.item.cat]
        : ink;
      const originX = p.sx + p.originX;
      const originY = p.sy + p.originY;
      if (stroked) ctx.strokeText(p.text, originX, originY);
      ctx.fillText(p.text, originX, originY);
      this.entries.push({
        item: p.placement.item,
        position: p.placement.position,
        sx: p.sx,
        sy: p.sy,
        sw: p.sw,
        sh: p.sh,
        ax: p.ax,
        ay: p.ay,
      });
    }
  }

  /** Blit the atlas at this frame's screen positions. */
  draw(viewport) {
    const dpr = this.dpr;
    const width = Math.round(viewport.width * dpr);
    const height = Math.round(viewport.height * dpr);
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width;
      this.canvas.height = height;
      // Both sizes, always. A canvas is a replaced element, so `inset: 0` with
      // an auto width does not stretch it to the container the way it would a
      // div - the used width is the intrinsic one, which is the backing store.
      // Leave these off and the element lays out dpr times too large, and every
      // label drifts from its dot in proportion to its distance from the top
      // left corner.
      this.canvas.style.width = `${viewport.width}px`;
      this.canvas.style.height = `${viewport.height}px`;
    }
    const ctx = this.ctx;
    ctx.clearRect(0, 0, width, height);

    const margin = CULL_MARGIN * dpr;
    const boxes = [];
    for (const e of this.entries) {
      const [px, py] = viewport.project(e.position);
      let dx = px * dpr - e.ax;
      let dy = py * dpr - e.ay;
      if (canvasLabelTuning.snap) {
        dx = Math.round(dx);
        dy = Math.round(dy);
      }
      if (dx + e.sw < -margin || dy + e.sh < -margin) continue;
      if (dx > width + margin || dy > height + margin) continue;
      ctx.drawImage(this.atlas, e.sx, e.sy, e.sw, e.sh, dx, dy, e.sw, e.sh);
      boxes.push({
        x0: dx / dpr,
        y0: dy / dpr,
        x1: (dx + e.sw) / dpr,
        y1: (dy + e.sh) / dpr,
        item: e.item,
      });
    }
    this.boxes = boxes;
  }

  /**
   * The item whose label is under a point, in CSS pixels, or null. Walked back
   * to front, so where labels do overlap - which only happens with collision
   * testing switched off - the one drawn on top is the one that answers.
   */
  pick(x, y) {
    if (x == null || y == null) return null;
    for (let i = this.boxes.length - 1; i >= 0; i--) {
      const b = this.boxes[i];
      if (x >= b.x0 && x <= b.x1 && y >= b.y0 && y <= b.y1) return b.item;
    }
    return null;
  }

  clear() {
    this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    this.boxes = [];
    this.entries = [];
    this.key = null;
    this.labelled = null;
  }
}
