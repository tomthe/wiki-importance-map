// Reading blobs out of the packed data files with HTTP Range requests.
//
// The tiles and the search index each live in a handful of big files rather
// than a hundred thousand small ones (see pipeline/packfile.py for why). A
// blob is addressed by a global byte offset over the concatenation of the
// parts; this resolves that to a part and asks for those bytes.
//
// Every host worth deploying to honours Range - GitHub Pages, Cloudflare,
// nginx, Apache. Python's own http.server does not, which is the single
// reason tools/serve.py exists, and why probe() runs before anything else:
// without it the first tile request would quietly start downloading a 90 MB
// part file instead of 3 KB.

export class Pack {
  constructor({ baseUrl, info }) {
    this.baseUrl = baseUrl;
    this.stem = info.stem;
    this.sizes = info.parts;
    this.total = info.bytes;
    this.starts = [];
    let running = 0;
    for (const size of this.sizes) {
      this.starts.push(running);
      running += size;
    }
    this.requests = 0;
    this.bytes = 0;
  }

  urlOf(part) {
    // Must match PackWriter._part_path, including the .bin - see the note there
    // about Cloudflare caching by extension.
    return `${this.baseUrl}/${this.stem}.${String(part).padStart(3, "0")}.bin`;
  }

  /** Which part holds this offset. Linear: there are single digits of them. */
  partAt(offset) {
    for (let i = this.starts.length - 1; i >= 0; i--) {
      if (offset >= this.starts[i]) return i;
    }
    return 0;
  }

  /** Cheap check that the host serves ranges, before we rely on it. */
  async probe() {
    const controller = new AbortController();
    let response;
    try {
      response = await fetch(this.urlOf(0), {
        headers: { Range: "bytes=0-3" },
        signal: controller.signal,
      });
    } catch (err) {
      throw new Error(`cannot reach ${this.urlOf(0)} - ${err.message}`);
    }
    if (response.status === 206) {
      // Drain it. Four bytes, but an unread body leaves the request hanging
      // and the browser logs it as an aborted load.
      await response.arrayBuffer();
      return true;
    }
    // Do not let a whole part stream in behind our back.
    controller.abort();
    if (response.status === 404) {
      throw new Error(`${this.urlOf(0)} not found - run the pipeline first`);
    }
    throw new Error(
      `this server ignores HTTP Range (got ${response.status} for a 4-byte ` +
        `request). Python's http.server cannot serve this map; use ` +
        `"python tools/serve.py" instead.`
    );
  }

  /** One blob, or one coalesced run of them. */
  async read(offset, length, signal) {
    const part = this.partAt(offset);
    const local = offset - this.starts[part];
    const response = await fetch(this.urlOf(part), {
      signal,
      headers: { Range: `bytes=${local}-${local + length - 1}` },
    });
    if (response.status !== 206 && response.status !== 200) {
      throw new Error(`${response.status} for ${this.stem} part ${part}`);
    }
    const buffer = await response.arrayBuffer();
    this.requests += 1;
    this.bytes += buffer.byteLength;
    if (buffer.byteLength !== length) {
      // A 200 means the host ignored the header and sent everything; probe()
      // should have caught it, but a proxy can change its mind.
      if (response.status === 200) {
        return buffer.slice(local, local + length);
      }
      throw new Error(
        `short read: asked for ${length} bytes, got ${buffer.byteLength}`
      );
    }
    return buffer;
  }
}

/**
 * Merge blob reads that sit close together into single requests.
 *
 * Tiles are packed in (zoom, x, y) order, so a viewport's worth of them is
 * mostly contiguous: a column of eight tiles is one request, not eight. Gaps
 * are worth paying for up to a point - `gap` bytes of somebody else's tile is
 * cheaper than a second round trip.
 *
 * @param items  [{offset, length, ...}], any order
 * @returns      [{start, length, items:[...]}] sorted by offset
 */
export function coalesce(items, { gap = 24 * 1024, maxRun = 1 << 21, partAt } = {}) {
  const sorted = [...items].sort((a, b) => a.offset - b.offset);
  const runs = [];
  let run = null;
  for (const item of sorted) {
    const end = item.offset + item.length;
    if (
      run &&
      item.offset - run.end <= gap &&
      end - run.start <= maxRun &&
      // A run must live inside one part, or it is two requests anyway.
      (!partAt || partAt(item.offset) === run.part)
    ) {
      run.end = Math.max(run.end, end);
      run.items.push(item);
      continue;
    }
    run = {
      start: item.offset,
      end,
      part: partAt ? partAt(item.offset) : 0,
      items: [item],
    };
    runs.push(run);
  }
  return runs.map((r) => ({ start: r.start, length: r.end - r.start, items: r.items }));
}
