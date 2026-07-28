"""Stage 5 - turn articles.parquet into a tile pyramid the browser can stream.

Pyramid
-------
Standard slippy-map XYZ tiles. Every item is written into exactly ONE tile:
the shallowest zoom whose tile still has room, taking items in importance
order. So zoom 0 holds the few hundred most important places on Earth, zoom 1
the next band, and so on.

That single property is what fixes the two old bugs. Rendering zoom Z means
loading the tiles for z = 0..Z that intersect the viewport and drawing their
union - parent tiles stay in cache while panning, nothing is ever loaded
twice, and nothing vanishes because a tile was replaced.

Packing
-------
The tiles are not written as files. 98,786 of them averaging 4 KiB is the
worst shape this data could take: it breaks shared-hosting inode quotas, makes
git crawl, and wastes 60% of the disk in cluster slack. They go into a handful
of big files instead and the client fetches one with an HTTP `Range` header -
see pipeline/packfile.py for why it is a handful and not literally one.

Tiles are laid out in (zoom, key) order where key = x<<16 | y, so the tiles a
viewport needs are usually adjacent in the pack and the client can pull a
whole column of them in a single request. Because the order is fixed, the
per-zoom index only has to store lengths: an offset is the running sum.

Format (WMT2)
-------------
One little-endian binary blob per tile, gzipped, laid out as columns so the
browser can hand the arrays straight to deck.gl without touching each row:

  offset 0   magic "WMT2", uint16 version, uint16 z
         8   uint32 x, uint32 y, uint32 count
        20   uint32 titleBytes, wikiBytes, descrBytes
        32   uint32 adminCount, adminBytes, reserved, reserved
        48   qid Uint32[n], titleOff Uint32[n+1], wikiOff Uint32[n+1],
             descrOff Uint32[n+1], lon Float32[n], lat Float32[n],
             population Uint32[n],
             score Uint16[n], pr Uint16[n], qr Uint16[n],
             country Uint16[n], admin Uint16[n],
             elevation Int16[n], year Int16[n],                  (pad to 4)
             cat Uint8[n], sub Uint8[n], flags Uint8[n], sitelinks Uint8[n],
             adminOff Uint32[adminCount+1], adminBlob UTF-8,
             titles UTF-8, wikis UTF-8, descrs UTF-8

Every numeric array starts on a 4-byte boundary so the decoder can create
typed array views directly on the buffer.

Three of those columns are worth explaining:

* `wiki` holds "lang|title", and the title half is left empty when it equals
  the displayed label - by far the common case, which gzip then squashes to
  nothing.
* `country` indexes a table shared by the whole pyramid and shipped in
  manifest.json. A tile is nearly always one country, so gzip erases the
  column entirely: it measures at 0.1 bytes per item.
* `admin` indexes a string table private to the tile. There are 293,833
  distinct admin areas, far too many for a global table, but only a handful in
  any one tile. For an item placed at a *borrowed* coordinate it holds the
  borrowed place's name instead - "Ulm" rather than "Baden-Württemberg" -
  because that is the word the tooltip needs and it costs no new column.
* `flags` bit 0 means an article exists, bit 1 an image, bit 2 a website, and
  bits 3-5 say why the item is at this coordinate at all: 0 its own P625,
  1 born here, 2 died here, 3 located here, 4 headquartered here, 5 set here,
  6 home port, 7 otherwise associated. Everything above 0 means the coordinate
  belongs to something else and the client must say so.

Sentinels rather than a null mask, because they compress to nothing:
population 0xFFFFFFFF, country/admin 0xFFFF, elevation/year -32768.

Usage:
    python -m pipeline.build_tiles [--max-zoom 12] [--capacity 250]
"""

from __future__ import annotations

import argparse
import functools
import gzip
import json
import math
import shutil
import struct
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import polars as pl

from pipeline import config
from pipeline.packfile import DEFAULT_PART_BYTES, PackWriter, remove_parts
from pipeline.taxonomy import CATEGORIES, CATEGORY_ID, PALETTE, subcategory_names

MAGIC = b"WMT2"
VERSION = 2
HEADER_BYTES = 48
FLAG_HAS_WIKI = 1
FLAG_HAS_IMAGE = 2
FLAG_HAS_WEBSITE = 4
# Bits 3-5 hold *why* this item is at this coordinate: 0 its own P625, 1 born
# here, 2 died here, and so on - see config.LOC_SOURCE. Three bits, seven
# meanings, and bits 6-7 are still free.
FLAG_LOC_SHIFT = 3
FLAG_LOC_MASK = 0b111

NO_POP = 0xFFFFFFFF
NO_REF = 0xFFFF          # country and admin
NO_INT16 = -32768        # elevation and year

PEOPLE_CAT = CATEGORY_ID["People"]

# Phyllotaxis: consecutive points a golden angle apart, at radius proportional
# to sqrt(index), which is how a sunflower packs seeds evenly into a disc.
GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))
JITTER_MIN_KM = 0.2
JITTER_MAX_KM = 8.0
KM_PER_DEGREE = 111.32

print = functools.partial(print, flush=True)


def tile_xy(lon: np.ndarray, lat: np.ndarray, z: int) -> tuple[np.ndarray, np.ndarray]:
    """Web-Mercator tile indices, clamped to the mercator latitude limits."""
    n = 1 << z
    x = np.floor((lon + 180.0) / 360.0 * n).astype(np.int64)
    clamped = np.clip(lat, -85.05112878, 85.05112878)
    sin_lat = np.sin(np.radians(clamped))
    y = np.floor(
        (0.5 - np.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * n
    ).astype(np.int64)
    return np.clip(x, 0, n - 1), np.clip(y, 0, n - 1)


def spread_derived(df: pl.DataFrame) -> pl.DataFrame:
    """Fan items placed at the same borrowed coordinate out around it.

    Everyone born in Paris resolves to one point. Left alone that is a single
    dot with fifty thousand things behind it: the collision pass can only ever
    show one of them, and the other 49,999 are invisible and unhoverable.

    So the k-th item at a place (in score order) is moved to radius
    R*sqrt(k/K) at k golden angles round the circle - even coverage of the
    disc, no random number generator, and the same answer on every rebuild.
    The most important one stays exactly on the place. R comes from the
    place's population, because a village should not scatter its people across
    a county.

    This invents a coordinate, which is why the location-source flag and the
    "born in Ulm" wording in the tooltip are not optional: the map must never
    imply the person is standing there.

    build_tiles and build_search both call this, on the whole table and before
    either of them filters anything, because they have to agree to the metre:
    if the search index kept the real coordinate, clicking "Albert Einstein"
    would fly to the middle of Ulm and his dot would be somewhere off-screen.
    That is also why the sort lives in here and why it breaks ties on qid - a
    plain sort on score alone is not a total order, and two callers could
    number a tied group differently.
    """
    df = df.sort(["score", "qid"], descending=[True, False])
    derived = pl.col("loc_pid") != 0
    df = df.with_columns(
        pl.int_range(pl.len()).over("loc_qid").alias("_k"),
        pl.len().over("loc_qid").alias("_kn"),
    )
    # Radius from the place's population where there is one, otherwise from
    # how many items landed there, which is a decent proxy for the same thing.
    radius_km = (
        pl.when(pl.col("loc_pop").is_not_null())
        .then(1.2 * (1 + pl.col("loc_pop")).log10() - 3.0)
        .otherwise(0.3 * pl.col("_kn").sqrt())
        .clip(JITTER_MIN_KM, JITTER_MAX_KM)
    )
    r = radius_km * (pl.col("_k") / pl.col("_kn")).sqrt()
    theta = pl.col("_k") * GOLDEN_ANGLE
    # Longitude degrees shrink with latitude; the clamp keeps a birthplace in
    # Svalbard from being flung across the Arctic.
    cos_lat = (pl.col("lat") * math.pi / 180).cos().clip(0.05, 1.0)
    moved = derived & (pl.col("_kn") > 1) & (pl.col("_k") > 0)
    out = df.with_columns(
        pl.when(moved)
        .then(pl.col("lon") + r * theta.cos() / (KM_PER_DEGREE * cos_lat))
        .otherwise(pl.col("lon"))
        .alias("lon"),
        pl.when(moved)
        .then(pl.col("lat") + r * theta.sin() / KM_PER_DEGREE)
        .otherwise(pl.col("lat"))
        .alias("lat"),
    ).with_columns(
        pl.col("lon").clip(-180.0, 180.0),
        pl.col("lat").clip(-85.05, 85.05),
    )
    n_moved = out.select(moved.sum()).item()
    places = out.filter(derived).select(pl.col("loc_qid").n_unique()).item()
    print(f"  spread {n_moved:,} derived items around {places:,} places")
    return out.drop("_k", "_kn")


def assign_zoom(
    df: pl.DataFrame,
    max_zoom: int,
    capacity: int,
    cat_quota: int,
    deep_capacity: int,
) -> pl.DataFrame:
    """Give every row the shallowest zoom whose tile still has room.

    Processing zoom by zoom is equivalent to walking the items one at a time
    in score order and pushing each down until it fits, but it stays vectorised.

    Each tile has two ways in: the global budget (`capacity` items by score)
    and a per-category budget (`cat_quota` items per category). Without the
    second one, filtering the map to "Museums" at world zoom would show
    nothing at all, because no museum outranks the top few hundred cities.
    """
    df = df.sort("score", descending=True)
    placed: list[pl.DataFrame] = []
    remaining = df
    for z in range(max_zoom + 1):
        if remaining.is_empty():
            break
        lon = remaining["lon"].to_numpy()
        lat = remaining["lat"].to_numpy()
        tx, ty = tile_xy(lon, lat, z)
        remaining = remaining.with_columns(
            pl.Series("tx", tx, dtype=pl.Int64),
            pl.Series("ty", ty, dtype=pl.Int64),
        ).with_columns(
            pl.int_range(pl.len()).over(["tx", "ty"]).alias("slot"),
            pl.int_range(pl.len()).over(["tx", "ty", "cat"]).alias("cat_slot"),
        )
        if z == max_zoom:
            # The deepest level used to take everything left over, which was
            # fine while every item had its own coordinate. It is not fine once
            # two million people are stacked on a few thousand cities: one z12
            # tile over central Paris wants 22,559 items and would encode to
            # 850 KiB for a single viewport. So the last level gets a budget
            # too - but the budget falls on the *borrowed* coordinates only.
            #
            # An item with a real P625 is never dropped. It is genuinely there,
            # it was on the map before People existed, and evicting a quarter
            # of a million real places to make room for synthetic points would
            # be a straight regression. People are what caused the crowding, so
            # people are what yields: each tile keeps its own-coordinate items
            # in full and fills whatever budget is left with the highest-scoring
            # derived ones.
            remaining = remaining.with_columns(
                (~pl.col("derived")).sum().over(["tx", "ty"]).alias("own_here"),
                pl.int_range(pl.len())
                .over(["tx", "ty", "derived"])
                .alias("derived_slot"),
            )
            keep = (~pl.col("derived")) | (
                pl.col("own_here") + pl.col("derived_slot") < deep_capacity
            )
            fits = remaining.filter(keep).drop("own_here", "derived_slot")
            rest = remaining.filter(~keep)
            if len(rest):
                worst = (
                    remaining.group_by("tx", "ty")
                    .len()
                    .sort("len", descending=True)
                    .head(1)
                )
                print(
                    f"  z={z:2d}  dropped {len(rest):,} borrowed-coordinate items "
                    f"over the {deep_capacity:,}-per-tile budget "
                    f"(fullest tile wanted {worst['len'][0]:,}); "
                    f"every item with a real coordinate was kept"
                )
            rest = rest.clear()  # nowhere deeper to push them
        else:
            keep = (pl.col("slot") < capacity) | (pl.col("cat_slot") < cat_quota)
            fits = remaining.filter(keep)
            rest = remaining.filter(~keep)
        placed.append(fits.with_columns(pl.lit(z, dtype=pl.UInt8).alias("z")))
        print(
            f"  z={z:2d}  {len(fits):9,d} items  "
            f"{fits.select(pl.struct('tx', 'ty').n_unique()).item():7,d} tiles  "
            f"{len(rest):9,d} left"
        )
        remaining = rest.drop("slot", "cat_slot")
    return pl.concat(placed)


# ------------------------------------------------------------------- encoding


def _blob(strings: list[str]) -> tuple[bytes, np.ndarray]:
    """UTF-8 concatenation plus the n+1 offsets into it."""
    n = len(strings)
    offsets = np.zeros(n + 1, dtype=np.uint32)
    parts: list[bytes] = []
    total = 0
    for i, s in enumerate(strings):
        raw = s.encode("utf-8")
        parts.append(raw)
        total += len(raw)
        offsets[i + 1] = total
    return b"".join(parts), offsets


def encode_tile(rows: dict, z: int, x: int, y: int) -> bytes:
    n = len(rows["qid"])

    title_blob, title_off = _blob(rows["title"])
    wiki_blob, wiki_off = _blob(rows["wiki"])
    descr_blob, descr_off = _blob(rows["descr"])

    # Admin areas repeat hard inside a tile, so a private string table plus a
    # u16 index costs a fraction of storing the name on every row.
    admin_ids = np.full(n, NO_REF, dtype=np.uint16)
    admin_names: list[str] = []
    admin_lookup: dict[str, int] = {}
    for i, name in enumerate(rows["admin"]):
        if not name:
            continue
        got = admin_lookup.get(name)
        if got is None:
            got = len(admin_names)
            if got >= NO_REF:  # pathological tile; the rest lose their admin
                continue
            admin_lookup[name] = got
            admin_names.append(name)
        admin_ids[i] = got
    admin_blob, admin_off = _blob(admin_names)

    parts: list[bytes] = [
        struct.pack(
            "<4sHHIIIIIIIIII",
            MAGIC, VERSION, z,
            x, y, n,
            len(title_blob), len(wiki_blob), len(descr_blob),
            len(admin_names), len(admin_blob),
            0, 0,
        )
    ]
    parts.append(rows["qid"].astype("<u4").tobytes())
    parts.append(title_off.astype("<u4").tobytes())
    parts.append(wiki_off.astype("<u4").tobytes())
    parts.append(descr_off.astype("<u4").tobytes())
    parts.append(rows["lon"].astype("<f4").tobytes())
    parts.append(rows["lat"].astype("<f4").tobytes())
    parts.append(rows["pop"].astype("<u4").tobytes())
    parts.append(rows["score"].astype("<u2").tobytes())
    parts.append(rows["pr"].astype("<u2").tobytes())
    parts.append(rows["qr"].astype("<u2").tobytes())
    parts.append(rows["country"].astype("<u2").tobytes())
    parts.append(admin_ids.astype("<u2").tobytes())
    parts.append(rows["elev"].astype("<i2").tobytes())
    parts.append(rows["year"].astype("<i2").tobytes())
    pad = (-14 * n) % 4
    if pad:
        parts.append(b"\0" * pad)
    parts.append(rows["cat"].astype("u1").tobytes())
    parts.append(rows["sub"].astype("u1").tobytes())
    parts.append(rows["flags"].astype("u1").tobytes())
    parts.append(rows["sitelinks"].astype("u1").tobytes())
    parts.append(admin_off.astype("<u4").tobytes())
    parts.append(admin_blob)
    parts.append(title_blob)
    parts.append(wiki_blob)
    parts.append(descr_blob)
    return b"".join(parts)


def _encode_and_gzip(job) -> tuple[int, bytes]:
    """Runs in a worker: the whole per-tile cost except writing."""
    key, z, x, y, rows = job
    return key, gzip.compress(encode_tile(rows, z, x, y), 6, mtime=0)


# ----------------------------------------------------------------- main build


NUMERIC_COLUMNS = (
    "qid", "lon", "lat", "score_u16", "pr", "qr",
    "pop", "country", "elev", "year", "cat", "sub", "flags", "sitelinks",
)
STRING_COLUMNS = ("title", "wiki", "descr", "admin")
_FIELD_OF = {"score_u16": "score"}


def tile_jobs(part: pl.DataFrame, z: int):
    """Split one zoom, already sorted by (tx, ty), into per-tile job tuples.

    Slicing pre-extracted arrays beats iterating polars groups: there are up to
    60,000 tiles in a zoom and building that many group frames dominates the
    stage.
    """
    tx = part["tx"].to_numpy()
    ty = part["ty"].to_numpy()
    keys = (tx.astype(np.uint32) << 16) | ty.astype(np.uint32)
    arrays = {name: part[name].to_numpy() for name in NUMERIC_COLUMNS}
    lists = {name: part[name].to_list() for name in STRING_COLUMNS}

    edges = np.flatnonzero(np.diff(keys)) + 1
    starts = np.concatenate(([0], edges))
    stops = np.concatenate((edges, [len(keys)]))
    for start, stop in zip(starts.tolist(), stops.tolist()):
        rows = {_FIELD_OF.get(k, k): arrays[k][start:stop] for k in NUMERIC_COLUMNS}
        rows.update({k: lists[k][start:stop] for k in STRING_COLUMNS})
        yield (
            int(keys[start]),
            z,
            int(tx[start]),
            int(ty[start]),
            rows,
        )


def zoom_index(keys: list[int], lengths: list[int]) -> bytes:
    """uint32 n, uint32 key[n], uint32 length[n] - gzipped.

    No offsets: tiles sit in the pack in exactly this order, so the client
    prefix-sums the lengths onto the zoom's base offset. That halves the index,
    which matters because the client downloads one per zoom it visits.
    """
    head = np.array([len(keys)], dtype="<u4").tobytes()
    body = np.array(keys, dtype="<u4").tobytes() + np.array(lengths, dtype="<u4").tobytes()
    return gzip.compress(head + body, 6, mtime=0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-zoom", type=int, default=12)
    ap.add_argument("--capacity", type=int, default=200)
    ap.add_argument("--cat-quota", type=int, default=8)
    ap.add_argument(
        "--deep-capacity",
        type=int,
        default=8000,
        help=(
            "per-tile budget at the deepest zoom, where there is nowhere left "
            "to push an item. Only borrowed-coordinate items are ever dropped "
            "by it; overflow is reported. 8000 keeps the worst tile at 358 KiB "
            "against a 326 KiB floor set by real places alone, and drops 3.8%% "
            "of the derived items. Doubling it to 16000 saves 80k more people "
            "but takes the worst tile to 673 KiB."
        ),
    )
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument(
        "--part-bytes",
        type=int,
        default=DEFAULT_PART_BYTES,
        help="max bytes per pack part; keep under GitHub's 100 MiB file limit",
    )
    args = ap.parse_args()

    started = time.perf_counter()
    print(f"reading {config.MASTER_PARQUET}")
    master = pl.read_parquet(
        config.MASTER_PARQUET,
        columns=[
            "qid", "lon", "lat", "score", "pr_norm", "qr_norm",
            "cat", "sub", "label_en", "title_en", "native_label",
            "title_native", "native_site", "title_any", "any_site",
            "image", "website", "descr_en", "population", "elevation",
            "inception", "birth", "n_sitelinks", "country_label", "admin_label",
            "n_countries", "n_admin",
            "loc_pid", "loc_qid", "loc_label", "loc_pop",
        ],
    )
    # "Where is it" only has an answer when there is one. A river through ten
    # countries has a P17 for each, and build_master keeps an arbitrary one; put
    # that in a tooltip and it reads as fact. Blank is better than wrong.
    master = master.with_columns(
        pl.when(pl.col("n_countries") > 1).then(None).otherwise(pl.col("country_label"))
        .alias("country_label"),
        pl.when(pl.col("n_admin") > 1).then(None).otherwise(pl.col("admin_label"))
        .alias("admin_label"),
    )
    print(f"  {len(master):,} items")

    # Before the unnamed rows are dropped, so build_search - which drops a
    # different set - lands on exactly the same coordinates.
    master = spread_derived(master)

    # A tile is a list of labels; an item with no name anywhere would draw as
    # "Q12345", which is noise. Those rows stay in articles.parquet.
    before = len(master)
    master = master.filter(
        pl.any_horizontal(
            pl.col("label_en").is_not_null(),
            pl.col("title_en").is_not_null(),
            pl.col("native_label").is_not_null(),
            pl.col("title_any").is_not_null(),
        )
    )
    print(f"  {before - len(master):,} unnamed items skipped, {len(master):,} to place")

    # Country ids, most common first: low ids leave the high byte zero, which
    # is one less thing for gzip to carry.
    countries = (
        master.select("country_label")
        .drop_nulls()
        .group_by("country_label")
        .len()
        .sort(["len", "country_label"], descending=[True, False])
        .with_row_index("country_id")
    )
    country_names = countries["country_label"].to_list()
    print(f"  {len(country_names):,} distinct countries")

    df = master.join(
        countries.select("country_label", "country_id"), on="country_label", how="left"
    ).select(
        pl.col("qid").cast(pl.UInt32),
        pl.col("lon").cast(pl.Float64),
        pl.col("lat").cast(pl.Float64),
        pl.col("score").cast(pl.Float64),
        (pl.col("pr_norm").fill_null(0.0) * 65535).clip(0, 65535).cast(pl.UInt16).alias("pr"),
        (pl.col("qr_norm").fill_null(0.0) * 65535).clip(0, 65535).cast(pl.UInt16).alias("qr"),
        pl.col("cat").cast(pl.UInt8),
        pl.col("sub").cast(pl.UInt8),
        # Population is exact, not log-scaled: the tooltip prints it. Wikidata
        # has nine items claiming more than a billion, and one claiming five,
        # so the clip is doing real work.
        pl.col("population")
        .clip(0, NO_POP - 1)
        .round()
        .fill_null(NO_POP)
        .cast(pl.UInt32)
        .alias("pop"),
        pl.col("elevation")
        .clip(NO_INT16 + 1, 32767)
        .round()
        .fill_null(NO_INT16)
        .cast(pl.Int16)
        .alias("elev"),
        # "1784-05-12T00:00:00Z", and "-0500-..." for BC. For a person the
        # interesting year is when they were born, not when anything was
        # founded; the client keys off the category to label it correctly.
        pl.when(pl.col("cat") == PEOPLE_CAT)
        .then(pl.col("birth"))
        .otherwise(pl.col("inception"))
        .str.extract(r"^(-?\d+)", 1)
        .cast(pl.Int32, strict=False)
        .clip(NO_INT16 + 1, 32767)
        .fill_null(NO_INT16)
        .cast(pl.Int16)
        .alias("year"),
        pl.col("n_sitelinks").fill_null(0).clip(0, 255).cast(pl.UInt8).alias("sitelinks"),
        pl.col("country_id").fill_null(NO_REF).cast(pl.UInt16).alias("country"),
        # For a derived row the admin slot carries the *place it was placed
        # at* - "Ulm", not "Baden-Württemberg". That is the word the tooltip
        # needs, it repeats hard within a tile so the per-tile string table
        # squashes it, and it costs no new column.
        pl.coalesce(pl.col("loc_label"), pl.col("admin_label"))
        .fill_null("")
        .alias("admin"),
        pl.col("loc_pid"),
        (pl.col("loc_pid") != 0).alias("derived"),
        pl.col("descr_en").fill_null("").alias("descr"),
        # Drawn label: English label, else the English article title, else the
        # name the place uses itself, else whatever article exists.
        pl.coalesce(
            pl.col("label_en"),
            pl.col("title_en"),
            pl.col("native_label"),
            pl.col("title_any"),
        ).alias("title"),
        pl.col("title_en"),
        # Article to link to, preferring English.
        pl.coalesce(pl.col("title_en"), pl.col("title_any")).alias("article"),
        pl.when(pl.col("title_en").is_not_null())
        .then(pl.lit("enwiki"))
        .otherwise(pl.col("any_site"))
        .alias("article_site"),
        pl.col("image"),
        pl.col("website"),
    ).with_columns(
        # "lang|title", with the title left empty when it equals the drawn
        # label - by far the common case, and gzip squashes the rest. Wikipedia
        # titles cannot contain "|", so it is a safe separator.
        pl.when(pl.col("article").is_null())
        .then(pl.lit(""))
        .otherwise(
            pl.format(
                "{}|{}",
                pl.col("article_site").str.strip_suffix("wiki").str.replace_all("_", "-"),
                pl.when(pl.col("article") == pl.col("title"))
                .then(pl.lit(""))
                .otherwise(pl.col("article")),
            )
        )
        .alias("wiki"),
        (
            pl.when(pl.col("article").is_not_null()).then(FLAG_HAS_WIKI).otherwise(0)
            + pl.when(pl.col("image").is_not_null()).then(FLAG_HAS_IMAGE).otherwise(0)
            + pl.when(pl.col("website").is_not_null()).then(FLAG_HAS_WEBSITE).otherwise(0)
            # Why this item is at this coordinate, in bits 3-5. Multiplication
            # rather than a shift because polars expressions have no <<.
            + pl.col("loc_pid")
            .replace_strict(config.LOC_SOURCE, default=0, return_dtype=pl.UInt8)
            .cast(pl.Int32)
            * (1 << FLAG_LOC_SHIFT)
        ).cast(pl.UInt8).alias("flags"),
    ).drop("title_en", "article", "article_site", "image", "website", "loc_pid")

    del master

    print("assigning zoom levels")
    # On the narrow frame only. assign_zoom sorts and re-partitions the whole
    # table once per zoom, and dragging 1 GB of descriptions through thirteen
    # rounds of that costs far more than the join back.
    placement = assign_zoom(
        df.select("qid", "lon", "lat", "score", "cat", "derived"),
        args.max_zoom,
        args.capacity,
        args.cat_quota,
        args.deep_capacity,
    ).select("qid", "z", "tx", "ty")
    df = df.with_columns(
        (pl.col("score") * 65535).clip(0, 65535).cast(pl.UInt16).alias("score_u16")
    ).drop("score").join(placement, on="qid", how="inner")
    del placement

    out_dir = config.DATA_DIR
    for stale in (out_dir / "tiles",):  # the old one-file-per-tile layout
        if stale.exists():
            print(f"removing the old {stale} layout")
            shutil.rmtree(stale)
    gone = remove_parts(out_dir, "tiles")
    if gone:
        print(f"removed {gone} pack part(s) from the previous build")

    print("encoding and packing")
    zoom_meta: dict[str, dict] = {}
    tile_count = 0
    with PackWriter(out_dir, "tiles", args.part_bytes) as pack, ProcessPoolExecutor(
        max_workers=args.workers
    ) as pool:
        for z in range(args.max_zoom + 1):
            part = df.filter(pl.col("z") == z)
            if part.is_empty():
                continue
            part = part.sort(["tx", "ty"])
            base = pack.total
            keys: list[int] = []
            lengths: list[int] = []
            for key, blob in pool.map(
                _encode_and_gzip, tile_jobs(part, z), chunksize=32
            ):
                keys.append(key)
                lengths.append(len(blob))
                pack.add(blob)
            tile_count += len(keys)
            zoom_bytes = sum(lengths)
            index_offset, index_length = pack.add(zoom_index(keys, lengths))
            zoom_meta[str(z)] = {
                "tiles": len(keys),
                "base": base,
                "bytes": zoom_bytes,
                "index": [index_offset, index_length],
            }
            print(
                f"  z={z:2d}  {len(keys):7,d} tiles  {zoom_bytes / 1e6:7.1f} MB  "
                f"index {index_length / 1024:6.1f} KiB"
            )
        pack.close()
        pack_info = pack.info()

    subs = subcategory_names()
    manifest = {
        "version": VERSION,
        "format": "WMT2",
        "maxZoom": args.max_zoom,
        "capacity": args.capacity,
        "tileCount": tile_count,
        "itemCount": len(df),
        "totalBytes": pack_info["bytes"],
        "pack": pack_info,
        "zooms": zoom_meta,
        "countries": country_names,
        "categories": [
            {"id": i, "name": name, "subcategories": subs.get(name, ["Other"])}
            for i, name in enumerate(CATEGORIES)
        ],
        "palette": PALETTE,
        # flag-bits code -> how the tooltip should introduce the place.
        "locSources": [config.LOC_PHRASE[i] for i in range(8)],
        "peopleCat": PEOPLE_CAT,
    }
    (config.DATA_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    index_bytes = sum(m["index"][1] for m in zoom_meta.values())
    print(
        f"\nwrote {tile_count:,} tiles into {len(pack_info['parts'])} pack part(s), "
        f"{pack_info['bytes'] / 1e6:.0f} MB total"
    )
    print(f"  mean tile {pack_info['bytes'] / max(tile_count, 1) / 1024:.1f} KiB")
    print(f"  zoom indexes {index_bytes / 1024:.0f} KiB (fetched lazily, per zoom)")
    print(f"  {(time.perf_counter() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
