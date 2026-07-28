"""Write a small synthetic articles.parquet so the tile/search/site path can be
exercised without waiting an hour for the real extraction.

    WIKIMAP_WORK=<tmpdir> python -m tests.make_fake_master
    WIKIMAP_WORK=<tmpdir> python -m pipeline.build_tiles --max-zoom 9
    WIKIMAP_WORK=<tmpdir> python -m pipeline.build_search --min-score 0.2

Note that build_tiles and build_search write into the repository's data/
directory whatever WIKIMAP_WORK says, so this replaces the real map.

It has to produce every column build_tiles and build_search read, including the
ones that are usually null - an item with no population, no founding date and
no description is the common case, and the encoder's sentinel paths only get
exercised if the fixture contains some.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from pipeline import config
from pipeline.taxonomy import CATEGORIES, CATEGORY_ID, subcategory_names

PEOPLE_CAT = CATEGORY_ID["People"]
# How many people to hang off each city, so the phyllotaxis spread in
# build_tiles has coincident points to actually spread and the deepest-zoom
# budget has something to bite on.
N_PEOPLE_PER_CITY = 900

CITIES = [
    ("London", -0.1276, 51.5072, "United Kingdom", "Greater London"),
    ("Paris", 2.3522, 48.8566, "France", "Île-de-France"),
    ("Berlin", 13.405, 52.52, "Germany", "Berlin"),
    ("New York", -74.006, 40.7128, "United States", "New York"),
    ("Tokyo", 139.6917, 35.6895, "Japan", "Kantō"),
    ("Rostock", 12.0991, 54.0924, "Germany", "Mecklenburg-Vorpommern"),
    ("Cairo", 31.2357, 30.0444, "Egypt", "Cairo Governorate"),
    ("São Paulo", -46.6333, -23.5505, "Brazil", "São Paulo"),
    ("Sydney", 151.2093, -33.8688, "Australia", "New South Wales"),
    ("Reykjavík", -21.9426, 64.1466, "Iceland", "Capital Region"),
]
N_PER_CITY = 2500
N_SCATTER = 8000


def main() -> None:
    rng = np.random.default_rng(20260727)
    lons, lats, names, countries, admins = [], [], [], [], []
    # Parallel arrays for the derived-location columns. A place row leaves them
    # null; a person row carries the city it was born in.
    loc_pids, loc_qids, loc_labels, loc_pops = [], [], [], []

    def place_row(lon, lat, name, country, admin):
        lons.append(lon)
        lats.append(lat)
        names.append(name)
        countries.append(country)
        admins.append(admin)
        loc_pids.append(0)
        loc_qids.append(None)
        loc_labels.append(None)
        loc_pops.append(None)

    for city_index, (name, lon, lat, country, admin) in enumerate(CITIES):
        place_row(lon, lat, name, country, admin)
        spread = 0.35
        for j in range(N_PER_CITY):
            place_row(
                lon + rng.normal(0, spread),
                lat + rng.normal(0, spread * 0.6),
                f"{name} place {j}",
                country,
                admin,
            )
        # Everyone "born" here shares the city's exact coordinate, which is the
        # case build_tiles has to cope with: without the spread they would be
        # one dot with nine hundred things behind it.
        for j in range(N_PEOPLE_PER_CITY):
            lons.append(lon)
            lats.append(lat)
            names.append(f"Person {j} of {name}")
            countries.append(country)
            admins.append(admin)
            # P19 place of birth for most, P20 place of death for a few, so
            # both flag codes appear in the fixture.
            loc_pids.append(config.P_BIRTHPLACE if j % 9 else config.P_DEATHPLACE)
            loc_qids.append(900_000 + city_index)
            loc_labels.append(name)
            loc_pops.append(float(200_000 * (city_index + 1)))

    for j in range(N_SCATTER):
        place_row(
            float(rng.uniform(-180, 180)),
            float(rng.uniform(-60, 70)),
            f"Remote thing {j}",
            None,
            None,
        )

    n = len(lons)
    subs = subcategory_names()
    is_person = np.array([p != 0 for p in loc_pids])
    cat = rng.integers(0, len(CATEGORIES), n)
    cat[is_person] = PEOPLE_CAT
    sub = np.array([rng.integers(0, len(subs[CATEGORIES[c]])) for c in cat])

    # Heavy-tailed importance, plus a guaranteed high score for the real cities
    # so they land in the shallow tiles.
    pr = rng.power(0.35, n)
    qr = rng.power(0.35, n)
    stride = 1 + N_PER_CITY + N_PEOPLE_PER_CITY
    for i in range(len(CITIES)):
        idx = i * stride
        pr[idx] = 0.93 + 0.06 * rng.random()
        qr[idx] = 0.93 + 0.06 * rng.random()

    score = 0.45 * pr + 0.45 * qr + 0.10 * rng.random(n)
    has_article = rng.random(n) < 0.6
    has_pop = rng.random(n) < 0.15
    has_elev = rng.random(n) < 0.25
    has_year = rng.random(n) < 0.2
    has_descr = rng.random(n) < 0.8

    def maybe(mask, values):
        return [v if m else None for m, v in zip(mask, values)]

    df = pl.DataFrame(
        {
            "qid": np.arange(1, n + 1, dtype=np.uint32),
            "lon": np.asarray(lons, dtype=np.float64),
            "lat": np.asarray(lats, dtype=np.float64),
            "score": score,
            "pr_norm": pr,
            "qr_norm": qr,
            "cat": cat.astype(np.uint8),
            "sub": sub.astype(np.uint8),
            "label_en": names,
            "title_en": [
                f"{nm} (disambig)" if a and i % 7 == 0 else (nm if a else None)
                for i, (nm, a) in enumerate(zip(names, has_article))
            ],
            "native_label": names,
            "title_native": names,
            "native_site": ["dewiki"] * n,
            "title_any": names,
            "any_site": ["enwiki" if a else "cebwiki" for a in has_article],
            "descr_en": maybe(has_descr, [f"a synthetic {CATEGORIES[c]} item" for c in cat]),
            # Wikidata stores these as doubles and an ISO timestamp string, and
            # both come through with nulls far more often than values.
            "population": maybe(has_pop, rng.integers(20, 9_000_000, n).astype(float)),
            "elevation": maybe(has_elev, rng.integers(-400, 6000, n).astype(float)),
            "inception": maybe(
                has_year,
                [f"{y:04d}-01-01T00:00:00Z" for y in rng.integers(800, 2020, n)],
            ),
            # People get a birth date instead of an inception date, which is
            # what build_tiles puts in the shared `year` column for them.
            "birth": [
                f"{y:04d}-03-04T00:00:00Z" if p else None
                for p, y in zip(is_person, rng.integers(1500, 2000, n))
            ],
            "n_sitelinks": rng.integers(0, 300, n).astype(np.int64),
            "country_label": countries,
            "admin_label": admins,
            # A tenth of them span several countries, so the "blank rather than
            # arbitrary" rule in build_tiles gets exercised.
            "n_countries": np.where(rng.random(n) < 0.1, 4, 1).astype(np.int64),
            "n_admin": np.ones(n, dtype=np.int64),
            "image": [
                "http://commons.wikimedia.org/x.jpg" if v else None
                for v in rng.random(n) < 0.3
            ],
            "website": ["https://example.org" if v else None for v in rng.random(n) < 0.1],
            "loc_pid": np.asarray(loc_pids, dtype=np.uint32),
            "loc_qid": pl.Series(loc_qids, dtype=pl.UInt32),
            "loc_label": loc_labels,
            "loc_pop": loc_pops,
        }
    ).sort("score", descending=True)

    config.MASTER_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(config.MASTER_PARQUET, compression="zstd")
    print(f"wrote {config.MASTER_PARQUET}: {len(df):,} synthetic items")


if __name__ == "__main__":
    main()
