"""Run the whole pipeline, or part of it.

    python -m pipeline.run_all                 # everything
    python -m pipeline.run_all --from master   # skip the slow extraction stages
    python -m pipeline.run_all --only tiles

Each stage runs in its own process so its memory is handed back before the
next one starts.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time

STAGES = [
    ("truthy", "pipeline.extract_truthy", "extract the Wikidata truthy dump (~45 min)"),
    ("ranks", "pipeline.extract_ranks", "danker PageRank + QRank (~1 min)"),
    ("sitelinks", "pipeline.extract_sitelinks", "Wikipedia titles per item (~5 min)"),
    ("master", "pipeline.build_master", "join into articles.parquet"),
    ("tiles", "pipeline.build_tiles", "binary tile pyramid"),
    ("search", "pipeline.build_search", "prefix search index"),
]
NAMES = [s[0] for s in STAGES]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="start", choices=NAMES, help="start at this stage")
    ap.add_argument("--only", choices=NAMES, help="run just this stage")
    ap.add_argument("--list", action="store_true", help="show the stages and exit")
    args = ap.parse_args()

    if args.list:
        for name, module, note in STAGES:
            print(f"  {name:10s} {module:28s} {note}")
        return

    if args.only:
        todo = [s for s in STAGES if s[0] == args.only]
    else:
        start = NAMES.index(args.start) if args.start else 0
        todo = STAGES[start:]

    overall = time.perf_counter()
    for name, module, note in todo:
        print(f"\n{'=' * 70}\n== {name}: {note}\n{'=' * 70}", flush=True)
        started = time.perf_counter()
        result = subprocess.run([sys.executable, "-m", module])
        if result.returncode != 0:
            sys.exit(f"stage '{name}' failed with exit code {result.returncode}")
        print(f"-- {name} took {(time.perf_counter() - started) / 60:.1f} min", flush=True)

    print(f"\nall done in {(time.perf_counter() - overall) / 60:.1f} min")


if __name__ == "__main__":
    main()
