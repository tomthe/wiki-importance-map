"""Run build_master's SQL against tiny synthetic shards.

The real join takes minutes on hundreds of millions of rows, so this builds a
handful of rows with the same schemas and checks the parts that are easy to get
wrong: picking one coordinate out of several, the label/title fallbacks, the
native-language title, the category priority rule, and the score maths.

    python -m tests.test_build_master
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import polars as pl

REPO = Path(__file__).resolve().parent.parent

# Items with a coordinate of their own:
# qid 1 London: two coordinates, enwiki article, a capital and a city
# qid 2 München: native label + dewiki title, no enwiki article
# qid 3 a nameless thing with no rank at all
# qid 4 a castle that is also a building - priority must pick castle
# qid 5 a river in two countries - the country must not be guessed at
#
# Items with no coordinate that have to borrow one:
# qid 6  a politician born in London          -> P19, one hop, cat People
# qid 7  someone who only has a place of death -> P20 is the fallback
# qid 8  born in qid 10, which has no coordinate but one unambiguous P131
# qid 9  born in London but has no article anywhere -> below the sitelink floor
# qid 10 the coordinate-less birthplace itself, admin-inside München
# qid 11 born in qid 12, whose P131 is ambiguous -> must NOT be placed
# qid 12 a coordinate-less place with two P131 values
COORDS = [
    (1, -0.1276, 51.5072),
    (1, -0.1280, 51.5080),
    (1, -0.1278, 51.5076),
    (2, 11.5820, 48.1351),
    (3, 100.0, 13.0),
    (4, 8.0, 47.0),
    (5, 12.0, 48.0),
]
CLAIMS_ITEM = [
    (1, 31, 5119), (1, 31, 515), (1, 17, 145),   # capital, city, country=UK
    (2, 31, 515), (2, 17, 183),                  # city, country=Germany
    (3, 31, 99999999),                           # unmapped class
    (4, 31, 23413), (4, 31, 41176),              # castle + building
    (23413, 279, 41176),                         # castle subclass of building
    (5, 17, 183), (5, 17, 145),                  # two countries, no single answer
    # --- derived locations -------------------------------------------------
    (6, 31, 5), (6, 106, 82955), (6, 106, 36180),  # human; politician + writer
    (6, 19, 1),                                    # born in London
    (7, 31, 5), (7, 20, 2),                        # human, only a place of death
    (8, 31, 5), (8, 19, 10),                       # born in a coordinate-less place
    (9, 31, 5), (9, 19, 1),                        # born in London, but no article
    (10, 31, 532), (10, 131, 2),                   # village, inside München
    (11, 31, 5), (11, 19, 12),
    (12, 31, 532), (12, 131, 1), (12, 131, 2),     # two parents: no single answer
]
LABELS = [
    (1, "London"), (2, "Munich"), (4, "Neuschwanstein"), (5, "Danube"),
    (145, "United Kingdom"), (183, "Germany"),
    (515, "city"), (5119, "capital"), (23413, "castle"), (41176, "building"),
    (5, "human"), (532, "village"), (82955, "politician"), (36180, "writer"),
    (6, "A Politician"), (7, "A Dead Person"), (8, "A Villager"),
    (9, "An Unread Person"), (10, "Kleinhausen"), (11, "An Ambiguous Person"),
    (12, "Zweidorf"),
]
SITELINKS = [
    (1, "enwiki", "London"), (1, "dewiki", "London"), (1, "frwiki", "Londres"),
    (1, "commonswiki", "Category:London"),   # must not count as a language edition
    (2, "dewiki", "München"), (2, "frwiki", "Munich"),
    (4, "enwiki", "Neuschwanstein Castle"),
    (6, "enwiki", "A Politician"), (6, "dewiki", "Ein Politiker"),
    (7, "enwiki", "A Dead Person"),
    (8, "enwiki", "A Villager"),
    (11, "enwiki", "An Ambiguous Person"),
    # qid 9 deliberately has none.
]


def write(work: Path) -> None:
    truthy = work / "truthy"
    ranks = work / "ranks"
    sites = work / "sitelinks"
    for d in (truthy, ranks, sites):
        d.mkdir(parents=True, exist_ok=True)

    pl.DataFrame(
        COORDS, schema={"qid": pl.UInt32, "lon": pl.Float64, "lat": pl.Float64},
        orient="row",
    ).write_parquet(truthy / "coords_00.parquet")

    pl.DataFrame(
        CLAIMS_ITEM, schema={"qid": pl.UInt32, "pid": pl.UInt32, "value": pl.UInt32},
        orient="row",
    ).write_parquet(truthy / "claims_item_00.parquet")

    pl.DataFrame(
        [(1, 1082, 8900000.0), (2, 1082, 1500000.0), (4, 2044, 800.0)],
        schema={"qid": pl.UInt32, "pid": pl.UInt32, "value": pl.Float64},
        orient="row",
    ).write_parquet(truthy / "claims_num_00.parquet")

    pl.DataFrame(
        [
            (4, 571, "1869-01-01T00:00:00Z"),
            (6, 569, "1879-03-14T00:00:00Z"),   # date of birth
            (6, 570, "1955-04-18T00:00:00Z"),   # date of death
        ],
        schema={"qid": pl.UInt32, "pid": pl.UInt32, "value": pl.String},
        orient="row",
    ).write_parquet(truthy / "claims_time_00.parquet")

    pl.DataFrame(
        [(1, 856, "https://london.gov.uk"), (4, 18, "http://commons/x.jpg")],
        schema={"qid": pl.UInt32, "pid": pl.UInt32, "value": pl.String},
        orient="row",
    ).write_parquet(truthy / "claims_iri_00.parquet")

    pl.DataFrame(
        [(2, 1705, "München", "de")],
        schema={
            "qid": pl.UInt32, "pid": pl.UInt32, "value": pl.String, "lang": pl.String
        },
        orient="row",
    ).write_parquet(truthy / "claims_mono_00.parquet")

    pl.DataFrame(
        [(183, 424, "de")],
        schema={"qid": pl.UInt32, "pid": pl.UInt32, "value": pl.String},
        orient="row",
    ).write_parquet(truthy / "claims_str_00.parquet")

    pl.DataFrame(
        LABELS, schema={"qid": pl.UInt32, "label": pl.String}, orient="row"
    ).write_parquet(truthy / "labels_en_00.parquet")

    pl.DataFrame(
        [(1, "Capital of England"), (2, "City in Bavaria")],
        schema={"qid": pl.UInt32, "descr": pl.String}, orient="row",
    ).write_parquet(truthy / "descr_en_00.parquet")

    pl.DataFrame(
        [(1, 1000.0), (2, 500.0), (4, 20.0), (6, 300.0), (7, 10.0), (8, 5.0),
         (11, 4.0)],
        schema={"qid": pl.UInt32, "pagerank": pl.Float64}, orient="row",
    ).write_parquet(ranks / "pagerank.parquet")

    pl.DataFrame(
        [(1, 900000), (2, 400000), (4, 5000), (6, 200000), (7, 900), (8, 100),
         (11, 90)],
        schema={"qid": pl.UInt32, "qrank": pl.Int64}, orient="row",
    ).write_parquet(ranks / "qrank.parquet")

    pl.DataFrame(
        SITELINKS, schema={"qid": pl.UInt32, "site": pl.String, "title": pl.String},
        orient="row",
    ).write_parquet(sites / "sitelinks.parquet")


def run(work: Path, *args: str) -> pl.DataFrame:
    env = dict(os.environ, WIKIMAP_WORK=str(work), PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "pipeline.build_master", *args],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=REPO,
    )
    if proc.returncode != 0:
        print(proc.stdout[-3000:])
        print(proc.stderr[-3000:])
        sys.exit("build_master failed")
    return pl.read_parquet(work / "articles.parquet")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    failures: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "work"
        write(work)
        # The floor is exercised separately at the end; this pass is about the
        # join, so nothing is allowed to drop out from under the assertions.
        df = run(work, "--derived-min-score", "0.0")
        rows = {r["qid"]: r for r in df.to_dicts()}

        def check(name: str, ok: bool, detail: str = "") -> None:
            print(f"{'ok  ' if ok else 'FAIL'} {name}{'' if ok else '  ' + detail}")
            if not ok:
                failures.append(name)

        # 5 with their own coordinates, plus 6, 7 and 8 borrowing one. Not 9
        # (no article anywhere) and not 11 (its birthplace has two parents).
        check("one row per mapped item", len(df) == 8, f"got {len(df)}")

        london = rows[1]
        check("multi-coordinate item keeps a real value",
              (london["lon"], london["lat"]) in [(c[1], c[2]) for c in COORDS if c[0] == 1],
              str((london["lon"], london["lat"])))
        check("n_coords counts the duplicates", london["n_coords"] == 3,
              str(london["n_coords"]))
        check("english title from enwiki", london["title_en"] == "London")
        check("country label resolved", london["country_label"] == "United Kingdom",
              str(london["country_label"]))
        check("single country counted as one", london["n_countries"] == 1,
              str(london["n_countries"]))
        check("commonswiki excluded from sitelink count", london["n_sitelinks"] == 3,
              str(london["n_sitelinks"]))
        check("capital beats city on priority", london["sub"] == 1,
              f"cat={london['cat']} sub={london['sub']}")
        check("description carried through",
              london["descr_en"] == "Capital of England")
        check("population carried through", london["population"] == 8900000.0)

        munich = rows[2]
        check("native label kept", munich["native_label"] == "München",
              str(munich["native_label"]))
        check("native title from the matching wiki", munich["title_native"] == "München",
              str(munich["title_native"]))
        check("no enwiki article stays null", munich["title_en"] is None,
              str(munich["title_en"]))

        nameless = rows[3]
        check("item with no rank scores zero", nameless["score"] == 0.0,
              str(nameless["score"]))
        check("unmapped class falls back to Other", nameless["cat"] == 0,
              str(nameless["cat"]))

        castle = rows[4]
        check("castle beats building on priority",
              castle["cat"] == 5 and castle["sub"] > 0,
              f"cat={castle['cat']} sub={castle['sub']}")

        # build_tiles blanks the name when this is above 1, because P17 has no
        # single answer for a river and an arbitrary pick reads as a fact.
        river = rows[5]
        check("several countries are counted, not collapsed",
              river["n_countries"] == 2, str(river["n_countries"]))

        check("top item scores highest",
              london["score"] == max(r["score"] for r in rows.values()),
              str(london["score"]))
        check("normalised signals are in range",
              all(0.0 <= (r["pr_norm"] or 0) <= 1.0 for r in rows.values()))

        # --- derived locations ------------------------------------------
        from pipeline.taxonomy import CATEGORY_ID, SUBCATEGORY_ID

        check("an item with its own coordinate is not marked derived",
              london["loc_pid"] == 0 and london["loc_qid"] is None,
              f"{london['loc_pid']} {london['loc_qid']}")

        politician = rows.get(6)
        check("a person with a birthplace is on the map", politician is not None)
        if politician:
            check("placed at the birthplace's real coordinate",
                  (politician["lon"], politician["lat"]) == (london["lon"], london["lat"]),
                  f"{politician['lon']},{politician['lat']}")
            check("records which property placed it", politician["loc_pid"] == 19,
                  str(politician["loc_pid"]))
            check("records the place it borrowed from",
                  politician["loc_qid"] == 1 and politician["loc_label"] == "London",
                  f"{politician['loc_qid']} {politician['loc_label']}")
            check("inherits the country of the place, not its own",
                  politician["country_label"] == "United Kingdom",
                  str(politician["country_label"]))
            check("categorised as a person",
                  politician["cat"] == CATEGORY_ID["People"],
                  str(politician["cat"]))
            # Politician (100) outranks writer (300) in OCCUPATION_ANCHORS.
            check("occupation decides the subcategory, by priority",
                  politician["sub"] == SUBCATEGORY_ID[("People", "Politician")],
                  f"sub={politician['sub']}")
            check("birth date kept for the year column",
                  (politician["birth"] or "").startswith("1879"),
                  str(politician["birth"]))
            check("a person has no population of their own",
                  politician["population"] is None, str(politician["population"]))
            check("the place's population is carried for the spread",
                  politician["loc_pop"] == 8900000.0, str(politician["loc_pop"]))

        dead = rows.get(7)
        check("place of death stands in when there is no birthplace",
              dead is not None and dead["loc_pid"] == 20 and dead["loc_qid"] == 2,
              "" if dead is None else f"{dead['loc_pid']} {dead['loc_qid']}")

        villager = rows.get(8)
        check("a coordinate-less birthplace resolves one hop up P131",
              villager is not None and villager["loc_qid"] == 2,
              "" if villager is None else str(villager["loc_qid"]))
        if villager:
            check("the one-hop fallback still names the place it landed on",
                  villager["loc_label"] == "Munich", str(villager["loc_label"]))

        check("no article anywhere means not placed at all", 9 not in rows)
        check("an ambiguous P131 parent is not guessed at", 11 not in rows)

        # --- the floor ---------------------------------------------------
        # It must bound how many *derived* rows the pyramid carries without
        # ever dropping something that has a coordinate of its own.
        high = run(work, "--derived-min-score", "0.95")
        kept = set(high["qid"].to_list())
        check("a high floor drops derived rows",
              not ({6, 7, 8} & kept), str(sorted(kept)))
        check("a high floor never drops an item with its own coordinate",
              {1, 2, 3, 4, 5} <= kept, str(sorted(kept)))

    if failures:
        sys.exit(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
    print("\nbuild_master behaves")


if __name__ == "__main__":
    main()
