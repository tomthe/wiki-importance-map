// Screen-space label selection.
//
// deck.gl ships a CollisionFilterExtension that does this on the GPU, and the
// old version of this map used it. It is replaced here because its behaviour
// depends on the renderer: on a software rasteriser it culled 297 of 300
// labels over central Paris, and there is no way to detect that from inside
// the page. Doing the work in JS is a few milliseconds and always produces the
// same answer.
//
// The method is the standard one: walk candidates in importance order, keep a
// label if its box does not touch a box already kept, and stop at the budget.
// An occupancy grid makes the overlap test O(1) per label.
//
// Crowding
// --------
// The hard case is several important things at one point - a cathedral, the
// square it stands on and the city named after it all share a coordinate, and
// straight collision testing shows one and silently drops the rest. Three ways
// out, and the map offers all three because they suit different questions:
//
//   * spread      try the label above, below and beside its anchor before
//                 giving up, and draw a leader line when it ends up displaced.
//                 The default: it costs nothing and recovers most of them.
//   * ignore      draw every label the budget allows, overlapping or not.
//                 Illegible in a city centre, but it is the only way to see
//                 that eleven things are stacked on one dot.
//   * max importance (in main.js) hide the loudest items so the next tier is
//                 not competing with them at all.

import { FONT_FAMILY, FONT_WEIGHT } from "./layers.js";

const CELL = 20; // px; smaller than a label, so a box covers several cells
const MARGIN = 140; // px of off-screen slack, so labels do not pop at the edge

// Beyond this a label reads as belonging to nothing, so it is better to drop it.
const MAX_DISPLACEMENT = 3;

// Text width depends on the string, and measuring is far too slow to redo per
// frame - but titles repeat across every render, so measure once and keep it.
const emWidths = new Map();
let context = null;

function emWidth(text) {
  let width = emWidths.get(text);
  if (width !== undefined) return width;
  if (!context) {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 8;
    context = canvas.getContext("2d");
    // The same font the labels are drawn in, from one definition - a box
    // measured in a different face is a box of the wrong size.
    context.font = `${FONT_WEIGHT} 32px ${FONT_FAMILY}`;
  }
  width = context.measureText(text).width / 32;
  emWidths.set(text, width);
  if (emWidths.size > 60000) emWidths.clear(); // bounded, rebuilt lazily
  return width;
}

/** Where to try putting a label, in order. Straight up and down first: a
 *  stack of names over one dot still reads as a list, where a scatter of
 *  diagonals does not. */
function* placements(halfW, halfH, spread) {
  yield [0, 0];
  if (!spread) return;
  const stepY = 2 * halfH + 3;
  const stepX = halfW + 6;
  for (let ring = 1; ring <= MAX_DISPLACEMENT; ring++) {
    yield [0, -ring * stepY];
    yield [0, ring * stepY];
    yield [stepX, -ring * stepY];
    yield [-stepX, -ring * stepY];
    yield [stepX, ring * stepY];
    yield [-stepX, ring * stepY];
  }
}

/**
 * @returns placements, most important first:
 *          {item, position, offset} where `position` is where the text goes
 *          (the item's own coordinate unless it had to be displaced) and
 *          `offset` is how far in pixels it moved, for the leader line.
 */
export function selectLabels({
  items,
  viewport,
  importance,
  sizeOf,
  budget,
  avoid = [],
  spread = true,
  ignoreCollisions = false,
}) {
  const occupied = new Set();
  const kept = [];
  const width = viewport.width;
  const height = viewport.height;
  const cellKey = (cx, cy) => (cx + 4096) * 32768 + (cy + 4096);

  // Block out the panels first, so no label is drawn where it cannot be read.
  // Skipped when collisions are ignored, since nothing is being avoided then.
  if (!ignoreCollisions) {
    for (const rect of avoid) {
      const cx1 = Math.floor(rect.x1 / CELL);
      const cy1 = Math.floor(rect.y1 / CELL);
      for (let cy = Math.floor(rect.y0 / CELL); cy <= cy1; cy++) {
        for (let cx = Math.floor(rect.x0 / CELL); cx <= cx1; cx++) {
          occupied.add(cellKey(cx, cy));
        }
      }
    }
  }

  // Importance order decides who wins a collision, so sort once up front.
  const ranked = items
    .map((item) => ({ item, value: importance(item) }))
    .sort((a, b) => b.value - a.value);

  for (const { item } of ranked) {
    if (kept.length >= budget) break;
    const [x, y] = viewport.project(item.position);
    if (x < -MARGIN || y < -MARGIN || x > width + MARGIN || y > height + MARGIN) {
      continue;
    }
    if (ignoreCollisions) {
      kept.push({ item, position: item.position, offset: 0 });
      continue;
    }

    const size = sizeOf(item);
    const halfW = (emWidth(item.title) * size) / 2 + 3;
    const halfH = size / 2 + 2;

    let placed = null;
    for (const [dx, dy] of placements(halfW, halfH, spread)) {
      const cx0 = Math.floor((x + dx - halfW) / CELL);
      const cx1 = Math.floor((x + dx + halfW) / CELL);
      const cy0 = Math.floor((y + dy - halfH) / CELL);
      const cy1 = Math.floor((y + dy + halfH) / CELL);

      let blocked = false;
      for (let cy = cy0; cy <= cy1 && !blocked; cy++) {
        for (let cx = cx0; cx <= cx1; cx++) {
          if (occupied.has(cellKey(cx, cy))) {
            blocked = true;
            break;
          }
        }
      }
      if (blocked) continue;

      for (let cy = cy0; cy <= cy1; cy++) {
        for (let cx = cx0; cx <= cx1; cx++) occupied.add(cellKey(cx, cy));
      }
      placed = [dx, dy];
      break;
    }
    if (!placed) continue;

    const [dx, dy] = placed;
    const offset = Math.hypot(dx, dy);
    kept.push({
      item,
      // Unprojected rather than kept as a pixel offset, so the leader line has
      // a real end point and the text layer needs no special handling. Both
      // drift together while zooming and are recomputed when the view settles,
      // which is already how the selection itself works.
      position: offset ? viewport.unproject([x + dx, y + dy]) : item.position,
      offset,
    });
  }
  return kept;
}
