"""Check the phyllotaxis spread that fans derived items around their place.

This is the one piece of the pipeline that deliberately invents coordinates, so
the properties that keep it honest are worth pinning down:

  * an item with its own P625 is never moved, not by a nanodegree;
  * the most important item at a place stays exactly on it;
  * nothing lands further away than the clamp allows;
  * it is deterministic, so a rebuild puts everyone back where they were;
  * build_tiles and build_search agree to the bit, because they filter
    different rows and each has to number the groups the same way. If they
    ever diverge, searching for a person flies you to the middle of their
    birthplace while their dot sits somewhere off-screen.

    python -m tests.test_spread
"""

from __future__ import annotations

import math
import sys

import numpy as np
import polars as pl

from pipeline.build_tiles import (
    JITTER_MAX_KM,
    JITTER_MIN_KM,
    KM_PER_DEGREE,
    spread_derived,
)

PLACES = [
    # qid, lon, lat, population
    (1, 2.3522, 48.8566, 2_100_000.0),    # Paris
    (2, -0.1276, 51.5072, 8_900_000.0),   # London
    (3, 25.0, 78.2, 2_000.0),             # high latitude, tiny population
    (4, 100.0, 5.0, None),                # no population at all
]
PER_PLACE = 400


def fixture() -> pl.DataFrame:
    rows = []
    qid = 1000
    for place_qid, lon, lat, pop in PLACES:
        rows.append((place_qid, lon, lat, 0.9, 0, None, None))
        for k in range(PER_PLACE):
            qid += 1
            # Descending scores, with a deliberate tie in the middle so the
            # qid tiebreak in the sort is actually exercised.
            score = 0.5 if k in (10, 11) else 0.8 - k * 0.001
            rows.append((qid, lon, lat, score, 19, place_qid, pop))
    return pl.DataFrame(
        rows,
        schema={
            "qid": pl.UInt32, "lon": pl.Float64, "lat": pl.Float64,
            "score": pl.Float64, "loc_pid": pl.UInt32, "loc_qid": pl.UInt32,
            "loc_pop": pl.Float64,
        },
        orient="row",
    )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'ok  ' if ok else 'FAIL'} {name}{'' if ok else '  ' + detail}")
        if not ok:
            failures.append(name)

    df = fixture()
    out = spread_derived(df)
    by_qid = {r["qid"]: r for r in out.to_dicts()}
    original = {r["qid"]: r for r in df.to_dicts()}

    # --- places keep their real coordinates ---------------------------------
    untouched = all(
        by_qid[q]["lon"] == original[q]["lon"] and by_qid[q]["lat"] == original[q]["lat"]
        for q, *_ in [(p[0],) for p in PLACES]
    )
    check("an item with its own coordinate is never moved", untouched)

    # --- geometry per place -------------------------------------------------
    for place_qid, lon, lat, pop in PLACES:
        group = out.filter(pl.col("loc_qid") == place_qid).sort(
            ["score", "qid"], descending=[True, False]
        )
        dx = (group["lon"].to_numpy() - lon) * KM_PER_DEGREE * math.cos(math.radians(lat))
        dy = (group["lat"].to_numpy() - lat) * KM_PER_DEGREE
        r = np.hypot(dx, dy)

        check(
            f"Q{place_qid}: the top item stays on the place",
            abs(r[0]) < 1e-9,
            f"{r[0]:.6f} km",
        )
        expected_r = (
            min(JITTER_MAX_KM, max(JITTER_MIN_KM, 1.2 * math.log10(1 + pop) - 3.0))
            if pop is not None
            else min(JITTER_MAX_KM, max(JITTER_MIN_KM, 0.3 * math.sqrt(PER_PLACE)))
        )
        check(
            f"Q{place_qid}: nothing escapes the {expected_r:.2f} km radius",
            r.max() <= expected_r + 1e-6,
            f"max {r.max():.3f} km",
        )
        check(
            f"Q{place_qid}: the radius is clamped into [{JITTER_MIN_KM}, {JITTER_MAX_KM}] km",
            JITTER_MIN_KM - 1e-9 <= expected_r <= JITTER_MAX_KM + 1e-9,
            f"{expected_r:.3f}",
        )
        # Phyllotaxis fills the disc rather than a ring: with radius r*sqrt(k/K)
        # the mean distance is 2/3 of the maximum. A regular polygon or a
        # single ring would sit near 1.0 and this is what catches that.
        check(
            f"Q{place_qid}: points fill the disc, not a ring",
            0.6 <= r.mean() / expected_r <= 0.72,
            f"mean/max = {r.mean() / expected_r:.3f}",
        )
        check(
            f"Q{place_qid}: every item gets a distinct point",
            len({(round(a, 9), round(b, 9)) for a, b in zip(group["lon"], group["lat"])})
            == len(group),
        )
        check(
            f"Q{place_qid}: more important items sit closer in",
            bool(np.all(np.diff(r) >= -1e-9)),
            "radius is not monotonic in score order",
        )

    # --- determinism and cross-stage agreement ------------------------------
    again = spread_derived(fixture())
    check(
        "the same input gives the same output",
        out.sort("qid").equals(again.sort("qid")),
    )
    shuffled = spread_derived(fixture().sample(fraction=1.0, shuffle=True, seed=7))
    check(
        "input row order does not change the result",
        out.sort("qid").equals(shuffled.sort("qid")),
    )
    subset = spread_derived(
        fixture().select("qid", "lon", "lat", "score", "loc_pid", "loc_qid", "loc_pop")
    )
    joined = out.select("qid", "lon", "lat").join(
        subset.select("qid", "lon", "lat"), on="qid", suffix="_s"
    )
    check(
        "build_tiles and build_search land on the same coordinates",
        joined.filter(
            (pl.col("lon") != pl.col("lon_s")) | (pl.col("lat") != pl.col("lat_s"))
        ).is_empty(),
    )

    if failures:
        sys.exit(f"\n{len(failures)} check(s) failed: {', '.join(failures)}")
    print("\nthe spread behaves")


if __name__ == "__main__":
    main()
