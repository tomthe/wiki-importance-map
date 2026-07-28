"""Stage 2 - turn the two importance datasets into parquet keyed by Q-number.

danker  PageRank over the link graph of *all* Wikipedia language editions
        (bag-of-links: a link present in 254 editions counts 254 times), so it
        measures global prominence rather than anglophone prominence.
        TSV, no header: "Q565<TAB>81605.188".
        Note danker uses (1 - damping) instead of (1 - damping)/N, so the
        scores are not probabilities - only the ranking is meaningful.

QRank   Twelve months of aggregated Wikimedia pageviews joined to Q-ids.
        CSV with header: "Entity,QRank".

Usage:
    python -m pipeline.extract_ranks
"""

from __future__ import annotations

import bz2
import gzip
import io
import time

import polars as pl

from pipeline import config


def _read_danker() -> pl.DataFrame:
    path = config.DANKER_DUMP
    if path is None or not path.exists():
        raise SystemExit("no danker *.allwiki.links.rank.bz2 found in the dump dir")
    print(f"reading {path.name}")
    started = time.perf_counter()
    try:
        import indexed_bzip2 as ibz2

        with ibz2.open(str(path), parallelization=0) as fh:
            raw = fh.read()
    except ImportError:
        with bz2.open(path, "rb") as fh:
            raw = fh.read()
    df = pl.read_csv(
        io.BytesIO(raw),
        separator="\t",
        has_header=False,
        new_columns=["entity", "pagerank"],
        schema_overrides={"entity": pl.String, "pagerank": pl.Float64},
    )
    print(f"  {len(df):,} rows in {time.perf_counter() - started:.1f}s")
    return df


def _read_qrank() -> pl.DataFrame:
    path = config.QRANK_DUMP
    if path is None or not path.exists():
        raise SystemExit("no qrank*.gz found in the dump dir")
    print(f"reading {path.name}")
    started = time.perf_counter()
    with gzip.open(path, "rb") as fh:
        raw = fh.read()
    df = pl.read_csv(
        io.BytesIO(raw),
        has_header=True,
        schema_overrides={"Entity": pl.String, "QRank": pl.Int64},
    ).rename({"Entity": "entity", "QRank": "qrank"})
    print(f"  {len(df):,} rows in {time.perf_counter() - started:.1f}s")
    return df


def _to_qid(df: pl.DataFrame, value_col: str) -> pl.DataFrame:
    """Q-prefixed string -> uint32, dropping anything that is not an item."""
    return (
        df.filter(pl.col("entity").str.starts_with("Q"))
        .with_columns(pl.col("entity").str.slice(1).cast(pl.UInt32, strict=False).alias("qid"))
        .drop_nulls("qid")
        .select("qid", value_col)
        .unique(subset="qid", keep="first")
    )


def main() -> None:
    pagerank = _to_qid(_read_danker(), "pagerank")
    pagerank.write_parquet(config.RANKS_OUT / "pagerank.parquet", compression="zstd")
    print(f"wrote pagerank.parquet: {len(pagerank):,} items")

    qrank = _to_qid(_read_qrank(), "qrank")
    qrank.write_parquet(config.RANKS_OUT / "qrank.parquet", compression="zstd")
    print(f"wrote qrank.parquet: {len(qrank):,} items")


if __name__ == "__main__":
    main()
