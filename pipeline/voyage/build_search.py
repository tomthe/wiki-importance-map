"""Stage 5 - the travel map's search index, in the Wikipedia map's format.

Every destination is findable; listings are findable above a floor, because
"Hotel Central" exists in four hundred towns and a result list of them is noise
without the town's name - which is why a listing's result row shows the
destination it is in, where a destination's shows its country.

Usage:
    python -m pipeline.voyage.build_search [--listing-min-score 0.45]
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import polars as pl

from pipeline.build_search import write_search
from pipeline.packfile import DEFAULT_PART_BYTES
from pipeline.voyage import config


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--listing-min-score", type=float, default=0.45)
    ap.add_argument("--cap", type=int, default=500)
    ap.add_argument("--part-bytes", type=int, default=DEFAULT_PART_BYTES)
    args = ap.parse_args()
    started = time.perf_counter()

    manifest = json.loads((config.DATA_DIR / "manifest.json").read_text(encoding="utf-8"))
    country_id = {name: i for i, name in enumerate(manifest["countries"])}

    df = pl.read_parquet(
        config.MASTER_PARQUET,
        columns=["id", "title", "lon", "lat", "s", "peak", "cat", "kind",
                 "country_label", "admin"],
    )
    is_dest = pl.col("kind") == "destination"
    df = df.filter(is_dest | (pl.col("s") >= args.listing_min_score)).with_columns(
        pl.col("title").alias("name"),
        pl.col("id").alias("qid"),
        pl.col("s").alias("score"),
        pl.col("country_label")
        .replace_strict(country_id, default=-1, return_dtype=pl.Int64)
        .alias("country_id"),
        pl.when(is_dest).then(None).otherwise(pl.col("admin")).alias("where"),
    ).sort("score", descending=True)
    n_dest = df.filter(is_dest).height
    print(f"indexing {n_dest:,} destinations and {len(df) - n_dest:,} listings")
    write_search(df, config.DATA_DIR, 0.0, args.cap, args.part_bytes)
    print(f"  {(time.perf_counter() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
