// Prefix search against the packed shards built by pipeline/build_search.py.
//
// Three range requests at most, and usually one. The root file says where each
// first character's directory lives; the directory says where each of that
// letter's shards lives; the shard is a gzipped list of entries. Directories
// are cached, so after the first "b" every b-query is a single request for a
// few kilobytes.
//
// The index only holds items above an importance floor (see build_search.py),
// so a search that finds nothing usually means the thing is too obscure to be
// worth a row rather than that the query was wrong - which is what the status
// line says.

import { Pack } from "./pack.js";
import { maybeGunzip } from "./decode.js";

const MAX_RESULTS = 12;

const decoder = new TextDecoder("utf-8");

/** Must match normalise() in pipeline/build_search.py. */
export function normalise(text) {
  return text
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "") // combining marks left by NFKD
    .replace(/[^\p{L}\p{N}\s]/gu, "")
    .trim();
}

/** uint32 count, uint32 prefixOff[count+1], uint32 length[count], utf8 names. */
export function decodeDirectory(buffer, base) {
  const count = new Uint32Array(buffer, 0, 1)[0];
  const nameOff = new Uint32Array(buffer, 4, count + 1);
  const lengths = new Uint32Array(buffer, 4 + 4 * (count + 1), count);
  const names = new Uint8Array(buffer, 4 + 4 * (count + 1) + 4 * count);
  const map = new Map();
  let running = base;
  for (let i = 0; i < count; i++) {
    map.set(decoder.decode(names.subarray(nameOff[i], nameOff[i + 1])), [
      running,
      lengths[i],
    ]);
    running += lengths[i];
  }
  return map;
}

export class Search {
  constructor({ baseUrl, root, categories, countries, input, results, onPick }) {
    this.pack = new Pack({ baseUrl, info: root.pack });
    this.letters = root.letters ?? {};
    this.rare = root.rare ?? null;
    this.minScore = root.minScore ?? 0;
    this.categories = categories;
    this.countries = countries ?? [];
    this.input = input;
    this.results = results;
    this.onPick = onPick;
    this.directories = new Map(); // letter -> Promise<Map>
    this.shards = new Map();      // "off:len" -> Promise<entries>
    this.token = 0;
    this.active = -1;
    this.current = [];
    this.wire();
  }

  wire() {
    let timer = null;
    this.input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => this.run(this.input.value), 120);
    });
    this.input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        const step = event.key === "ArrowDown" ? 1 : -1;
        this.active = Math.max(
          0,
          Math.min(this.current.length - 1, this.active + step)
        );
        this.paint();
      } else if (event.key === "Enter") {
        const pick = this.current[Math.max(this.active, 0)];
        if (pick) this.choose(pick);
      } else if (event.key === "Escape") {
        this.close();
        this.input.blur();
      }
    });
    document.addEventListener("click", (event) => {
      if (!this.results.contains(event.target) && event.target !== this.input) {
        this.close();
      }
    });
  }

  /** prefix -> [offset, length], for every shard under one first character. */
  directoryFor(letter) {
    const known = Object.prototype.hasOwnProperty.call(this.letters, letter);
    const key = known ? letter : this.rare;
    if (key === null || !Object.prototype.hasOwnProperty.call(this.letters, key)) {
      return Promise.resolve(null);
    }
    if (!this.directories.has(key)) {
      const [base, offset, length] = this.letters[key];
      this.directories.set(
        key,
        this.pack
          .read(offset, length)
          .then(maybeGunzip)
          .then((buffer) => decodeDirectory(buffer, base))
          .catch(() => null)
      );
    }
    return this.directories.get(key);
  }

  async shardFor(query) {
    const directory = await this.directoryFor(query[0]);
    if (!directory) return [];
    for (const level of [3, 2, 1]) {
      if (query.length < level) continue;
      const where = directory.get(query.slice(0, level));
      if (!where) continue;
      const key = `${where[0]}:${where[1]}`;
      if (!this.shards.has(key)) {
        this.shards.set(
          key,
          this.pack
            .read(where[0], where[1])
            .then(maybeGunzip)
            .then((buffer) => JSON.parse(decoder.decode(new Uint8Array(buffer))))
            .catch(() => [])
        );
      }
      return await this.shards.get(key);
    }
    return [];
  }

  async run(raw) {
    const query = normalise(raw);
    if (query.length < 1) return this.close();
    const token = ++this.token;
    const entries = await this.shardFor(query);
    if (token !== this.token) return; // a later keystroke already won

    const scored = [];
    for (const entry of entries) {
      const name = normalise(entry[0]);
      let rank;
      if (name.startsWith(query)) rank = 0;
      else if (name.split(" ").some((w) => w.startsWith(query))) rank = 1;
      else if (name.includes(query)) rank = 2;
      else continue;
      scored.push({ entry, rank });
    }
    scored.sort((a, b) => a.rank - b.rank || b.entry[4] - a.entry[4]);
    this.current = scored.slice(0, MAX_RESULTS).map((s) => s.entry);
    this.active = this.current.length ? 0 : -1;
    this.paint();
  }

  paint() {
    this.results.textContent = "";
    if (!this.current.length) return this.close();
    this.current.forEach((entry, i) => {
      const [name, , , , , cat, country] = entry;
      const row = document.createElement("div");
      row.className = "result";
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(i === this.active));

      const swatch = document.createElement("span");
      swatch.className = "swatch";
      swatch.style.background = this.categories.style?.palette?.[cat] ?? "transparent";

      const label = document.createElement("span");
      label.className = "name";
      label.textContent = name;

      const kind = document.createElement("span");
      kind.className = "cat";
      // Two Springfields look identical without the country.
      kind.textContent =
        this.countries[country] ?? this.categories.categories[cat]?.name ?? "";

      row.append(swatch, label, kind);
      row.addEventListener("mousedown", (event) => {
        event.preventDefault();
        this.choose(entry);
      });
      this.results.append(row);
    });
    this.results.classList.add("open");
  }

  choose(entry) {
    const [name, lon, lat, qid, score, cat] = entry;
    this.close();
    this.input.value = name;
    this.onPick({ name, lon, lat, qid, score, cat });
  }

  close() {
    this.results.classList.remove("open");
    this.results.textContent = "";
    this.current = [];
    this.active = -1;
  }
}
