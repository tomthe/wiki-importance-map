"""Stage 3 - one table of everything the travel map draws, with its seasons.

Two kinds of row:

**Destinations** - one per Wikidata item that has a Wikivoyage article in any
edition, plus the handful of articles with a `{{geo}}` but no Wikidata item.
Their importance is the sum of their pageviews across every edition, with each
edition's redirects folded into the article they point at, kept month by month.

**Listings** - the See / Do / Buy / Eat / Drink / Sleep / Go entries from the
English and German guides. Nobody links to a listing and it has no pageviews of
its own, so its importance is borrowed from the article it is listed in, less a
penalty that depends on what it is and where it sits in the article. That is
the honest version of the number: a listing in a popular guide is something
many travellers have read about, and nothing finer than that is measured.
Listings borrow their article's seasonal profile too, so a beach bar in
Mallorca rises in July with Mallorca.

Importance
----------
    V      = views over a year, all editions summed; each calendar month is
             the lower of its two years (see build_destinations)
    s      = log10(1 + V) / log10(1 + V_max)                 the year
    s_m    = log10(1 + 12 * views in month m) / log10(1 + V_max)
                                                             one month, annualised

so a place with the same traffic every month has s_m = s all year, and a ski
resort has s_m far above s in February and far below in July. The tile carries
s and the twelve monthly shares (as bytes, relative to the item's own peak
month); the browser turns them back into s_m for whichever month is selected.

The pyramid is built on the *peak* month, not the year. A destination that only
matters in one season would otherwise be pushed to deep zooms by its annual
total, and never be in the tiles that the map shows for its season.

Coordinates are never invented: a destination uses its Wikidata P625 (as the
Wikipedia map resolved it), else the article's own `{{geo}}`; a listing uses
its own lat/long, else its Wikidata item's P625; and everything else is left
off the map.

Output: WORK/voyage.parquet

Usage:
    python -m pipeline.voyage.build_master
"""

from __future__ import annotations

import functools
import json
import math
import sys
import time
import unicodedata

import numpy as np
import polars as pl

from pipeline.taxonomy import CATEGORIES as WIKIMAP_CATEGORIES
from pipeline.taxonomy import subcategory_names as wikimap_subcategory_names
from pipeline.voyage import config
from pipeline.voyage.taxonomy import (
    CATEGORY_ID,
    DEST_TYPE_SUB,
    KIND_CATEGORY,
    SUB_ID,
    VCARD_SKIP,
    VCARD_TYPE,
    WIKIMAP_DEST_SUB,
    WIKIMAP_SUB,
    keyword_sub,
)

print = functools.partial(print, flush=True)

STATUS_CODE = {"outline": 1, "usable": 2, "guide": 3, "star": 4}
SYNTHETIC = 0x8000_0000  # ids at or above this are not Wikidata items

# Listing penalty, in units of normalised importance (1.0 = the most viewed
# destination). 0.12 is about a factor of five in views below its article.
LISTING_BASE = 0.12
KIND_PENALTY = {
    "see": 0.00, "do": 0.02, "eat": 0.05, "drink": 0.06, "buy": 0.07,
    "sleep": 0.07, "go": 0.08, "other": 0.10,
}
ORDER_STEP = 0.004   # per listing ahead of it in the same section
ORDER_CAP = 0.06
BONUS_WIKIDATA = 0.03
BONUS_IMAGE = 0.01
BONUS_WIKIPEDIA = 0.01

# A listing this far from the city it is listed in has a typo in its
# coordinate - usually a dropped minus sign or swapped lat/long.
MAX_LISTING_KM = {"City": 60.0, "District": 30.0, "Airport": 30.0}


def haversine_km(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = (
        np.sin((lat2 - lat1) * p / 2) ** 2
        + np.cos(lat1 * p) * np.cos(lat2 * p) * np.sin((lon2 - lon1) * p / 2) ** 2
    )
    return 12742 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def norm_name(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(ch for ch in text if ch.isalnum())


def load_wikimap(qids: pl.Series) -> pl.DataFrame:
    """Coordinates and facts for these items, from the Wikipedia map's table.
    Only items standing on their own P625: a borrowed coordinate would put a
    listing at somebody's birthplace."""
    cat_names = WIKIMAP_CATEGORIES
    sub_names = wikimap_subcategory_names()
    wm = (
        pl.scan_parquet(config.WIKIMAP_MASTER)
        .filter(pl.col("qid").is_in(qids.implode()) & (pl.col("loc_pid") == 0))
        .select(
            "qid", "lon", "lat", "label_en", "descr_en", "cat", "sub", "image",
            "population", "elevation",
            pl.when(pl.col("n_countries") > 1).then(None)
            .otherwise(pl.col("country_label")).alias("country_label"),
        )
        .collect()
    )
    cats = [cat_names[c] for c in wm["cat"].to_list()]
    subs = [sub_names.get(cat, ["Other"])[s] if s < len(sub_names.get(cat, [])) else "Other"
            for cat, s in zip(cats, wm["sub"].to_list())]
    return wm.with_columns(
        pl.Series("wm_cat", cats), pl.Series("wm_sub", subs)
    ).drop("cat", "sub")


def monthly_views(pages: pl.DataFrame) -> pl.DataFrame:
    """(lang, title, year, month, views) with redirects folded into their
    targets. Despiked counts, every access method summed."""
    pv = (
        pl.read_parquet(config.PAGEVIEWS_PARQUET)
        .group_by("lang", "title", "year", "month")
        .agg(pl.col("despiked").sum().alias("views"))
        .with_columns(pl.col("title").str.replace_all("_", " "))
    )
    redirects = (
        pages.filter(pl.col("redirect").is_not_null())
        .select(
            "lang", "title",
            # "Target#Section" - the section is not part of the page.
            pl.col("redirect").str.replace(r"#.*$", "").alias("target"),
        )
    )
    total = pv["views"].sum()
    pv = (
        pv.join(redirects, on=["lang", "title"], how="left")
        .with_columns(pl.coalesce("target", "title").alias("title"))
        .drop("target")
    )
    articles = pages.filter(pl.col("redirect").is_null()).select("lang", "title")
    pv = pv.join(articles, on=["lang", "title"], how="semi")
    kept = pv["views"].sum()
    print(
        f"  pageviews: {total:,} on main-namespace titles, {kept:,} "
        f"({100 * kept / max(total, 1):.0f}%) land on an article after redirects"
    )
    return pv.group_by("lang", "title", "year", "month").agg(pl.col("views").sum())


def build_destinations(pages: pl.DataFrame, pv: pl.DataFrame) -> pl.DataFrame:
    articles = pages.filter(pl.col("redirect").is_null() & ~pl.col("disamb"))
    sitelinks = (
        pl.scan_parquet(config.SITELINKS_PARQUET)
        .filter(pl.col("site").str.ends_with("wikivoyage"))
        .with_columns(pl.col("site").str.strip_suffix("wikivoyage").str.replace_all("_", "-")
                      .alias("lang"))
        .select("lang", "title", "qid")
        .collect()
    )
    articles = articles.join(sitelinks, on=["lang", "title"], how="left")
    # Articles nobody linked to Wikidata can still be placed by their own
    # {{geo}}; they get a synthetic id.
    no_item = articles.filter(pl.col("qid").is_null() & pl.col("geo_lat").is_not_null())
    articles = pl.concat(
        [
            articles.filter(pl.col("qid").is_not_null()).with_columns(
                pl.col("qid").cast(pl.UInt32).alias("id")
            ),
            no_item.with_row_index("n").with_columns(
                (pl.col("n") + SYNTHETIC).cast(pl.UInt32).alias("id")
            ).drop("n"),
        ],
        how="diagonal_relaxed",
    )
    print(
        f"  {articles['id'].n_unique():,} destinations from {len(articles):,} articles "
        f"({len(no_item):,} placed by their own {{{{geo}}}} with no Wikidata item)"
    )

    # Views per destination and month, every edition summed.
    # Every edition summed, then the years combined month by month: the lower
    # of the years, so a month counts only what every year agrees on. Traffic
    # that bots add is extra, never missing, and it rarely hits the same
    # article in the same month two years running - the minimum drops it, and
    # a real season, which comes back every year, survives. A year with no
    # views at all in that month counts as zero.
    n_years = max(1, pv.select(pl.struct("year", "month").n_unique()).item() // 12)
    views = (
        pv.join(articles.select("lang", "title", "id"), on=["lang", "title"])
        .group_by("id", "year", "month")
        .agg(pl.col("views").sum())
        .group_by("id", "month")
        .agg(
            pl.when(pl.len() < n_years).then(0).otherwise(pl.col("views").min())
            .alias("views")
        )
    )
    print(f"  {n_years} year(s) of each calendar month, combined by their minimum")
    article_views = (
        pv.join(articles.select("lang", "title"), on=["lang", "title"])
        .group_by("lang", "title")
        .agg(pl.col("views").sum().alias("article_views"))
    )
    articles = articles.join(article_views, on=["lang", "title"], how="left").with_columns(
        pl.col("article_views").fill_null(0)
    )

    # One row per destination: the English article where there is one, else
    # the edition people actually read most.
    best = (
        articles.sort(
            [pl.col("lang") == "en", "article_views"], descending=[True, True]
        )
        .group_by("id", maintain_order=True)
        .agg(
            pl.col("lang").first().alias("link_lang"),
            pl.col("title").first().alias("link_title"),
            pl.col("lead").first().alias("lead"),
            pl.col("lead").filter(pl.col("lang") == "en").first().alias("lead_en"),
            pl.col("status").filter(pl.col("lang") == "en").first().alias("status"),
            pl.col("dest_type").filter(pl.col("lang") == "en").first().alias("dest_type"),
            pl.col("parent").drop_nulls().first().alias("parent"),
            pl.col("geo_lat").drop_nulls().first().alias("geo_lat"),
            pl.col("geo_lon").drop_nulls().first().alias("geo_lon"),
            pl.col("title").filter(pl.col("lang") == "en").first().alias("title_en"),
            pl.len().alias("n_editions"),
        )
    )
    month_wide = (
        views.pivot(on="month", index="id", values="views", sort_columns=True)
        .rename({str(m): f"m{m}" for m in range(1, 13)}, strict=False)
    )
    for m in range(1, 13):
        if f"m{m}" not in month_wide.columns:
            month_wide = month_wide.with_columns(pl.lit(0, dtype=pl.Int64).alias(f"m{m}"))
    dest = best.join(month_wide, on="id", how="left").with_columns(
        [pl.col(f"m{m}").fill_null(0) for m in range(1, 13)]
    )

    wm = load_wikimap(dest.filter(pl.col("id") < SYNTHETIC)["id"])
    dest = dest.join(wm, left_on="id", right_on="qid", how="left")
    dest = dest.with_columns(
        pl.coalesce("lon", "geo_lon").alias("lon"),
        pl.coalesce("lat", "geo_lat").alias("lat"),
    )
    missing = dest.filter(pl.col("lon").is_null())
    dest = dest.filter(pl.col("lon").is_not_null())
    print(f"  {len(missing):,} destinations have no coordinate anywhere and are left off")
    # A phrasebook is linked to its language's Wikidata item, and languages
    # have coordinates - which would put "Russian phrasebook" in Moscow.
    phrasebook = (
        (pl.col("dest_type") == "phrasebook")
        | pl.col("link_title").str.contains(
            r"(?i)phrasebook|sprachführer|guide linguistique|frasario|rozmówki|разговорник"
        )
    ).fill_null(False)
    print(f"  {dest.filter(phrasebook).height:,} phrasebooks left off")
    dest = dest.filter(~phrasebook)

    # Type: English's own banner first, the Wikipedia map's class otherwise.
    def dest_sub(row) -> str:
        if row["dest_type"]:
            return DEST_TYPE_SUB.get(row["dest_type"], "Other")
        cat, sub = row["wm_cat"], row["wm_sub"]
        return WIKIMAP_DEST_SUB.get((cat, sub)) or WIKIMAP_DEST_SUB.get((cat, None)) or "Other"

    subs = [dest_sub(r) for r in dest.select("dest_type", "wm_cat", "wm_sub").iter_rows(named=True)]
    dest = dest.with_columns(
        pl.lit(CATEGORY_ID["Destination"], dtype=pl.UInt8).alias("cat"),
        pl.Series("sub", [SUB_ID[("Destination", s)] for s in subs], dtype=pl.UInt8),
        pl.Series("sub_name", subs),
        # Article titles carry their place in the guide - "Chicago/Bronzeville",
        # "Stratford (New Zealand)" - which the map already shows by position
        # and the hover shows as the parent. The label is the name alone.
        pl.coalesce("title_en", "label_en", "link_title")
        .str.replace(r"^.*/", "")
        .str.replace(r"\s+\([^)]*\)$", "")
        .alias("title"),
        # English lead, then Wikidata's one-line English description, then the
        # lead of whichever edition the link goes to.
        pl.coalesce(
            pl.col("lead_en").replace("", None),
            pl.col("descr_en"),
            pl.col("lead").replace("", None),
        ).fill_null("").alias("descr"),
        pl.col("parent").alias("admin"),
        pl.col("status").replace_strict(STATUS_CODE, default=0, return_dtype=pl.UInt8)
        .alias("status"),
        pl.col("image").is_not_null().alias("has_image"),
        pl.lit(False).alias("has_url"),
        pl.col("n_editions").clip(0, 255).cast(pl.UInt8).alias("editions"),
        pl.lit("destination").alias("kind"),
    )
    return dest


NEAR_M = 300  # two listings this close with matching names are one place


def drop_near_duplicates(listings: pl.DataFrame) -> pl.DataFrame:
    """One row per place when the same thing is listed twice without a shared
    Wikidata item: "Russian Colony" in both See and Do, or "Dutch quarter" in
    German next to "Holländisches Viertel (Dutch Quarter)" in English.

    Two listings are the same place when they are within NEAR_M of each other
    and one normalised name contains the other (at least five characters, so
    "Café" does not swallow every café in town). The English one wins, then the
    better ranked. Candidates are paired through a grid of ~550 m cells and
    their neighbours, so this never compares a listing with the whole city.
    """
    df = listings.sort([pl.col("lang") == "en", "s"], descending=[True, True])
    df = (
        df.with_row_index("_rank")
        .with_columns(
            pl.Series("_n", [norm_name(n) for n in df["name"].to_list()]),
            (pl.col("lat") * 200).floor().cast(pl.Int32).alias("_cy"),
            (pl.col("lon") * 200).floor().cast(pl.Int32).alias("_cx"),
        )
    )
    keys = df.select("_rank", "_n", "lat", "lon", "_cy", "_cx").filter(
        pl.col("_n").str.len_chars() >= 5
    )
    shifted = keys.join(
        pl.DataFrame({"_dy": [-1, -1, -1, 0, 0, 0, 1, 1, 1],
                      "_dx": [-1, 0, 1, -1, 0, 1, -1, 0, 1]},
                     schema={"_dy": pl.Int32, "_dx": pl.Int32}),
        how="cross",
    ).with_columns((pl.col("_cy") + pl.col("_dy")).alias("_cy"),
                   (pl.col("_cx") + pl.col("_dx")).alias("_cx"))
    pairs = shifted.join(keys, on=["_cy", "_cx"], suffix="_b").filter(
        pl.col("_rank") < pl.col("_rank_b")
    )
    km = haversine_km(pairs["lat"].to_numpy(), pairs["lon"].to_numpy(),
                      pairs["lat_b"].to_numpy(), pairs["lon_b"].to_numpy())
    pairs = pairs.filter(pl.Series(km * 1000 < NEAR_M))
    same = [a in b or b in a for a, b in zip(pairs["_n"].to_list(), pairs["_n_b"].to_list())]
    losers = pairs.filter(pl.Series(same))["_rank_b"].unique()
    return df.filter(~pl.col("_rank").is_in(losers.implode())).drop(
        "_rank", "_n", "_cy", "_cx"
    )


def build_listings(dest: pl.DataFrame, pages: pl.DataFrame) -> pl.DataFrame:
    listings = pl.read_parquet(config.LISTINGS_PARQUET).filter(
        ~pl.col("vtype").is_in(list(VCARD_SKIP))
    )
    print(f"  {len(listings):,} listings in {', '.join(config.LISTING_LANGS)}")

    # The destination each listing is in, through the article's own id.
    sitelinks = (
        pl.scan_parquet(config.SITELINKS_PARQUET)
        .filter(pl.col("site").is_in([f"{l}wikivoyage" for l in config.LISTING_LANGS]))
        .with_columns(pl.col("site").str.strip_suffix("wikivoyage").alias("lang"))
        .select("lang", pl.col("title").alias("article"), pl.col("qid").alias("dest_id"))
        .collect()
    )
    listings = listings.join(sitelinks, on=["lang", "article"], how="left")
    parent = dest.select(
        pl.col("id").alias("dest_id"),
        pl.col("title").alias("dest_title"),
        pl.col("lon").alias("dest_lon"),
        pl.col("lat").alias("dest_lat"),
        pl.col("sub_name").alias("dest_sub"),
        pl.col("country_label").alias("dest_country"),
        "s", "peak", *[f"m{m}" for m in range(1, 13)],
    )
    before = len(listings)
    listings = listings.join(parent, on="dest_id", how="inner")
    print(f"  {before - len(listings):,} are in articles that are not on the map themselves")

    # Coordinates: the listing's own, else its Wikidata item's.
    linked = listings["wikidata"].drop_nulls().unique().cast(pl.UInt32)
    wm = load_wikimap(linked).select(
        pl.col("qid").cast(pl.Int64).alias("wikidata"),
        pl.col("lon").alias("wd_lon"), pl.col("lat").alias("wd_lat"),
        "wm_cat", "wm_sub",
        pl.col("image").alias("wd_image"),
    )
    listings = listings.join(wm, on="wikidata", how="left")
    own_ok = (
        pl.col("lat").is_between(-90, 90) & pl.col("lon").is_between(-180, 180)
        & ~((pl.col("lat").abs() < 1e-6) & (pl.col("lon").abs() < 1e-6))
    )
    listings = listings.with_columns(
        pl.when(own_ok).then(pl.col("lon")).otherwise(pl.col("wd_lon")).alias("lon"),
        pl.when(own_ok).then(pl.col("lat")).otherwise(pl.col("wd_lat")).alias("lat"),
    )
    placed = listings.filter(pl.col("lon").is_not_null())
    print(
        f"  {len(placed):,} have a position ({len(listings) - len(placed):,} have "
        f"neither a lat/long nor a Wikidata item with one)"
    )
    listings = placed

    km = haversine_km(
        listings["lat"].to_numpy(), listings["lon"].to_numpy(),
        listings["dest_lat"].to_numpy(), listings["dest_lon"].to_numpy(),
    )
    limit = listings["dest_sub"].replace_strict(MAX_LISTING_KM, default=float("inf"),
                                                return_dtype=pl.Float64).to_numpy()
    far = km > limit
    print(f"  {int(far.sum()):,} dropped as implausibly far from the city they are listed in")
    listings = listings.filter(pl.Series(~far))

    # Category and subcategory.
    cats, subs = [], []
    for row in listings.select("kind", "vtype", "wm_sub", "name").iter_rows(named=True):
        kind = row["kind"]
        vt = VCARD_TYPE.get(row["vtype"]) if row["vtype"] else None
        if kind == "other" and vt:
            cat = vt[0]
        else:
            cat = KIND_CATEGORY.get(kind, "Practical")
        sub = None
        if vt and vt[0] == cat:
            sub = vt[1]
        if sub is None and row["wm_sub"]:
            mapped = WIKIMAP_SUB.get(row["wm_sub"])
            if mapped and (cat, mapped) in SUB_ID:
                sub = mapped
        if sub is None:
            sub = keyword_sub(row["name"], cat)
        cats.append(CATEGORY_ID[cat])
        subs.append(SUB_ID[(cat, sub or "Other")])
    listings = listings.with_columns(
        pl.Series("cat", cats, dtype=pl.UInt8),
        pl.Series("sub", subs, dtype=pl.UInt8),
    )

    # Borrowed importance: the article's, less a penalty for what it is and how
    # far down its section it was listed.
    order = pl.int_range(pl.len()).over("lang", "article", "kind")
    penalty = (
        LISTING_BASE
        + pl.col("kind").replace_strict(KIND_PENALTY, default=0.10, return_dtype=pl.Float64)
        + (order * ORDER_STEP).clip(0, ORDER_CAP)
        - pl.when(pl.col("wikidata").is_not_null()).then(BONUS_WIKIDATA).otherwise(0.0)
        - pl.when(pl.col("has_image") | pl.col("wd_image").is_not_null())
        .then(BONUS_IMAGE).otherwise(0.0)
        - pl.when(pl.col("has_wikipedia")).then(BONUS_WIKIPEDIA).otherwise(0.0)
    )
    listings = listings.sort("lang", "article", "idx").with_columns(penalty.alias("penalty"))
    listings = listings.with_columns(
        (pl.col("s") - pl.col("penalty")).clip(0, 1).alias("s"),
        (pl.col("peak") - pl.col("penalty")).clip(0, 1).alias("peak"),
    )

    # Duplicates. The same museum is often in both the English and the German
    # guide, or in a city article and again in its district article; and a
    # listing for something that has its own destination article is that
    # destination twice.
    n0 = len(listings)
    dest_ids = dest.filter(pl.col("id") < SYNTHETIC)["id"].cast(pl.Int64)
    listings = listings.filter(
        pl.col("wikidata").is_null() | ~pl.col("wikidata").is_in(dest_ids.implode())
    )
    n1 = len(listings)
    listings = listings.sort(
        [pl.col("lang") == "en", "s"], descending=[True, True]
    )
    with_wd = listings.filter(pl.col("wikidata").is_not_null()).unique(
        "wikidata", keep="first", maintain_order=True
    )
    listings = pl.concat([with_wd, listings.filter(pl.col("wikidata").is_null())])
    n2 = len(listings)
    listings = drop_near_duplicates(listings)
    print(
        f"  duplicates removed: {n0 - n1:,} that are destinations themselves, "
        f"{n1 - n2:,} by Wikidata item, {n2 - len(listings):,} by name and position"
    )

    # Ids: the Wikidata item where it is unique, otherwise synthetic. The
    # decoder reads anything at or above SYNTHETIC as "no Wikidata item".
    listings = listings.with_row_index("n").with_columns(
        pl.when(pl.col("wikidata").is_not_null())
        .then(pl.col("wikidata"))
        .otherwise(pl.col("n") + SYNTHETIC + 0x0100_0000)
        .cast(pl.UInt32)
        .alias("id")
    ).drop("n")

    return listings.with_columns(
        pl.col("name").alias("title"),
        # The alternative name is often the local one - "Schloss Cecilienhof" -
        # and the only description a listing has.
        pl.when((pl.col("alt") != "") & (pl.col("content") != ""))
        .then(pl.format("{} ({})", pl.col("content"), pl.col("alt")))
        .when(pl.col("alt") != "")
        .then(pl.col("alt"))
        .otherwise(pl.col("content"))
        .alias("descr"),
        pl.col("dest_title").alias("admin"),
        pl.col("dest_country").alias("country_label"),
        pl.col("lang").alias("link_lang"),
        pl.col("article").alias("link_title"),
        pl.lit(0, dtype=pl.UInt8).alias("status"),
        (pl.col("has_image") | pl.col("wd_image").is_not_null()).alias("has_image"),
        pl.lit(0, dtype=pl.UInt8).alias("editions"),
        pl.lit(None, dtype=pl.Float64).alias("population"),
        pl.lit(None, dtype=pl.Float64).alias("elevation"),
    )


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    started = time.perf_counter()
    pages = pl.read_parquet(config.PAGES_PARQUET)
    print("pageviews")
    pv = monthly_views(pages)

    print("destinations")
    dest = build_destinations(pages, pv)

    months = [f"m{m}" for m in range(1, 13)]
    annual = dest.select(pl.sum_horizontal(months)).to_series()
    scale = math.log10(1 + annual.max())
    dest = dest.with_columns(
        pl.Series("s", np.log10(1 + annual.to_numpy()) / scale),
        pl.Series("annual", annual),
        (
            pl.max_horizontal([(1 + 12 * pl.col(m)).log10() for m in months]) / scale
        ).clip(0, 1).alias("peak"),
    )
    print(
        f"  {len(dest):,} destinations on the map; the most read has "
        f"{annual.max():,} views a year; median {int(annual.median()):,}"
    )

    print("listings")
    listings = build_listings(dest, pages)

    # Monthly shares relative to the peak month, one byte each. Listings carry
    # their article's profile, which build_listings already copied over.
    keep = [
        "id", "lon", "lat", "s", "peak", "cat", "sub", "title", "descr", "admin",
        "country_label", "link_lang", "link_title", "status", "has_image", "has_url",
        "editions", "population", "elevation", "kind", *months,
    ]
    both = pl.concat(
        [dest.select(keep), listings.select(keep)], how="vertical_relaxed"
    )
    peak_month = pl.max_horizontal(months)
    both = both.with_columns(
        [
            pl.when(peak_month > 0)
            .then((pl.col(m) * 255 / peak_month).round())
            .otherwise(0)
            .cast(pl.UInt8)
            .alias(m)
            for m in months
        ]
    )
    dupes = len(both) - both["id"].n_unique()
    if dupes:
        raise SystemExit(f"{dupes} duplicate ids - the id assignment is broken")

    both = both.sort("peak", descending=True)
    both.write_parquet(config.MASTER_PARQUET)
    print(f"\nwrote {len(both):,} rows to {config.MASTER_PARQUET}")
    print(f"  importance scale: log10(1 + {annual.max():,}) = {scale:.4f}")
    span = (
        pl.read_parquet(config.PAGEVIEWS_PARQUET, columns=["year", "month"])
        .unique()
        .sort("year", "month")
    )
    first, last = span.row(0), span.row(-1)
    (config.WORK_DIR / "voyage_meta.json").write_text(
        json.dumps({
            "importanceScale": scale,
            "viewsFrom": f"{first[0]}-{first[1]:02d}",
            "viewsTo": f"{last[0]}-{last[1]:02d}",
        }),
        encoding="utf-8",
    )
    print(
        both.group_by("cat").agg(pl.len(), pl.col("s").mean().round(3))
        .sort("cat")
    )
    print(f"  {(time.perf_counter() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
