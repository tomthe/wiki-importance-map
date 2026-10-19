"""Stage 1 - Wikivoyage pageviews, month by month, out of the pageview dumps.

Input: pageviews-YYYYMM-user.bz2 from dumps.wikimedia.org/other/pageview_complete/
monthly/, one per month. The `user` files already exclude spiders and the
traffic Wikimedia classifies as automated, which is the whole reason to use them
rather than the per-article API's `all-agents`.

Each line is

    en.wikivoyage Potsdam 28315 mobile-web 1234 A40B37C51...

project, title, page id, access method, monthly total, daily breakdown. A file
is about 6 GB of bz2 and 30 GB of text, of which Wikivoyage is well under one
percent - but the file is sorted by project, so every edition's Wikivoyage rows
are one contiguous run. A chunk is skipped with a single `find`, which runs at
memory speed, and the regex only ever sees the runs that matter.

The months are the point. Separate counts per article and month are what let
the map ask where people are looking in February, and a ski resort and a Greek
island have opposite answers - so nothing is summed here, and build_master
decides how the years combine.

Which months
------------
Not the latest ones. From November 2024 on, Wikivoyage's "user" traffic - the
class that is supposed to be people - more than tripled (English: about 5M
views a month through 2023 and most of 2024, then 15-30M), and the excess is
crawlers that the automated-traffic filter does not catch. It is not spikes
that a cap could remove: it is months-long, it lands on every article at once,
and it is seasonal itself, so in 2026 data Rovaniemi "peaks" in September and
Zermatt in September too. Before it, the seasons are where you would expect:
Rovaniemi in December, Zermatt in December and January, Mykonos in summer.

So the map uses the two years before it, November 2022 to October 2024: every
calendar month twice, which halves the noise on small articles and lets
build_master throw out a month that only one of the two years saw.

Despiking
---------
Within a month, a single day hammered by a script is still common. `despiked`
caps every day at three times the month's median day, so that day counts as an
ordinary one; `views` is the raw total, kept for comparison.

Output: WORK/pageviews.parquet
    (lang, title, access, year, month 1..12, views, despiked)

Usage:
    python -m pipeline.voyage.extract_pageviews [--workers 12]
"""

from __future__ import annotations

import argparse
import functools
import re
import time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import polars as pl

from pipeline.voyage import config

print = functools.partial(print, flush=True)

CHUNK_BYTES = 64 << 20
NEEDLE = b".wikivoyage "
# project, title, page id, access, count, daily counts. Titles never contain
# a space.
RE_LINE = re.compile(rb"^([a-z0-9_-]+)\.wikivoyage (\S+) \S+ (\S+) (\d+) (\S*)", re.M)
RE_DAY = re.compile(rb"[A-_](\d+)")
RE_FILE = re.compile(r"pageviews-(\d{4})(\d{2})-user\.bz2$")


def despiked(days: bytes, total: int) -> int:
    """The month's views with each day capped at three times the month's
    median day (and never below 10): one day of a crawler hammering a page
    becomes one ordinary day. Days without views count as zero."""
    values = sorted(int(v) for v in RE_DAY.findall(days))
    if len(values) < 2:
        return min(total, 10)
    values = [0] * (30 - len(values)) + values if len(values) < 30 else values
    median = values[len(values) // 2]
    cap = max(3 * median, 10)
    return sum(min(v, cap) for v in values)


def scan_month(path: str, threads: int) -> tuple[str, dict, float]:
    """(lang, title, access) -> [views, despiked views] for one monthly file.
    Runs in a worker."""
    import indexed_bzip2 as ibz2

    started = time.perf_counter()
    counts: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
    tail = b""
    with ibz2.open(path, parallelization=threads) as fh:
        while True:
            buf = fh.read(CHUNK_BYTES)
            if not buf:
                break
            if tail:
                buf = tail + buf
            cut = buf.rfind(b"\n") + 1
            tail = buf[cut:]
            first = buf.find(NEEDLE, 0, cut)
            if first < 0:
                continue
            # The run of Wikivoyage rows inside this chunk, widened to whole lines.
            start = buf.rfind(b"\n", 0, first) + 1
            last = buf.rfind(NEEDLE, 0, cut)
            stop = buf.find(b"\n", last)
            stop = cut if stop < 0 else stop + 1
            for m in RE_LINE.finditer(buf, start, stop):
                key = (
                    m.group(1).decode("ascii"),
                    m.group(2).decode("utf-8", "replace"),
                    m.group(3).decode("ascii"),
                )
                total = int(m.group(4))
                slot = counts[key]
                slot[0] += total
                slot[1] += despiked(m.group(5), total) if total > 30 else total
    return path, dict(counts), time.perf_counter() - started


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=12, help="months scanned at once")
    ap.add_argument("--threads", type=int, default=8, help="bz2 threads per month")
    args = ap.parse_args()

    files = sorted(config.PAGEVIEW_DIR.glob("pageviews-*-user.bz2"))
    if not files:
        raise SystemExit(f"no pageviews-YYYYMM-user.bz2 in {config.PAGEVIEW_DIR}")
    months = {}
    for f in files:
        m = RE_FILE.search(f.name)
        months[str(f)] = (int(m.group(1)), int(m.group(2)))
    per_month = defaultdict(int)
    for _, mm in months.values():
        per_month[mm] += 1
    print(f"{len(files)} monthly files: {', '.join(f.name[10:16] for f in files)}")
    if len(set(per_month.values())) != 1 or len(per_month) != 12:
        print(
            "  WARNING: calendar months are not equally covered "
            f"({dict(sorted(per_month.items()))}); seasonality will be skewed"
        )

    started = time.perf_counter()
    frames = []
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        jobs = [pool.submit(scan_month, str(f), args.threads) for f in files]
        for job in jobs:
            path, counts, seconds = job.result()
            year, month = months[path]
            frame = pl.DataFrame(
                {
                    "lang": [k[0] for k in counts],
                    "title": [k[1] for k in counts],
                    "access": [k[2] for k in counts],
                    "views": [v[0] for v in counts.values()],
                    "despiked": [v[1] for v in counts.values()],
                },
                schema={"lang": pl.String, "title": pl.String, "access": pl.String,
                        "views": pl.Int64, "despiked": pl.Int64},
            ).with_columns(
                pl.lit(year, dtype=pl.UInt16).alias("year"),
                pl.lit(month, dtype=pl.UInt8).alias("month"),
            )
            frames.append(frame)
            print(
                f"  {Path(path).name}: {len(counts):,} titles, "
                f"{frame['views'].sum():,} views, {seconds / 60:.1f} min"
            )

    out = pl.concat(frames).sort("lang", "title", "access", "year", "month")
    out.write_parquet(config.PAGEVIEWS_PARQUET)
    by_lang = out.group_by("lang").agg(pl.col("views").sum()).sort("views", descending=True)
    print(f"\nwrote {len(out):,} rows to {config.PAGEVIEWS_PARQUET}")
    print("  views by edition: " + ", ".join(
        f"{r['lang']} {r['views'] / 1e6:.1f}M" for r in by_lang.head(12).iter_rows(named=True)
    ))
    print(f"  {(time.perf_counter() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
