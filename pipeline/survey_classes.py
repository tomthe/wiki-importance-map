"""Helper - list the most common P31 classes among geolocated items.

The category taxonomy in taxonomy.py is curated by hand, but the list of
classes worth curating should come from the data rather than from guesswork.
This prints the top classes with their English labels and item counts.

Usage:
    python -m pipeline.survey_classes --top 400 > classes.txt
"""

from __future__ import annotations

import argparse

import duckdb

from pipeline import config


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=400)
    args = ap.parse_args()

    con = duckdb.connect()
    con.execute("PRAGMA threads=32")
    # Forward slashes: DuckDB's globbing does not like Windows backslashes.
    truthy = str(config.TRUTHY_OUT).replace("\\", "/")

    rows = con.execute(
        f"""
        WITH geo AS (
            SELECT DISTINCT qid FROM read_parquet('{truthy}/coords_*.parquet')
        ),
        inst AS (
            SELECT c.value AS class_qid, count(*) AS n
            FROM read_parquet('{truthy}/claims_item_*.parquet') c
            JOIN geo ON geo.qid = c.qid
            WHERE c.pid = 31
            GROUP BY 1
        ),
        labels AS (
            SELECT qid, any_value(label) AS label
            FROM read_parquet('{truthy}/labels_en_*.parquet')
            GROUP BY qid
        )
        SELECT i.class_qid, coalesce(l.label, '?') AS label, i.n
        FROM inst i LEFT JOIN labels l ON l.qid = i.class_qid
        ORDER BY i.n DESC
        LIMIT {args.top}
        """
    ).fetchall()

    total = sum(r[2] for r in rows)
    print(f"# top {len(rows)} P31 classes among geolocated items ({total:,} claims)")
    for class_qid, label, n in rows:
        print(f"Q{class_qid}\t{n}\t{label}")


if __name__ == "__main__":
    main()
