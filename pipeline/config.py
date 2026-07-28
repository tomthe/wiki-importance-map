"""Shared paths and constants for the wikipedia-pagerank-map data pipeline.

Override the two directories with environment variables if the dumps live
somewhere else:

    WIKIMAP_DUMPS   directory holding the downloaded dumps
    WIKIMAP_WORK    scratch directory for intermediate parquet
"""

import os
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_DIR / "data"

DUMP_DIR = Path(os.environ.get("WIKIMAP_DUMPS", r"G:\tt\wikidata"))
WORK_DIR = Path(os.environ.get("WIKIMAP_WORK", str(DUMP_DIR / "work")))


def _newest(pattern: str):
    """Newest dump matching a glob, so re-downloads pick themselves up."""
    hits = sorted(DUMP_DIR.glob(pattern))
    return hits[-1] if hits else None


TRUTHY_DUMP = DUMP_DIR / "latest-truthy.nt.bz2"
DANKER_DUMP = _newest("*.allwiki.links.rank.bz2")
QRANK_DUMP = _newest("qrank*.gz")
SITELINKS_DUMP = DUMP_DIR / "wikidatawiki-latest-wb_items_per_site.sql.gz"

# Stage outputs
TRUTHY_OUT = WORK_DIR / "truthy"
RANKS_OUT = WORK_DIR / "ranks"
SITELINKS_OUT = WORK_DIR / "sitelinks"
MASTER_PARQUET = WORK_DIR / "articles.parquet"

# Wikidata properties we pull out of the truthy dump.
P_COORD = 625
P_INSTANCE_OF = 31
P_SUBCLASS_OF = 279
P_COUNTRY = 17
P_ADMIN = 131
P_OFFICIAL_LANG = 37
P_POPULATION = 1082
P_ELEVATION = 2044
P_INCEPTION = 571
P_IMAGE = 18
P_WEBSITE = 856
P_NATIVE_LABEL = 1705
P_OFFICIAL_NAME = 1448
P_LANG_CODE = 424

for _d in (WORK_DIR, TRUTHY_OUT, RANKS_OUT, SITELINKS_OUT, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)
