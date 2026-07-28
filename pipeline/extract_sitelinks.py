"""Stage 3 - Wikipedia titles per item, from the wb_items_per_site SQL dump.

The truthy dump carries no sitelinks, so article titles come from Wikidata's
own `wb_items_per_site` table instead:

    INSERT INTO `wb_items_per_site` VALUES (55,3596065,'abwiki','...'),...

Rows are (row_id, item_id, site_id, page_title). That gives, per item, the
title in every language edition, which is what "english title" and "original
title" both need, plus the sitelink count - a decent notability signal in its
own right.

Output: WORK/sitelinks/sitelinks.parquet  (qid, site, title)

Usage:
    python -m pipeline.extract_sitelinks
"""

from __future__ import annotations

import gzip
import re
import time

import pyarrow as pa
import pyarrow.parquet as pq

from pipeline import config

# (row_id, item_id, 'site', 'title') with MySQL backslash escaping. Both string
# bodies use the unrolled-loop form so a failed match cannot backtrack
# exponentially.
SQL_STR = rb"[^'\\]*(?:\\.[^'\\]*)*"
RE_TUPLE = re.compile(rb"\((\d+),(\d+),'(" + SQL_STR + rb")','(" + SQL_STR + rb")'\)")

RE_SQL_ESCAPE = re.compile(rb"\\(.)")
SQL_UNESCAPE = {
    b"0": b"\0",
    b"b": b"\b",
    b"n": b"\n",
    b"r": b"\r",
    b"t": b"\t",
    b"Z": b"\x1a",
    b"\\": b"\\",
    b"'": b"'",
    b'"': b'"',
}

SCHEMA = pa.schema(
    [("qid", pa.uint32()), ("site", pa.string()), ("title", pa.string())]
)
FLUSH_ROWS = 4 << 20


def sql_unescape(raw: bytes) -> str:
    if b"\\" in raw:
        raw = RE_SQL_ESCAPE.sub(lambda m: SQL_UNESCAPE.get(m.group(1), m.group(1)), raw)
    return raw.decode("utf-8", "replace")


def main() -> None:
    path = config.SITELINKS_DUMP
    if not path.exists():
        raise SystemExit(f"missing sitelinks dump: {path}")

    out_path = config.SITELINKS_OUT / "sitelinks.parquet"
    writer = pq.ParquetWriter(out_path, SCHEMA, compression="zstd")
    qids: list[int] = []
    sites: list[str] = []
    titles: list[str] = []
    total = 0
    read_bytes = 0
    started = time.perf_counter()

    def flush() -> None:
        nonlocal total
        if not qids:
            return
        writer.write_table(
            pa.table(
                {
                    "qid": pa.array(qids, pa.uint32()),
                    "site": pa.array(sites, pa.string()),
                    "title": pa.array(titles, pa.string()),
                },
                schema=SCHEMA,
            )
        )
        total += len(qids)
        qids.clear()
        sites.clear()
        titles.clear()

    print(f"reading {path.name}")
    with gzip.open(path, "rb") as fh:
        for line in fh:
            read_bytes += len(line)
            if not line.startswith(b"INSERT INTO"):
                continue
            for m in RE_TUPLE.finditer(line):
                qids.append(int(m.group(2)))
                sites.append(m.group(3).decode("ascii", "replace"))
                titles.append(sql_unescape(m.group(4)))
            if len(qids) >= FLUSH_ROWS:
                flush()
                elapsed = time.perf_counter() - started
                print(
                    f"  {total:12,d} sitelinks  {read_bytes / 2**30:5.1f} GiB"
                    f"  {elapsed / 60:5.1f} min",
                    flush=True,
                )
    flush()
    writer.close()
    print(
        f"wrote {out_path}: {total:,} sitelinks in "
        f"{(time.perf_counter() - started) / 60:.1f} min"
    )


if __name__ == "__main__":
    main()
