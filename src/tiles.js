// Tile loading and caching.
//
// What went wrong in the old version, and how this fixes it:
//
//  * "sometimes data disappears" - the old code kept one tile per zoom level in
//    a slot that the next pan overwrote. Here every tile lives in an LRU cache
//    and what gets drawn is the union of all cached tiles covering the view.
//  * "sometimes too much data was loaded" - the old code refetched on every
//    move. Here a tile is fetched at most once, requests that scroll out of
//    view are aborted, and a per-zoom index means a tile that does not exist is
//    never requested at all.
//
// Because the pyramid puts every item in exactly one tile, drawing zoom Z means
// unioning tiles for z = 0..Z: parents stay cached while panning, and nothing
// is ever drawn twice.
//
// Tiles come out of a packed file by byte range rather than one file each. Two
// things follow from that. Neighbouring tiles are adjacent in the pack, so a
// column of them is fetched in one request instead of eight. And the per-zoom
// index is itself a range in the pack, so it is fetched when a zoom is first
// visited rather than all thirteen at startup - which is most of what used to
// stand between opening the page and seeing a map.

import { decodeTile, decodeZoomIndex, maybeGunzip } from "./decode.js";
import { coalesce } from "./pack.js";

const MAX_CONCURRENT = 8;        // parallel range requests
const MAX_CACHED_TILES = 1200;
const MAX_CACHED_BYTES = 96e6;   // decoded; deep tiles are far bigger than the mean
const BOUNDS_PADDING = 0.1;      // fraction of the viewport, so panning has a head start

export function lonToTileX(lon, z) {
  return Math.floor(((lon + 180) / 360) * (1 << z));
}

export function latToTileY(lat, z) {
  const n = 1 << z;
  const clamped = Math.max(-85.05112878, Math.min(85.05112878, lat));
  const s = Math.sin((clamped * Math.PI) / 180);
  const y = Math.floor((0.5 - Math.log((1 + s) / (1 - s)) / (4 * Math.PI)) * n);
  return Math.max(0, Math.min(n - 1, y));
}

export class TileManager {
  constructor({ pack, manifest, onUpdate }) {
    this.pack = pack;
    this.maxZoom = manifest.maxZoom;
    this.countries = manifest.countries ?? [];
    this.zoomMeta = manifest.zooms ?? {};
    this.onUpdate = onUpdate;
    this.cache = new Map();      // "z/x/y" -> {items, bytes, used}
    this.indexes = new Map();    // z -> {keys, lengths, offsets, byKey}
    this.indexPending = new Map();
    this.runs = new Map();       // run id -> {controller, keys}
    this.needed = [];            // ordered list of keys currently in view
    this.queue = [];
    this.clock = 0;
    this.runId = 0;
    this.cachedBytes = 0;
  }

  // ----------------------------------------------------------------- indexes

  /** The directory for one zoom, fetched the first time that zoom is drawn. */
  ensureIndex(z) {
    if (this.indexes.has(z) || this.indexPending.has(z)) return;
    const meta = this.zoomMeta[String(z)];
    if (!meta) {
      this.indexes.set(z, null); // the build produced nothing at this zoom
      return;
    }
    const [offset, length] = meta.index;
    const promise = this.pack
      .read(offset, length)
      .then(maybeGunzip)
      .then((buffer) => {
        this.indexes.set(z, decodeZoomIndex(buffer, meta.base));
        this.indexPending.delete(z);
        // The view that asked for this index was told the zoom had no tiles,
        // so redrawing is not enough - the whole reconciliation has to run
        // again now that they can be found.
        this.refresh();
      })
      .catch((err) => {
        console.warn(`zoom ${z} index failed`, err);
        this.indexPending.delete(z);
      });
    this.indexPending.set(z, promise);
  }

  /** Every tile covering the viewport, from the world tile down to the current
   *  zoom, nearest-to-centre first. Zooms whose index has not arrived are
   *  skipped and requested; their tiles appear on the next update. */
  tilesForView(bounds, zoom) {
    const [minX, minY, maxX, maxY] = bounds;
    const padX = (maxX - minX) * BOUNDS_PADDING;
    const padY = (maxY - minY) * BOUNDS_PADDING;
    const west = minX - padX, east = maxX + padX;
    const south = minY - padY, north = maxY + padY;
    const centreX = (minX + maxX) / 2, centreY = (minY + maxY) / 2;

    const zMax = Math.min(Math.max(Math.floor(zoom), 0), this.maxZoom);
    const wanted = [];
    for (let z = 0; z <= zMax; z++) {
      const index = this.indexes.get(z);
      if (!index) {
        this.ensureIndex(z);
        continue;
      }
      const n = 1 << z;
      const yTop = latToTileY(north, z);
      const yBottom = latToTileY(south, z);
      let xLeft = lonToTileX(west, z);
      let xRight = lonToTileX(east, z);
      if (xRight - xLeft + 1 >= n) { xLeft = 0; xRight = n - 1; } // whole world in view
      const cx = lonToTileX(centreX, z), cy = latToTileY(centreY, z);
      for (let x = xLeft; x <= xRight; x++) {
        const wrapped = ((x % n) + n) % n; // antimeridian
        for (let y = yTop; y <= yBottom; y++) {
          const slot = index.byKey.get((((wrapped << 16) | y) >>> 0));
          if (slot === undefined) continue;
          wanted.push({
            z, x: wrapped, y, slot,
            key: `${z}/${wrapped}/${y}`,
            offset: index.offsets[slot],
            length: index.lengths[slot],
            dist: Math.abs(x - cx) + Math.abs(y - cy),
          });
        }
      }
    }
    return wanted;
  }

  /** Redo the last update, for when something other than the view changed. */
  refresh() {
    if (this.lastView) this.update(this.lastView.bounds, this.lastView.zoom);
    this.onUpdate?.();
  }

  /** Reconcile the in-flight work with what the current view needs. */
  update(bounds, zoom) {
    this.lastView = { bounds, zoom };
    const wanted = this.tilesForView(bounds, zoom);
    const wantedKeys = new Set(wanted.map((t) => t.key));
    this.needed = wanted.map((t) => t.key);

    for (const [id, run] of this.runs) {
      if (!run.keys.some((key) => wantedKeys.has(key))) {
        run.controller.abort();
        this.runs.delete(id);
      }
    }
    const inFlight = new Set();
    for (const run of this.runs.values()) for (const key of run.keys) inFlight.add(key);

    // Shallow tiles first: they carry the important labels and cover the most
    // ground, so the map fills in from the top down.
    const missing = wanted
      .filter((t) => !this.cache.has(t.key) && !inFlight.has(t.key))
      .sort((a, b) => a.z - b.z || a.dist - b.dist);

    // Coalesce within a zoom only. Runs across zooms would span the gap where
    // the previous zoom's index blob sits, and mixing them costs more than it
    // saves because shallow zooms are wanted first anyway.
    this.queue = [];
    for (let z = 0; z <= this.maxZoom; z++) {
      const atZoom = missing.filter((t) => t.z === z);
      if (!atZoom.length) continue;
      this.queue.push(
        ...coalesce(atZoom, { partAt: (o) => this.pack.partAt(o) })
      );
    }

    this.pump();
    this.evict(wantedKeys);
  }

  pump() {
    while (this.runs.size < MAX_CONCURRENT && this.queue.length) {
      const run = this.queue.shift();
      const id = ++this.runId;
      const controller = new AbortController();
      this.runs.set(id, { controller, keys: run.items.map((t) => t.key) });
      this.pack
        .read(run.start, run.length, controller.signal)
        .then(async (buffer) => {
          for (const tile of run.items) {
            const from = tile.offset - run.start;
            const slice = buffer.slice(from, from + tile.length);
            let decoded = null;
            try {
              decoded = decodeTile(await maybeGunzip(slice), this.countries);
            } catch (err) {
              console.warn("tile failed to decode", tile.key, err);
            }
            this.store(tile.key, decoded);
          }
          this.runs.delete(id);
          this.pump();
          this.onUpdate?.();
        })
        .catch((err) => {
          this.runs.delete(id);
          if (err.name !== "AbortError") {
            console.warn("range failed", run.start, run.length, err);
            // Remember the failure so a redraw does not hammer the same bytes.
            for (const tile of run.items) this.store(tile.key, null);
          }
          this.pump();
        });
    }
  }

  store(key, decoded) {
    const bytes = decoded ? decoded.bytes : 0;
    // Two runs should never carry the same tile, but the byte accounting is
    // what drives eviction, so do not let a repeat inflate it.
    this.cachedBytes -= this.cache.get(key)?.bytes ?? 0;
    this.cache.set(key, {
      items: decoded ? decoded.items : [],
      bytes,
      used: ++this.clock,
    });
    this.cachedBytes += bytes;
  }

  evict(keep) {
    if (this.cache.size <= MAX_CACHED_TILES && this.cachedBytes <= MAX_CACHED_BYTES) {
      return;
    }
    const victims = [...this.cache.entries()]
      .filter(([key]) => !keep.has(key))
      .sort((a, b) => a[1].used - b[1].used);
    for (const [key, tile] of victims) {
      if (this.cache.size <= MAX_CACHED_TILES && this.cachedBytes <= MAX_CACHED_BYTES) {
        break;
      }
      this.cache.delete(key);
      this.cachedBytes -= tile.bytes;
    }
  }

  /** Everything currently in view that has arrived. Missing tiles simply are
   *  not there yet - they never blank out what is already drawn. */
  visibleItems() {
    const out = [];
    for (const key of this.needed) {
      const tile = this.cache.get(key);
      if (!tile) continue;
      tile.used = ++this.clock;
      for (const item of tile.items) out.push(item);
    }
    return out;
  }

  stats() {
    let inFlight = 0;
    for (const run of this.runs.values()) inFlight += run.keys.length;
    const queued = this.queue.reduce((sum, run) => sum + run.items.length, 0);
    return {
      cached: this.cache.size,
      pending: inFlight,
      queued,
      needed: this.needed.length,
      megabytes: this.pack.bytes / 1e6,
      requests: this.pack.requests,
    };
  }
}
