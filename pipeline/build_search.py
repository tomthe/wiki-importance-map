"""Stage 6 - a static prefix index so search needs no server.

Names are normalised (lowercase, accents stripped) and indexed under the first
1, 2 and 3 characters of the whole name and of each word, so "gate" finds
"Golden Gate Bridge". Entries are

    [name, lon, lat, qid, score, cat, country]

which is enough to render a result, fly to it and build a link with no second
lookup.

An importance floor, not a truncation
-------------------------------------
The old index took the top three million items by score and kept every one of
them under every prefix they matched: 59,518 shards, 424 MB, and a worst case
where typing three common letters downloaded 7.65 MB to render twelve rows.

Capping each shard fixes the size but answers the wrong question - it makes
what you can find depend on how many other things share your prefix. The floor
does it properly: an item is in the index if it clears `--min-score`, wherever
it sits alphabetically. Score 0.20 keeps 1.39M of 10.4M named items, which is
everything with any claim to being looked up, and nothing that would only ever
be noise in a result list. `--cap` is still there as a bound on the worst
keystroke, but with the floor in place it barely binds.

Layout
------
Like the tiles, the shards go into a packed file read by HTTP `Range` rather
than into tens of thousands of little JSON files - which also retires the
`con.json` problem, since Windows has no opinion about byte offsets.

    data/search.json        root: first character -> where its directory lives
    data/search.NNN.bin     gzipped shards, then one directory per letter

A directory is fetched the first time you type a character that needs it
(a few KB), and then every keystroke under that letter is one range request.
The old build made every visitor download a 1.26 MB manifest before they could
type anything.

Usage:
    python -m pipeline.build_search [--min-score 0.20] [--cap 500]
"""

from __future__ import annotations

import argparse
import functools
import gzip
import json
import shutil
import time
import unicodedata
from collections import defaultdict

import numpy as np
import polars as pl

from pipeline import config
from pipeline.build_tiles import spread_derived
from pipeline.packfile import DEFAULT_PART_BYTES, PackWriter, remove_parts

print = functools.partial(print, flush=True)

TOP_PER_SHORT_SHARD = 2500
MAX_WORDS = 5
# Below this many distinct prefixes, a first character is not worth its own
# directory chunk; they share one. Keeps the root that every visitor downloads
# to a few kilobytes instead of the long tail of scripts with three entries.
MIN_PREFIXES_PER_LETTER = 64
RARE = " "


def normalise(text: str) -> str:
    """Lowercase, strip accents, drop punctuation."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    kept = [
        ch
        for ch in decomposed
        if not unicodedata.combining(ch) and (ch.isalnum() or ch.isspace())
    ]
    return "".join(kept).strip()


def directory_blob(prefixes: list[str], lengths: list[int]) -> bytes:
    """uint32 count, uint32 prefixOff[count+1], uint32 length[count], utf8 names.

    Shards for one letter are written back to back, so the client recovers an
    offset by prefix-summing the lengths onto the base in the root file - the
    same trick the tile index uses.
    """
    blob = bytearray()
    offsets = np.zeros(len(prefixes) + 1, dtype="<u4")
    for i, prefix in enumerate(prefixes):
        blob += prefix.encode("utf-8")
        offsets[i + 1] = len(blob)
    body = (
        np.array([len(prefixes)], dtype="<u4").tobytes()
        + offsets.tobytes()
        + np.array(lengths, dtype="<u4").tobytes()
        + bytes(blob)
    )
    return gzip.compress(body, 6, mtime=0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--min-score",
        type=float,
        default=0.20,
        help="importance floor; below this an item is not findable at all",
    )
    ap.add_argument(
        "--cap",
        type=int,
        default=500,
        help="max entries per 3-character shard, best score first",
    )
    ap.add_argument("--part-bytes", type=int, default=DEFAULT_PART_BYTES)
    args = ap.parse_args()

    started = time.perf_counter()

    # Country ids have to agree with the ones build_tiles wrote, so read them
    # back rather than deriving them again.
    manifest_path = config.DATA_DIR / "manifest.json"
    countries: list[str] = []
    if manifest_path.exists():
        countries = json.loads(manifest_path.read_text(encoding="utf-8")).get(
            "countries", []
        )
    country_id = {name: i for i, name in enumerate(countries)}
    if not countries:
        print("  no country table in manifest.json - run build_tiles first")

    name_expr = pl.coalesce(
        pl.col("label_en"),
        pl.col("title_en"),
        pl.col("native_label"),
        pl.col("title_any"),
    )
    df = (
        # spread_derived before anything is filtered out: it numbers the items
        # at each borrowed coordinate in score order, and build_tiles does the
        # same on the same full table. Drop rows first and the numbering - and
        # so the coordinates - would diverge from the tiles.
        spread_derived(
            pl.read_parquet(
                config.MASTER_PARQUET,
                columns=[
                    "qid", "lon", "lat", "score", "cat", "country_label",
                    "n_countries",
                    "label_en", "title_en", "native_label", "title_any",
                    "loc_pid", "loc_qid", "loc_pop",
                ],
            )
        )
        .with_columns(
            name_expr.alias("name"),
            # Same rule the tiles use: a country only disambiguates a result if
            # the item is in exactly one.
            pl.when(pl.col("n_countries") > 1)
            .then(None)
            .otherwise(pl.col("country_label"))
            .alias("country_label"),
        )
        .drop_nulls("name")
    )
    named = len(df)
    df = df.filter(pl.col("score") >= args.min_score).sort("score", descending=True)
    print(
        f"indexing {len(df):,} of {named:,} named items "
        f"(score >= {args.min_score}, {100 * len(df) / named:.1f}%)"
    )

    names = df["name"].to_list()
    lons = df["lon"].to_list()
    lats = df["lat"].to_list()
    qids = df["qid"].to_list()
    scores = df["score"].to_list()
    cats = df["cat"].to_list()
    country_labels = df["country_label"].to_list()
    del df

    shards: dict[str, list] = defaultdict(list)
    seen: set[tuple[str, int]] = set()
    for i, raw_name in enumerate(names):
        norm = normalise(raw_name)
        if not norm:
            continue
        entry = [
            raw_name,
            round(lons[i], 5),
            round(lats[i], 5),
            qids[i],
            round(scores[i], 4),
            cats[i],
            country_id.get(country_labels[i], -1),
        ]
        keys = [norm]
        keys.extend(norm.split()[1:MAX_WORDS])
        for key in keys:
            prefix = key[:3]
            if not prefix or (prefix, qids[i]) in seen:
                continue
            seen.add((prefix, qids[i]))
            shards[prefix].append(entry)
        if i and i % 500_000 == 0:
            print(f"  {i:,} items, {len(shards):,} shards")
    del seen

    # Levels 1 and 2 keep only the best entries: that is all a one or two
    # character query can usefully show anyway.
    short: dict[int, dict[str, list]] = {1: defaultdict(list), 2: defaultdict(list)}
    truncated = 0
    for prefix, entries in shards.items():
        entries.sort(key=lambda e: -e[4])
        if len(entries) > args.cap:
            truncated += 1
            del entries[args.cap :]
        for level in (1, 2):
            if len(prefix) > level:
                short[level][prefix[:level]].extend(entries[:TOP_PER_SHORT_SHARD])
    for level in (1, 2):
        for prefix, entries in short[level].items():
            entries.sort(key=lambda e: -e[4])
            shards.setdefault(prefix, [])
            # A 1- or 2-character prefix can also be a whole name ("Y", "Ba"),
            # and those entries are already in `shards` - merge, do not drop.
            merged = shards[prefix] + entries
            merged.sort(key=lambda e: -e[4])
            deduped, taken = [], set()
            for entry in merged:
                if entry[3] in taken:
                    continue
                taken.add(entry[3])
                deduped.append(entry)
                if len(deduped) >= TOP_PER_SHORT_SHARD:
                    break
            shards[prefix] = deduped
    del short

    print(f"  {len(shards):,} shards, {truncated:,} hit the {args.cap}-entry cap")

    # Group by first character so a query needs one small directory, not all of
    # them. Scripts with only a handful of prefixes share one chunk.
    by_letter: dict[str, list[str]] = defaultdict(list)
    for prefix in shards:
        by_letter[prefix[0]].append(prefix)
    letters = {
        letter: sorted(prefixes)
        for letter, prefixes in by_letter.items()
        if len(prefixes) >= MIN_PREFIXES_PER_LETTER
    }
    rare = sorted(
        prefix
        for letter, prefixes in by_letter.items()
        if letter not in letters
        for prefix in prefixes
    )
    if rare:
        letters[RARE] = rare
    print(f"  {len(letters):,} directory chunks ({len(rare):,} prefixes in the shared one)")

    out_dir = config.DATA_DIR
    if (out_dir / "search").exists():
        print("removing the old data/search tree")
        shutil.rmtree(out_dir / "search")
    gone = remove_parts(out_dir, "search")
    if gone:
        print(f"removed {gone} pack part(s) from the previous build")

    root: dict[str, list] = {}
    total_entries = 0
    with PackWriter(out_dir, "search", args.part_bytes) as pack:
        for letter, prefixes in sorted(letters.items()):
            base = pack.total
            lengths = []
            for prefix in prefixes:
                entries = shards[prefix]
                total_entries += len(entries)
                blob = gzip.compress(
                    json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    ),
                    6,
                    mtime=0,
                )
                lengths.append(len(blob))
                pack.add(blob)
            offset, length = pack.add(directory_blob(prefixes, lengths))
            root[letter] = [base, offset, length]
        pack.close()
        pack_info = pack.info()

    (out_dir / "search.json").write_text(
        json.dumps(
            {
                "version": 2,
                "pack": pack_info,
                # letter -> [shard base offset, directory offset, directory length]
                "letters": root,
                "rare": RARE,
                "levels": [1, 2, 3],
                "entries": total_entries,
                "items": len(names),
                "minScore": args.min_score,
                "cap": args.cap,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    root_bytes = (out_dir / "search.json").stat().st_size
    print(
        f"wrote {len(shards):,} shards, {total_entries:,} entries, "
        f"{pack_info['bytes'] / 1e6:.0f} MB in {len(pack_info['parts'])} part(s)"
    )
    print(f"  root file {root_bytes / 1024:.0f} KiB")
    print(f"  {(time.perf_counter() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
