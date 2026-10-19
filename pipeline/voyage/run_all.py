"""Build the travel map, or part of it.

    python -m pipeline.voyage.run_all                  # everything
    python -m pipeline.voyage.run_all --from master    # after new pageviews
    python -m pipeline.voyage.run_all --only tiles

Needs the Wikipedia map's `sitelinks`, `truthy` and `master` stages to have
run once (their outputs in WIKIMAP_WORK are read for coordinates, countries and
Wikidata ids), and these downloads in WIKIVOYAGE_DUMPS:

    pageviews/pageviews-YYYYMM-user.bz2    Nov 2022 - Oct 2024, 24 months
                                           (why not later: extract_pageviews.py)
    xml/<lang>wikivoyage-<date>-pages-articles.xml.bz2   every edition
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

STAGES = [
    ("pageviews", "pipeline.voyage.extract_pageviews", "12 monthly pageview dumps (~15 min)"),
    ("xml", "pipeline.voyage.extract_xml", "articles, redirects, listings (~2 min)"),
    ("master", "pipeline.voyage.build_master", "destinations + listings + seasons"),
    ("tiles", "pipeline.voyage.build_tiles", "tile pyramid into voyage/data"),
    ("search", "pipeline.voyage.build_search", "search index into voyage/data"),
]
NAMES = [s[0] for s in STAGES]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="start", choices=NAMES)
    ap.add_argument("--only", choices=NAMES)
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()
    if args.list:
        for name, module, note in STAGES:
            print(f"  {name:10s} {module:36s} {note}")
        return
    if args.only:
        todo = [s for s in STAGES if s[0] == args.only]
    else:
        todo = STAGES[NAMES.index(args.start) if args.start else 0 :]
    overall = time.perf_counter()
    for name, module, note in todo:
        print(f"\n{'=' * 70}\n== {name}: {note}\n{'=' * 70}", flush=True)
        started = time.perf_counter()
        if subprocess.run([sys.executable, "-m", module]).returncode != 0:
            sys.exit(f"stage '{name}' failed")
        print(f"-- {name} took {(time.perf_counter() - started) / 60:.1f} min", flush=True)
    print(f"\nall done in {(time.perf_counter() - overall) / 60:.1f} min")


if __name__ == "__main__":
    main()
