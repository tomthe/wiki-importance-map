"""Paths for the Wikivoyage travel map, the second site this repository builds.

It shares the tile format, the pack files, the search index and the whole
front end with the Wikipedia map, but none of its inputs: the importance signal
here is nothing but Wikivoyage's own pageviews, month by month, so the map can
answer "where are travellers looking in February" rather than "what matters".

    WIKIVOYAGE_DUMPS   pageview and XML dumps (default G:\\tt\\wikivoyage)
    WIKIVOYAGE_WORK    intermediate parquet (default <dumps>/work)
    WIKIVOYAGE_DATA    where the tiles and search index are published
                       (default voyage/data in the repository)

The Wikidata stages of the main pipeline are read, not rerun: coordinates,
country, population and English descriptions all come from its outputs in
WIKIMAP_WORK, which is the only reason the travel map needs no 43 GB dump of
its own.
"""

import os
from pathlib import Path

from pipeline import config as wikimap

REPO_DIR = wikimap.REPO_DIR

DUMP_DIR = Path(os.environ.get("WIKIVOYAGE_DUMPS", r"G:\tt\wikivoyage"))
WORK_DIR = Path(os.environ.get("WIKIVOYAGE_WORK", str(DUMP_DIR / "work")))
DATA_DIR = Path(os.environ.get("WIKIVOYAGE_DATA", str(REPO_DIR / "voyage" / "data")))

PAGEVIEW_DIR = DUMP_DIR / "pageviews"   # pageviews-YYYYMM-user.bz2
XML_DIR = DUMP_DIR / "xml"              # <lang>wikivoyage-<date>-pages-articles.xml.bz2

# Stage outputs
PAGEVIEWS_PARQUET = WORK_DIR / "pageviews.parquet"   # lang, title, month, views
PAGES_PARQUET = WORK_DIR / "pages.parquet"           # one row per article or redirect
LISTINGS_PARQUET = WORK_DIR / "listings.parquet"     # one row per See/Do/Eat/... entry
MASTER_PARQUET = WORK_DIR / "voyage.parquet"         # what the tiles are built from

# Read from the Wikipedia map's pipeline.
SITELINKS_PARQUET = wikimap.SITELINKS_OUT / "sitelinks.parquet"
WIKIMAP_MASTER = wikimap.MASTER_PARQUET
WIKIDATA_COORDS = wikimap.TRUTHY_OUT  # coords_*.parquet

# Listings are taken from these editions only. A listing has no Wikidata item
# most of the time, so the same museum listed in English and in German cannot
# be matched reliably - and these two are by far the most complete. Everything
# else still contributes its destination articles and their pageviews.
LISTING_LANGS = ("en", "de")

for _d in (WORK_DIR, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)
