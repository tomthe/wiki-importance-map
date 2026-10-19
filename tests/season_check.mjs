// The travel map's monthly importance, in src/site.js, against the definition
// in pipeline/voyage/build_master.py:
//
//   s   = log10(1 + V) / scale                      the year
//   s_m = log10(1 + 12 * V * share_m) / scale       one month, annualised
//
// The tile only carries s (as `pr`) and twelve bytes of monthly share, so the
// browser has to rebuild V and s_m from those - an error there would make every
// month a slightly different map from the one the pipeline placed tiles for.
//
//   node tests/season_check.mjs

import { siteFor, monthShares } from "../src/site.js";

const scale = 5.5;
const manifest = {
  importanceScale: scale,
  categories: [{ name: "Practical" }, { name: "Destination" }, { name: "See" }],
  statuses: ["", "outline", "usable", "guide", "star"],
};
const site = siteFor("voyage", manifest);
let failures = 0;
const check = (name, ok, detail = "") => {
  console.log((ok ? "ok   " : "FAIL ") + name + (ok ? "" : `: ${detail}`));
  if (!ok) failures++;
};
const close = (a, b, eps = 1e-3) => Math.abs(a - b) < eps;

const item = (views, months) => {
  const peak = Math.max(...months);
  return {
    cat: 1,
    pr: Math.log10(1 + views) / scale,
    extra: [...months.map((m) => Math.round((m * 255) / peak)), 3],
  };
};
let key = 0;
const style = (month, seasonWeight = 0) => ({
  month,
  seasonWeight,
  seasonKey: `${++key}`,
});

const flat = item(100000, Array(12).fill(1));
check(
  "a flat year scores the same in every month as for the year",
  [1, 4, 7, 12].every((m) => close(site.importance(flat, style(m)), flat.pr)),
  [1, 4, 7, 12].map((m) => site.importance(flat, style(m))).join(" ")
);
check(
  "...and seasonal emphasis does nothing to it",
  close(site.importance(flat, style(7, 1)), flat.pr)
);

const skiMonths = [30, 35, 20, 6, 1, 1, 2, 2, 1, 2, 5, 25];
const ski = item(50000, skiMonths);
const total = skiMonths.reduce((a, b) => a + b, 0);
const expected = (m) => Math.log10(1 + 12 * 50000 * (skiMonths[m - 1] / total)) / scale;
check(
  "a seasonal place matches the pipeline's formula month by month",
  [2, 7].every((m) => close(site.importance(ski, style(m)), expected(m), 0.01)),
  `Feb ${site.importance(ski, style(2))} vs ${expected(2)}, Jul ${site.importance(ski, style(7))} vs ${expected(7)}`
);
check(
  "February above the year, July below it",
  site.importance(ski, style(2)) > ski.pr && site.importance(ski, style(7)) < ski.pr
);
check(
  "'in season' lifts the peak further and sinks the trough further",
  site.importance(ski, style(2, 1)) > site.importance(ski, style(2, 0)) &&
    site.importance(ski, style(7, 1)) < site.importance(ski, style(7, 0))
);
check("the whole year is the year's score", site.importance(ski, style(0)) === ski.pr);
check("busiest month is February", site.busiest(ski) === 1, String(site.busiest(ski)));
check("a flat year has no busiest month", site.busiest(flat) === null);

const cached = site.importance(ski, style(2));
const s = style(7);
const july = site.importance(ski, s);
check("a new key is not served the old month's value", july !== cached);

const silent = { cat: 2, pr: 0.3, extra: Array(13).fill(0) };
check(
  "no views at all falls back to the year rather than dividing by zero",
  monthShares(silent) === null && site.importance(silent, style(5)) === 0.3
);

check(
  "a listing links into its guide's section",
  site.articleAnchor({ cat: 2, wikiLang: "de" }) === "#Sehensw%C3%BCrdigkeiten" &&
    site.articleAnchor({ cat: 1, wikiLang: "en" }) === ""
);
check(
  "synthetic ids are not Wikidata items",
  site.wikidataOf({ qid: 0x80000005 }) === null && site.wikidataOf({ qid: 64 }) === 64
);

if (failures) {
  console.log(`${failures} check(s) failed`);
  process.exit(1);
}
console.log("\nseasons behave");
