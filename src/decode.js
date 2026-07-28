// Binary tile decoder for the WMT2 format written by pipeline/build_tiles.py.
//
// Layout (little-endian, every numeric array 4-byte aligned so the views below
// are zero-copy):
//   0   "WMT2", uint16 version, uint16 z
//   8   uint32 x, uint32 y, uint32 count
//   20  uint32 titleBytes, wikiBytes, descrBytes
//   32  uint32 adminCount, adminBytes, reserved, reserved
//   48  qid u32[n], titleOff u32[n+1], wikiOff u32[n+1], descrOff u32[n+1],
//       lon f32[n], lat f32[n], population u32[n],
//       score u16[n], pr u16[n], qr u16[n], country u16[n], admin u16[n],
//       elevation i16[n], year i16[n],                            (pad to 4)
//       cat u8[n], sub u8[n], flags u8[n], sitelinks u8[n],
//       adminOff u32[adminCount+1], adminBlob, titles, wikis, descrs
//
// Items with a non-zero location source are drawn at somebody else's
// coordinate - a person at their birthplace, a painting at its museum. The
// decoder surfaces that as `locSrc`/`derived` so nothing downstream can
// accidentally present a borrowed point as the item's own position.

const MAGIC = 0x32544d57; // "WMT2" read as a little-endian uint32
const FLAG_HAS_WIKI = 1;
const FLAG_HAS_IMAGE = 2;
const FLAG_HAS_WEBSITE = 4;
// Bits 3-5: why the item is at this coordinate. 0 means it has its own; any
// other value means the coordinate was borrowed from the place named in
// `admin`, and manifest.locSources[locSrc] is the phrase that says which way.
const FLAG_LOC_SHIFT = 3;
const FLAG_LOC_MASK = 0b111;

const NO_POP = 0xffffffff;
const NO_REF = 0xffff;
const NO_INT16 = -32768;

const decoder = new TextDecoder("utf-8");

/** Gunzip unless the host already did it via Content-Encoding. */
export async function maybeGunzip(buffer) {
  const head = new Uint8Array(buffer, 0, Math.min(2, buffer.byteLength));
  if (head.length < 2 || head[0] !== 0x1f || head[1] !== 0x8b) return buffer;
  const stream = new Blob([buffer]).stream().pipeThrough(new DecompressionStream("gzip"));
  return await new Response(stream).arrayBuffer();
}

/** Decode one tile. `countries` is the shared table from manifest.json. */
export function decodeTile(buffer, countries = []) {
  const view = new DataView(buffer);
  if (view.getUint32(0, true) !== MAGIC) throw new Error("not a WMT2 tile");

  const z = view.getUint16(6, true);
  const x = view.getUint32(8, true);
  const y = view.getUint32(12, true);
  const n = view.getUint32(16, true);
  const titleBytes = view.getUint32(20, true);
  const wikiBytes = view.getUint32(24, true);
  const descrBytes = view.getUint32(28, true);
  const adminCount = view.getUint32(32, true);
  const adminBytes = view.getUint32(36, true);

  let o = 48;
  const qid = new Uint32Array(buffer, o, n); o += 4 * n;
  const titleOff = new Uint32Array(buffer, o, n + 1); o += 4 * (n + 1);
  const wikiOff = new Uint32Array(buffer, o, n + 1); o += 4 * (n + 1);
  const descrOff = new Uint32Array(buffer, o, n + 1); o += 4 * (n + 1);
  const lon = new Float32Array(buffer, o, n); o += 4 * n;
  const lat = new Float32Array(buffer, o, n); o += 4 * n;
  const pop = new Uint32Array(buffer, o, n); o += 4 * n;
  const score = new Uint16Array(buffer, o, n); o += 2 * n;
  const pr = new Uint16Array(buffer, o, n); o += 2 * n;
  const qr = new Uint16Array(buffer, o, n); o += 2 * n;
  const country = new Uint16Array(buffer, o, n); o += 2 * n;
  const admin = new Uint16Array(buffer, o, n); o += 2 * n;
  const elev = new Int16Array(buffer, o, n); o += 2 * n;
  const year = new Int16Array(buffer, o, n); o += 2 * n;
  o += (4 - ((14 * n) % 4)) % 4;
  const cat = new Uint8Array(buffer, o, n); o += n;
  const sub = new Uint8Array(buffer, o, n); o += n;
  const flags = new Uint8Array(buffer, o, n); o += n;
  const sitelinks = new Uint8Array(buffer, o, n); o += n;
  const adminOff = new Uint32Array(buffer, o, adminCount + 1); o += 4 * (adminCount + 1);
  const adminBlob = new Uint8Array(buffer, o, adminBytes); o += adminBytes;
  const titleBlob = new Uint8Array(buffer, o, titleBytes); o += titleBytes;
  const wikiBlob = new Uint8Array(buffer, o, wikiBytes); o += wikiBytes;
  const descrBlob = new Uint8Array(buffer, o, descrBytes);

  // A tile holds a few dozen distinct admin areas at most, so the table is
  // decoded once and every row that needs it gets the same string reference.
  const adminNames = new Array(adminCount);
  for (let i = 0; i < adminCount; i++) {
    adminNames[i] = decoder.decode(adminBlob.subarray(adminOff[i], adminOff[i + 1]));
  }

  // Tiles hold a few hundred rows, so plain objects here cost nothing and keep
  // the rest of the app (deck.gl accessors, tooltips, filters) straightforward.
  const items = new Array(n);
  for (let i = 0; i < n; i++) {
    const title = decoder.decode(titleBlob.subarray(titleOff[i], titleOff[i + 1]));
    const wikiRaw = decoder.decode(wikiBlob.subarray(wikiOff[i], wikiOff[i + 1]));
    // "lang|title"; an empty title part means it equals the drawn label, which
    // is how the encoder avoids storing the same string twice.
    let wikiLang = null;
    let wiki = null;
    if (flags[i] & FLAG_HAS_WIKI) {
      const bar = wikiRaw.indexOf("|");
      wikiLang = bar < 0 ? "en" : wikiRaw.slice(0, bar);
      const rest = bar < 0 ? wikiRaw : wikiRaw.slice(bar + 1);
      wiki = rest || title;
    }
    items[i] = {
      qid: qid[i],
      position: [lon[i], lat[i]],
      title,
      wiki,
      wikiLang,
      descr: descrOff[i + 1] > descrOff[i]
        ? decoder.decode(descrBlob.subarray(descrOff[i], descrOff[i + 1]))
        : "",
      hasImage: !!(flags[i] & FLAG_HAS_IMAGE),
      hasWebsite: !!(flags[i] & FLAG_HAS_WEBSITE),
      locSrc: (flags[i] >> FLAG_LOC_SHIFT) & FLAG_LOC_MASK,
      derived: ((flags[i] >> FLAG_LOC_SHIFT) & FLAG_LOC_MASK) !== 0,
      score: score[i] / 65535,
      pr: pr[i] / 65535,
      qr: qr[i] / 65535,
      // Sentinels become null so callers can test one thing, not two.
      pop: pop[i] === NO_POP ? null : pop[i],
      elev: elev[i] === NO_INT16 ? null : elev[i],
      year: year[i] === NO_INT16 ? null : year[i],
      sitelinks: sitelinks[i],
      country: country[i] === NO_REF ? null : countries[country[i]] ?? null,
      admin: admin[i] === NO_REF ? null : adminNames[admin[i]] ?? null,
      cat: cat[i],
      sub: sub[i],
    };
  }
  return { z, x, y, items, bytes: buffer.byteLength };
}

/**
 * Per-zoom directory: uint32 n, uint32 key[n], uint32 length[n].
 *
 * The pack stores this zoom's tiles back to back in key order, so an offset is
 * the zoom's base plus the running sum of the lengths before it. Storing the
 * sums instead would double a file the client downloads for every zoom it
 * visits.
 */
export function decodeZoomIndex(buffer, base) {
  const n = new Uint32Array(buffer, 0, 1)[0];
  const keys = new Uint32Array(buffer, 4, n);
  const lengths = new Uint32Array(buffer, 4 + 4 * n, n);
  // Float64 because the pack can exceed 4 GB long before the tile count does.
  const offsets = new Float64Array(n);
  const byKey = new Map();
  let running = base;
  for (let i = 0; i < n; i++) {
    offsets[i] = running;
    running += lengths[i];
    byKey.set(keys[i], i);
  }
  return { keys, lengths, offsets, byKey, count: n };
}
