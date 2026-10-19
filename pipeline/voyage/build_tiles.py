"""Stage 4 - the travel map's tile pyramid, in the Wikipedia map's format.

Same pyramid, same WMT2 tiles, same pack files and the same client; only the
columns mean slightly different things:

    score   the year: log views over twelve months (see build_master)
    pr      the year again - the client's "importance" before any month is picked
    qr      the peak month, which is also what the pyramid is built on
    extras  twelve bytes of monthly share relative to the item's own peak month,
            then the English guide's status (0 none, 1 outline ... 4 star)
    admin   "in <destination>" for a listing, the parent region for a destination
    sitelinks   how many Wikivoyage editions cover a destination

Placement uses the peak month rather than the year, so a destination that only
matters in one season is still in the shallow tiles the map draws for that
season. Nothing here is at a borrowed coordinate, so nothing is spread and the
deepest zoom keeps every item.

Usage:
    python -m pipeline.voyage.build_tiles [--max-zoom 13]
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
import time

import polars as pl

from pipeline.build_tiles import (
    FLAG_HAS_IMAGE,
    FLAG_HAS_WEBSITE,
    FLAG_HAS_WIKI,
    NO_INT16,
    NO_POP,
    NO_REF,
    VERSION,
    assign_zoom,
    write_pyramid,
)
from pipeline.packfile import DEFAULT_PART_BYTES
from pipeline.voyage import config
from pipeline.voyage.taxonomy import CATEGORIES, PALETTE, subcategory_names

print = functools.partial(print, flush=True)

MONTHS = [f"m{m}" for m in range(1, 13)]
EXTRAS = (*MONTHS, "status")
MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
STATUSES = ["", "outline", "usable", "guide", "star"]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-zoom", type=int, default=13)
    ap.add_argument("--capacity", type=int, default=200)
    ap.add_argument("--cat-quota", type=int, default=8)
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--part-bytes", type=int, default=DEFAULT_PART_BYTES)
    args = ap.parse_args()

    started = time.perf_counter()
    master = pl.read_parquet(config.MASTER_PARQUET)
    meta = json.loads((config.WORK_DIR / "voyage_meta.json").read_text(encoding="utf-8"))
    scale = meta["importanceScale"]
    print(f"{len(master):,} items")

    countries = (
        master.select("country_label").drop_nulls().group_by("country_label").len()
        .sort(["len", "country_label"], descending=[True, False])
        .with_row_index("country_id")
    )
    country_names = countries["country_label"].to_list()

    u16 = lambda col: (pl.col(col) * 65535).clip(0, 65535).cast(pl.UInt16)  # noqa: E731
    df = master.join(
        countries.select("country_label", "country_id"), on="country_label", how="left"
    ).select(
        pl.col("id").cast(pl.UInt32).alias("qid"),
        pl.col("lon").cast(pl.Float64),
        pl.col("lat").cast(pl.Float64),
        pl.col("peak").cast(pl.Float64).alias("score"),
        u16("s").alias("score_u16"),
        u16("s").alias("pr"),
        u16("peak").alias("qr"),
        pl.col("cat").cast(pl.UInt8),
        pl.col("sub").cast(pl.UInt8),
        pl.col("population").clip(0, NO_POP - 1).round().fill_null(NO_POP)
        .cast(pl.UInt32).alias("pop"),
        pl.col("elevation").clip(NO_INT16 + 1, 32767).round().fill_null(NO_INT16)
        .cast(pl.Int16).alias("elev"),
        pl.lit(NO_INT16, dtype=pl.Int16).alias("year"),
        pl.col("editions").cast(pl.UInt8).alias("sitelinks"),
        pl.col("country_id").fill_null(NO_REF).cast(pl.UInt16).alias("country"),
        pl.col("admin").fill_null("").alias("admin"),
        pl.col("descr").fill_null("").alias("descr"),
        pl.col("title"),
        # "lang|title", title left empty when it is the drawn label - always the
        # case for an English destination, never for a listing.
        pl.format(
            "{}|{}",
            pl.col("link_lang"),
            pl.when(pl.col("link_title") == pl.col("title"))
            .then(pl.lit(""))
            .otherwise(pl.col("link_title")),
        ).alias("wiki"),
        (
            FLAG_HAS_WIKI
            + pl.when(pl.col("has_image")).then(FLAG_HAS_IMAGE).otherwise(0)
            + pl.when(pl.col("has_url")).then(FLAG_HAS_WEBSITE).otherwise(0)
        ).cast(pl.UInt8).alias("flags"),
        *[pl.col(m).cast(pl.UInt8) for m in MONTHS],
        pl.col("status").cast(pl.UInt8),
        pl.lit(False).alias("derived"),
    )

    print("assigning zoom levels")
    placement = assign_zoom(
        df.select("qid", "lon", "lat", "score", "cat", "derived"),
        args.max_zoom,
        args.capacity,
        args.cat_quota,
        deep_capacity=1 << 30,  # nothing is borrowed, so nothing is dropped
    ).select("qid", "z", "tx", "ty")
    df = df.drop("score").join(placement, on="qid", how="inner")

    zoom_meta, tile_count, pack_info = write_pyramid(
        df, config.DATA_DIR, args.max_zoom, args.part_bytes, args.workers, EXTRAS
    )

    subs = subcategory_names()
    manifest = {
        "version": VERSION,
        "format": "WMT2",
        "site": "voyage",
        "maxZoom": args.max_zoom,
        "capacity": args.capacity,
        "tileCount": tile_count,
        "itemCount": len(df),
        "destinationCount": int((master["kind"] == "destination").sum()),
        "totalBytes": pack_info["bytes"],
        "pack": pack_info,
        "zooms": zoom_meta,
        "countries": country_names,
        "categories": [
            {"id": i, "name": name, "subcategories": subs.get(name, ["Other"])}
            for i, name in enumerate(CATEGORIES)
        ],
        "palette": PALETTE,
        "locSources": [""] * 8,
        "peopleCat": -1,
        # How the client turns the bytes back into a monthly importance:
        # s_m = log10(1 + 12 * V * share_m) / importanceScale, where
        # V = 10^(pr * importanceScale) - 1 and share_m = extra[m] / sum(extra[0..11]).
        "importanceScale": scale,
        "viewsFrom": meta["viewsFrom"],
        "viewsTo": meta["viewsTo"],
        "extras": list(EXTRAS),
        "months": MONTH_NAMES,
        "statuses": STATUSES,
    }
    (config.DATA_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"\nwrote {tile_count:,} tiles, {pack_info['bytes'] / 1e6:.1f} MB in "
        f"{len(pack_info['parts'])} part(s); mean tile "
        f"{pack_info['bytes'] / max(tile_count, 1) / 1024:.1f} KiB"
    )
    print(f"  {(time.perf_counter() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
