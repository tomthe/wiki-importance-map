"""Stage 1 - pull the geo-relevant slice out of the Wikidata truthy dump.

`latest-truthy.nt.bz2` is ~43 GB compressed and ~1 TB of N-Triples. A bzip2
stream cannot be split by byte offset, so the layout is one reader plus a pool
of parsers:

  reader   indexed_bzip2 decodes bz2 blocks across all cores (~500 MB/s here)
           and hands line-aligned chunks to the workers over one queue each.
  workers  scan a chunk with regexes anchored on distinctive predicate
           literals. Lines we do not care about - about 97% of the dump - are
           rejected inside the C matcher and never become Python objects.

Each worker writes its own parquet shards, so nothing is merged in memory:

  coords_NN.parquet       qid, lon, lat            (P625, Earth only)
  claims_item_NN.parquet  qid, pid, value          (P31 P279 P17 P131 P37,
                                                    P19 P20 P106 P159 P276
                                                    P504 P532 P551 P840 P937)
  claims_num_NN.parquet   qid, pid, value          (P1082 P2044)
  claims_time_NN.parquet  qid, pid, value          (P571 P569 P570)
  claims_iri_NN.parquet   qid, pid, value          (P18 P856)
  claims_mono_NN.parquet  qid, pid, value, lang    (P1705 P1448)
  claims_str_NN.parquet   qid, pid, value          (P424)
  labels_en_NN.parquet    qid, label
  descr_en_NN.parquet     qid, descr

Usage:
    python -m pipeline.extract_truthy                 # full run
    python -m pipeline.extract_truthy --bench FILE    # parse a plain .nt sample
    python -m pipeline.extract_truthy --limit-gb 20   # stop early (smoke test)
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import queue as queue_mod
import re
import sys
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from pipeline import config
from pipeline.ntutil import unescape

# --- line anatomy ----------------------------------------------------------
# <http://www.wikidata.org/entity/Q42> <predicate> <object> .
SUBJECT_PREFIX = b"<http://www.wikidata.org/entity/Q"
SUBJECT_PREFIX_LEN = len(SUBJECT_PREFIX)

# Every pattern starts with a literal distinctive enough that CPython's regex
# engine can memchr its way to the candidates instead of walking every line.
#
# QUOTED is the unrolled-loop form of "an escaped N-Triples literal body". The
# obvious spelling, (?:[^"\\]+|\\.)*, is ambiguous - a run of ordinary
# characters can be split across iterations in exponentially many ways - so
# every label that fails the trailing @en test, which is most of the dump,
# would backtrack catastrophically. This form has exactly one parse.
QUOTED = rb'[^"\\]*(?:\\.[^"\\]*)*'

RE_COORD = re.compile(rb'/prop/direct/P625> "Point\(([-+0-9.eE]+) ([-+0-9.eE]+)\)"')
# The second block are the *derived location* properties: an item that has no
# P625 of its own but points at something that does. No human in Wikidata has a
# coordinate, but over half have a place of birth. P106 (occupation) rides along
# because it is what gives those people a subcategory. The trailing "> " in the
# pattern anchors each alternative, so no id can be a prefix of another one.
RE_ITEM = re.compile(
    rb"/prop/direct/P(17|19|20|31|37|106|131|159|276|279|504|532|551|840|937)>"
    rb" <http://www\.wikidata\.org/entity/Q(\d+)> \."
)
RE_NUM = re.compile(rb'/prop/direct/P(1082|2044)> "([-+0-9.eE]+)"')
# 571 inception, 569 date of birth, 570 date of death.
RE_TIME = re.compile(rb'/prop/direct/P(569|570|571)> "([^"]+)"')
RE_IRI = re.compile(rb"/prop/direct/P(18|856)> <([^>]*)>")
RE_MONO = re.compile(rb'/prop/direct/P(1705|1448)> "(' + QUOTED + rb')"@([\w-]+)')
RE_STR = re.compile(rb'/prop/direct/P(424)> "(' + QUOTED + rb')" \.')
RE_LABEL_EN = re.compile(rb'/2000/01/rdf-schema#label> "(' + QUOTED + rb')"@en \.')
RE_DESCR_EN = re.compile(
    rb'<http://schema\.org/description> "(' + QUOTED + rb')"@en \.'
)

CHUNK_BYTES = 32 << 20
FLUSH_ROWS = 1 << 20

SCHEMAS = {
    "coords": pa.schema(
        [("qid", pa.uint32()), ("lon", pa.float64()), ("lat", pa.float64())]
    ),
    "claims_item": pa.schema(
        [("qid", pa.uint32()), ("pid", pa.uint32()), ("value", pa.uint32())]
    ),
    "claims_num": pa.schema(
        [("qid", pa.uint32()), ("pid", pa.uint32()), ("value", pa.float64())]
    ),
    "claims_time": pa.schema(
        [("qid", pa.uint32()), ("pid", pa.uint32()), ("value", pa.string())]
    ),
    "claims_iri": pa.schema(
        [("qid", pa.uint32()), ("pid", pa.uint32()), ("value", pa.string())]
    ),
    "claims_mono": pa.schema(
        [
            ("qid", pa.uint32()),
            ("pid", pa.uint32()),
            ("value", pa.string()),
            ("lang", pa.string()),
        ]
    ),
    "claims_str": pa.schema(
        [("qid", pa.uint32()), ("pid", pa.uint32()), ("value", pa.string())]
    ),
    "labels_en": pa.schema([("qid", pa.uint32()), ("label", pa.string())]),
    "descr_en": pa.schema([("qid", pa.uint32()), ("descr", pa.string())]),
}


class ShardWriter:
    """Buffers columnar rows and flushes them to one parquet file per table."""

    def __init__(self, outdir: Path, worker_id: int):
        self.outdir = outdir
        self.worker_id = worker_id
        self.cols: dict[str, list[list]] = {
            name: [[] for _ in schema] for name, schema in SCHEMAS.items()
        }
        self.writers: dict[str, pq.ParquetWriter] = {}
        self.counts = dict.fromkeys(SCHEMAS, 0)

    def add(self, table: str, *values) -> None:
        cols = self.cols[table]
        for col, value in zip(cols, values):
            col.append(value)
        if len(cols[0]) >= FLUSH_ROWS:
            self.flush(table)

    def flush(self, table: str) -> None:
        cols = self.cols[table]
        if not cols[0]:
            return
        schema = SCHEMAS[table]
        batch = pa.table(
            {field.name: pa.array(col, field.type) for field, col in zip(schema, cols)},
            schema=schema,
        )
        writer = self.writers.get(table)
        if writer is None:
            path = self.outdir / f"{table}_{self.worker_id:02d}.parquet"
            writer = pq.ParquetWriter(path, schema, compression="zstd")
            self.writers[table] = writer
        writer.write_table(batch)
        self.counts[table] += len(cols[0])
        for col in cols:
            col.clear()

    def close(self) -> dict[str, int]:
        for table in SCHEMAS:
            self.flush(table)
        for writer in self.writers.values():
            writer.close()
        return self.counts


def subject_qid(chunk: bytes, match_start: int) -> int:
    """Q-number of the line containing `match_start`, or -1 if not an item."""
    line_start = chunk.rfind(b"\n", 0, match_start) + 1
    if chunk[line_start : line_start + SUBJECT_PREFIX_LEN] != SUBJECT_PREFIX:
        return -1  # lexemes, properties, Special:EntityData subjects
    end = chunk.find(b">", line_start + SUBJECT_PREFIX_LEN)
    if end < 0:
        return -1
    digits = chunk[line_start + SUBJECT_PREFIX_LEN : end]
    return int(digits) if digits.isdigit() else -1


def parse_chunk(chunk: bytes, out: ShardWriter) -> None:
    """Run every extraction pattern over one line-aligned chunk."""
    add = out.add

    # Coordinates. Values on another globe serialise as
    # "<http://www.wikidata.org/entity/Q308> Point(...)", so requiring the
    # literal to start with Point( keeps us on Earth.
    for m in RE_COORD.finditer(chunk):
        qid = subject_qid(chunk, m.start())
        if qid < 0:
            continue
        try:
            lon = float(m.group(1))
            lat = float(m.group(2))
        except ValueError:
            continue
        if -180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0:
            add("coords", qid, lon, lat)

    for m in RE_ITEM.finditer(chunk):
        qid = subject_qid(chunk, m.start())
        if qid >= 0:
            add("claims_item", qid, int(m.group(1)), int(m.group(2)))

    for m in RE_NUM.finditer(chunk):
        qid = subject_qid(chunk, m.start())
        if qid < 0:
            continue
        try:
            add("claims_num", qid, int(m.group(1)), float(m.group(2)))
        except ValueError:
            continue

    for m in RE_TIME.finditer(chunk):
        qid = subject_qid(chunk, m.start())
        if qid >= 0:
            add("claims_time", qid, int(m.group(1)), m.group(2).decode("ascii", "replace"))

    for m in RE_IRI.finditer(chunk):
        qid = subject_qid(chunk, m.start())
        if qid >= 0:
            add("claims_iri", qid, int(m.group(1)), unescape(m.group(2)))

    for m in RE_MONO.finditer(chunk):
        qid = subject_qid(chunk, m.start())
        if qid >= 0:
            add(
                "claims_mono",
                qid,
                int(m.group(1)),
                unescape(m.group(2)),
                m.group(3).decode("ascii", "replace"),
            )

    for m in RE_STR.finditer(chunk):
        qid = subject_qid(chunk, m.start())
        if qid >= 0:
            add("claims_str", qid, int(m.group(1)), unescape(m.group(2)))

    for m in RE_LABEL_EN.finditer(chunk):
        qid = subject_qid(chunk, m.start())
        if qid >= 0:
            add("labels_en", qid, unescape(m.group(1)))

    for m in RE_DESCR_EN.finditer(chunk):
        qid = subject_qid(chunk, m.start())
        if qid >= 0:
            add("descr_en", qid, unescape(m.group(1)))


def worker_main(worker_id: int, outdir: str, chunks: mp.Queue, results: mp.Queue) -> None:
    out = ShardWriter(Path(outdir), worker_id)
    parsed = 0
    while True:
        chunk = chunks.get()
        if chunk is None:
            break
        parse_chunk(chunk, out)
        parsed += len(chunk)
    counts = out.close()
    results.put((worker_id, parsed, counts))


def run(n_workers: int, limit_gb: float | None) -> None:
    import indexed_bzip2 as ibz2

    outdir = config.TRUTHY_OUT
    for stale in outdir.glob("*.parquet"):
        stale.unlink()

    ctx = mp.get_context("spawn")
    results: mp.Queue = ctx.Queue()
    queues = [ctx.Queue(maxsize=3) for _ in range(n_workers)]
    procs = [
        ctx.Process(
            target=worker_main, args=(i, str(outdir), queues[i], results), daemon=True
        )
        for i in range(n_workers)
    ]
    for p in procs:
        p.start()

    limit = None if limit_gb is None else int(limit_gb * (1 << 30))
    print(f"reading {config.TRUTHY_DUMP} with {n_workers} parser workers", flush=True)
    started = time.perf_counter()
    total = 0
    tail = b""
    index = 0

    with ibz2.open(str(config.TRUTHY_DUMP), parallelization=0) as fh:
        while True:
            buf = fh.read(CHUNK_BYTES)
            if not buf:
                break
            if tail:
                buf = tail + buf
            cut = buf.rfind(b"\n") + 1
            if cut == 0:  # pathological: no newline in 32 MB
                tail = buf
                continue
            tail = buf[cut:]
            queues[index % n_workers].put(buf[:cut])
            index += 1
            total += cut
            if index % 64 == 0:
                elapsed = time.perf_counter() - started
                print(
                    f"  {total / 2**30:8.1f} GiB  {total / 1e6 / elapsed:6.0f} MB/s"
                    f"  {elapsed / 60:6.1f} min",
                    flush=True,
                )
            if limit is not None and total >= limit:
                print("  reached --limit-gb, stopping read", flush=True)
                break
    if tail.strip():
        queues[index % n_workers].put(tail)

    for q in queues:
        q.put(None)

    summary: dict[str, int] = dict.fromkeys(SCHEMAS, 0)
    for _ in procs:
        worker_id, parsed, counts = results.get()
        for table, n in counts.items():
            summary[table] += n
    for p in procs:
        p.join()

    elapsed = time.perf_counter() - started
    print(f"\nread {total / 2**30:.1f} GiB in {elapsed / 60:.1f} min")
    for table, n in summary.items():
        print(f"  {table:14s} {n:14,d} rows")


def bench(path: Path) -> None:
    """Parse a plain-text .nt sample to measure MB/s and sanity-check output."""
    raw = path.read_bytes()
    out = ShardWriter(config.WORK_DIR / "bench", 0)
    (config.WORK_DIR / "bench").mkdir(exist_ok=True)
    started = time.perf_counter()
    parse_chunk(raw, out)
    elapsed = time.perf_counter() - started
    counts = {t: len(c[0]) for t, c in out.cols.items()}
    print(f"parsed {len(raw) / 1e6:.0f} MB in {elapsed:.1f}s -> {len(raw) / 1e6 / elapsed:.0f} MB/s per core")
    for table, n in counts.items():
        print(f"  {table:14s} {n:10,d}")
    for table in ("coords", "labels_en", "claims_item"):
        cols = out.cols[table]
        if cols[0]:
            print(f"  sample {table}: {[c[0] for c in cols]}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=14)
    ap.add_argument("--limit-gb", type=float, default=None)
    ap.add_argument("--bench", type=Path, default=None)
    args = ap.parse_args()

    if args.bench:
        bench(args.bench)
        return
    if not config.TRUTHY_DUMP.exists():
        sys.exit(f"missing dump: {config.TRUTHY_DUMP}")
    run(args.workers, args.limit_gb)


if __name__ == "__main__":
    main()
