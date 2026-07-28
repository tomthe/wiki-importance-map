// Round trip the category selection through its URL form.
//
//   node tests/hash_check.mjs
//
// The selection is the half of a shared link that a screenshot cannot show, and
// it is encoded and decoded in the same file, so the checks worth having are the
// ones about links that were *not* made by this build: a token from an older
// manifest, a token somebody edited by hand, a token from the future.

import { encodeSelection, parseSelection } from "../src/categories.js";

const CATEGORIES = [
  { id: 0, name: "Other", subcategories: ["Other", "Battle", "Siege", "War"] },
  { id: 1, name: "Settlements", subcategories: ["Other", "Capital", "City"] },
  { id: 2, name: "Nature", subcategories: ["Other", "Mountain"] },
  {
    id: 3,
    name: "People",
    // Wide enough that a mask-style encoding would have needed more than one
    // machine word, which is why this one is a list of runs instead.
    subcategories: Array.from({ length: 34 }, (_, i) => `Job ${i}`),
  },
];

let failures = 0;
const check = (name, ok, detail = "") => {
  console.log(`${ok ? "ok  " : "FAIL"} ${name}${ok ? "" : "  " + detail}`);
  if (!ok) failures++;
};

/** A selection as a plain object: { catId: [subs] }, categories left out are off. */
function make(spec) {
  const catOn = new Map(CATEGORIES.map((c) => [c.id, false]));
  const subOn = new Map(CATEGORIES.map((c) => [c.id, new Set()]));
  for (const [id, subs] of Object.entries(spec)) {
    catOn.set(Number(id), subs.length > 0);
    subOn.set(Number(id), new Set(subs));
  }
  return { catOn, subOn };
}

const all = (cat) => cat.subcategories.map((_, i) => i);

function dump(state) {
  return CATEGORIES.map((cat) =>
    state.catOn.get(cat.id) ? [...state.subOn.get(cat.id)].sort((a, b) => a - b) : []
  );
}

const encode = (spec) => encodeSelection(CATEGORIES, make(spec).catOn, make(spec).subOn);

/** encode -> parse -> encode has to be a fixed point, and the parsed state has
 *  to be the state we started from. */
function roundTrip(name, spec, expected) {
  const token = encode(spec);
  check(`${name} encodes to ${JSON.stringify(expected)}`, token === expected, token);
  const parsed = parseSelection(token, CATEGORIES);
  check(
    `${name} survives the round trip`,
    parsed !== null && JSON.stringify(dump(parsed)) === JSON.stringify(dump(make(spec))),
    JSON.stringify(parsed && dump(parsed))
  );
}

// --- the shapes a selection can take ----------------------------------------
roundTrip("everything on", Object.fromEntries(CATEGORIES.map((c) => [c.id, all(c)])), "all");
roundTrip("everything off", {}, "none");
roundTrip(
  "whole categories",
  { 0: all(CATEGORIES[0]), 2: all(CATEGORIES[2]) },
  "0.2"
);
roundTrip(
  // The war map: one subcategory of one category.
  "a single subcategory",
  { 0: [1] },
  "0~1"
);
// Whichever of "only these" and "all but these" is shorter wins, so a run is
// only spelled out when the complement would be longer: three of thirty-four.
roundTrip("a run of subcategories", { 3: [1, 2, 3] }, "3~1-3");
roundTrip("three of four is written as the one that is off", { 0: [1, 2, 3] }, "0!0");
roundTrip(
  // Unticking one box should not spell out the other thirty-three.
  "all but one subcategory",
  { 3: all(CATEGORIES[3]).filter((i) => i !== 7) },
  "3!7"
);
roundTrip(
  "a mixed selection",
  { 0: [1, 3], 1: all(CATEGORIES[1]), 3: [0, 33] },
  "0~1,3.1.3~0,33"
);

// Two adjacent indices are written as a pair, not a range: "1,2" is the same
// length as "1-2" and reads as what it is.
check("adjacent pairs are not written as ranges", encode({ 0: [1, 2] }) === "0~1,2");

// --- links this build did not write -----------------------------------------
const cases = [
  ["the default token is not special-cased", "0.1.2.3", "all"],
  ["whitespace-free hand editing", "3", "3"],
  ["an out-of-range subcategory is dropped", "0~1,99", "0~1"],
  ["a category id from a newer manifest is ignored", "0~1.77", "0~1"],
  ["a range past the end is clipped", "2~0-9", "2"],
  ["an inverted list keeps the rest of the token", "0~3-1.2", "2"],
  ["an empty exclusion is the whole category", "1!", "1"],
  ["an empty inclusion turns the category off", "1~.2", "2"],
  ["garbage segments are skipped", "0~1.settlements.-.2", "0~1.2"],
];
for (const [name, token, expected] of cases) {
  const parsed = parseSelection(token, CATEGORIES);
  const again = parsed && encodeSelection(CATEGORIES, parsed.catOn, parsed.subOn);
  check(`${name} (${token} -> ${expected})`, again === expected, String(again));
}

// A token that says nothing this manifest recognises must not resolve to an
// empty map - the caller keeps its defaults instead, so a stale link degrades to
// "the site as it opens" rather than to a blank screen.
for (const token of ["", "99", "banana", "9~1", undefined, null]) {
  check(
    `an unusable token is rejected: ${JSON.stringify(token)}`,
    parseSelection(token, CATEGORIES) === null
  );
}
// ...but an explicit "nothing" is not unusable, it is a choice.
check(
  "'none' parses to an empty selection",
  JSON.stringify(dump(parseSelection("none", CATEGORIES))) ===
    JSON.stringify([[], [], [], []])
);

console.log(failures ? `\n${failures} failure(s)` : "\ncategory links round trip");
process.exit(failures ? 1 : 0);
