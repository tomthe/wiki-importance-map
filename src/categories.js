// Category / subcategory filter: builds the panel and answers "draw this item?".

export function hexToRgb(hex) {
  const h = hex.replace("#", "");
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
  ];
}

const SUB_STRIDE = 256;

// ------------------------------------------------------------ URL encoding
//
// A selection is half of what a shared link has to carry - the other half is
// where the map is looking - so it is written with characters a URL fragment
// leaves alone: `.` between categories, `~` for "only these subcategories",
// `!` for "all but these", `3-7` for a run. Nothing here ever needs escaping,
// which is what keeps the hash readable in a chat window.
//
//   cat=all                 everything
//   cat=none                nothing
//   cat=0.2.9               those three categories, all of their subcategories
//   cat=0~1,3-5.9!0         Other: only subs 1 and 3-5; People: all but sub 0
//
// Ids rather than names because the hash is already the longest thing on the
// URL bar, and because a name would have to be escaped the moment somebody
// renames a category to something with a space in it.

/** Sorted indices -> "1,3-7,9". Runs collapse, which is the common case: most
 *  partial selections are "all of them except the one I just unticked". */
function encodeList(indices) {
  const runs = [];
  for (const i of indices) {
    const last = runs[runs.length - 1];
    if (last && i === last[1] + 1) last[1] = i;
    else runs.push([i, i]);
  }
  return runs
    .map(([a, b]) => (a === b ? `${a}` : b === a + 1 ? `${a},${b}` : `${a}-${b}`))
    .join(",");
}

/** "1,3-7" -> [1,3,4,5,6,7]; null if it is not that shape. Indices at or past
 *  `limit` are dropped rather than rejected, so a link made before a category
 *  gained or lost a subcategory still resolves to something sensible. */
function parseList(text, limit) {
  if (text === "") return [];
  const out = [];
  for (const piece of text.split(",")) {
    const match = /^(\d+)(?:-(\d+))?$/.exec(piece);
    if (!match) return null;
    const from = Number(match[1]);
    const to = match[2] === undefined ? from : Number(match[2]);
    if (to < from || to - from > SUB_STRIDE) return null;
    for (let i = from; i <= to; i++) if (i < limit) out.push(i);
  }
  return out;
}

/** The whole filter state as one URL-safe token. */
export function encodeSelection(categories, catOn, subOn) {
  const parts = [];
  let everyCategoryFull = true;
  let anythingOn = false;
  for (const cat of categories) {
    const count = cat.subcategories.length;
    const on = catOn.get(cat.id)
      ? [...subOn.get(cat.id)].filter((i) => i < count).sort((a, b) => a - b)
      : [];
    if (on.length === 0) {
      everyCategoryFull = false;
      continue;
    }
    anythingOn = true;
    if (on.length === count) {
      parts.push(String(cat.id));
      continue;
    }
    everyCategoryFull = false;
    const chosen = new Set(on);
    const off = [];
    for (let i = 0; i < count; i++) if (!chosen.has(i)) off.push(i);
    const only = `${cat.id}~${encodeList(on)}`;
    const except = `${cat.id}!${encodeList(off)}`;
    parts.push(except.length < only.length ? except : only);
  }
  if (!anythingOn) return "none";
  if (everyCategoryFull) return "all";
  return parts.join(".");
}

/** Inverse of encodeSelection. Returns `{ catOn, subOn }`, or null when the
 *  token says nothing this manifest recognises - a caller that gets null should
 *  keep the default selection rather than draw an empty map. */
export function parseSelection(text, categories) {
  if (!text) return null;
  const catOn = new Map(categories.map((c) => [c.id, false]));
  const subOn = new Map(categories.map((c) => [c.id, new Set()]));
  const turnOn = (cat) => {
    catOn.set(cat.id, true);
    const set = subOn.get(cat.id);
    cat.subcategories.forEach((_, i) => set.add(i));
  };
  if (text === "none") return { catOn, subOn };
  if (text === "all") {
    for (const cat of categories) turnOn(cat);
    return { catOn, subOn };
  }
  const byId = new Map(categories.map((c) => [c.id, c]));
  let understood = false;
  for (const part of text.split(".")) {
    const match = /^(\d+)(?:([~!])([\d,-]*))?$/.exec(part);
    if (!match) continue;
    const cat = byId.get(Number(match[1]));
    if (!cat) continue;
    if (!match[2]) {
      turnOn(cat);
      understood = true;
      continue;
    }
    const listed = parseList(match[3], cat.subcategories.length);
    if (!listed) continue;
    const set = subOn.get(cat.id);
    if (match[2] === "~") {
      for (const i of listed) set.add(i);
    } else {
      const off = new Set(listed);
      cat.subcategories.forEach((_, i) => {
        if (!off.has(i)) set.add(i);
      });
    }
    catOn.set(cat.id, set.size > 0);
    if (set.size > 0) understood = true;
  }
  return understood ? { catOn, subOn } : null;
}

export class CategoryFilter {
  constructor({ manifest, container, onChange, defaultOff = [] }) {
    this.categories = manifest.categories;
    this.container = container;
    this.onChange = onChange;
    // Flat lookup keyed by cat*256+sub: filtering runs over every visible item
    // on every render, so it should not walk Sets. render() fills it.
    this.lookup = new Uint8Array(this.categories.length * SUB_STRIDE);
    const off = new Set(defaultOff);
    this.catOn = new Map(this.categories.map((c) => [c.id, !off.has(c.name)]));
    this.subOn = new Map(
      this.categories.map((c) => [
        c.id,
        new Set(off.has(c.name) ? [] : c.subcategories.map((_, i) => i)),
      ])
    );
    this.countEls = new Map();
    this.render();
    // What the site starts with. A shared link carries a selection only when it
    // differs from this, so a plain `#zoom/lat/lon` link keeps meaning "however
    // the map opens" even if the defaults are changed later.
    this.defaultEncoded = this.encode();
  }

  accept(item) {
    return this.lookup[item.cat * SUB_STRIDE + item.sub] === 1;
  }

  encode() {
    return encodeSelection(this.categories, this.catOn, this.subOn);
  }

  /** The token for the URL, or null when nothing needs saying. */
  urlValue() {
    const encoded = this.encode();
    return encoded === this.defaultEncoded ? null : encoded;
  }

  /** Apply an encoded selection. Returns false and changes nothing if the token
   *  is unusable. Does not fire onChange: the caller is the one that knows
   *  whether a redraw is already coming. */
  apply(text) {
    const parsed = parseSelection(text, this.categories);
    if (!parsed) return false;
    for (const cat of this.categories) {
      this.catOn.set(cat.id, parsed.catOn.get(cat.id));
      const set = this.subOn.get(cat.id);
      set.clear();
      for (const sub of parsed.subOn.get(cat.id)) set.add(sub);
    }
    this.rebuildLookup();
    this.syncInputs();
    return true;
  }

  rebuildLookup() {
    this.lookup.fill(0);
    for (const cat of this.categories) {
      if (!this.catOn.get(cat.id)) continue;
      const subs = this.subOn.get(cat.id);
      for (const sub of subs) this.lookup[cat.id * SUB_STRIDE + sub] = 1;
      // Items can carry a subcategory id beyond the named list only if the
      // manifest and tiles disagree; be permissive rather than silently blank.
      for (let s = cat.subcategories.length; s < SUB_STRIDE; s++) {
        this.lookup[cat.id * SUB_STRIDE + s] = subs.has(0) ? 1 : 0;
      }
    }
  }

  setAll(on) {
    for (const cat of this.categories) {
      this.catOn.set(cat.id, on);
      const subs = this.subOn.get(cat.id);
      subs.clear();
      if (on) cat.subcategories.forEach((_, i) => subs.add(i));
    }
    this.rebuildLookup();
    this.syncInputs();
    this.onChange?.();
  }

  syncInputs() {
    for (const input of this.container.querySelectorAll("input[data-cat]")) {
      const catId = Number(input.dataset.cat);
      if (input.dataset.sub === undefined) {
        input.checked = this.catOn.get(catId);
      } else {
        input.checked = this.subOn.get(catId).has(Number(input.dataset.sub));
      }
    }
  }

  /** Show how many of the items on screen fall in each category. */
  updateCounts(items) {
    const counts = new Array(this.categories.length).fill(0);
    for (const item of items) counts[item.cat] = (counts[item.cat] || 0) + 1;
    for (const [catId, el] of this.countEls) {
      el.textContent = counts[catId] ? counts[catId].toLocaleString() : "";
    }
  }

  setPalette(colors) {
    this.colors = colors;
    for (const [catId, swatch] of this.swatches ?? []) {
      swatch.style.background = colors[catId];
    }
  }

  render() {
    this.container.textContent = "";
    this.swatches = new Map();
    for (const cat of this.categories) {
      const row = document.createElement("div");
      row.className = "cat-row";

      const twisty = document.createElement("button");
      twisty.className = "twisty";
      twisty.type = "button";
      twisty.textContent = "▶";
      twisty.title = "Show subcategories";

      const label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.checked = this.catOn.get(cat.id);
      box.dataset.cat = String(cat.id);

      const swatch = document.createElement("span");
      swatch.className = "swatch";
      this.swatches.set(cat.id, swatch);

      const name = document.createElement("span");
      name.className = "name";
      name.textContent = cat.name;

      const count = document.createElement("span");
      count.className = "count";
      this.countEls.set(cat.id, count);

      label.append(box, swatch, name, count);
      row.append(twisty, label);

      const subs = document.createElement("div");
      subs.className = "subs";
      cat.subcategories.forEach((subName, subId) => {
        const subLabel = document.createElement("label");
        const subBox = document.createElement("input");
        subBox.type = "checkbox";
        subBox.checked = this.subOn.get(cat.id).has(subId);
        subBox.dataset.cat = String(cat.id);
        subBox.dataset.sub = String(subId);
        const text = document.createElement("span");
        text.textContent = subName || "Other";
        subLabel.append(subBox, text);
        subs.append(subLabel);

        subBox.addEventListener("change", () => {
          const set = this.subOn.get(cat.id);
          if (subBox.checked) set.add(subId);
          else set.delete(subId);
          // Turning any subcategory on implies the category itself is on.
          this.catOn.set(cat.id, set.size > 0);
          box.checked = set.size > 0;
          this.rebuildLookup();
          this.onChange?.();
        });
      });

      twisty.addEventListener("click", () => {
        const open = subs.classList.toggle("open");
        twisty.textContent = open ? "▼" : "▶";
      });

      box.addEventListener("change", () => {
        this.catOn.set(cat.id, box.checked);
        const set = this.subOn.get(cat.id);
        set.clear();
        if (box.checked) cat.subcategories.forEach((_, i) => set.add(i));
        this.rebuildLookup();
        this.syncInputs();
        this.onChange?.();
      });

      this.container.append(row, subs);
    }
    this.rebuildLookup();
  }
}
