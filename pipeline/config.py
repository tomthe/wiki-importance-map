"""Shared paths and constants for the wikipedia-pagerank-map data pipeline.

Override the two directories with environment variables if the dumps live
somewhere else:

    WIKIMAP_DUMPS   directory holding the downloaded dumps
    WIKIMAP_WORK    scratch directory for intermediate parquet
    WIKIMAP_DATA    where build_tiles and build_search publish to

WIKIMAP_DATA exists because those two stages write the *site's* data directory
no matter what WIKIMAP_WORK says, so running the synthetic-fixture path used to
overwrite a real build with test data. Point it somewhere else and the real
data/ is safe.
"""

import os
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent

DUMP_DIR = Path(os.environ.get("WIKIMAP_DUMPS", r"G:\tt\wikidata"))
WORK_DIR = Path(os.environ.get("WIKIMAP_WORK", str(DUMP_DIR / "work")))
DATA_DIR = Path(os.environ.get("WIKIMAP_DATA", str(REPO_DIR / "data")))


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

# Occupation, which is where a person's subcategory comes from, and the two
# dates that make a person's tooltip read like a person's.
P_OCCUPATION = 106
P_BIRTH_DATE = 569
P_DEATH_DATE = 570

# Derived locations: properties whose *value* is a place with a real P625. An
# item carrying one of these gets put on the map at that place's coordinate,
# and LOC_SOURCE records which property it was so the tooltip can say "born in
# Ulm" rather than pretending the person is a point on the ground.
#
# The order of this list is the precedence order - the first property an item
# has is the one that places it. Place of birth first is the honest default for
# people: it is the most often present and the most informative, and place of
# death only stands in when there is no birthplace.
P_BIRTHPLACE = 19
P_DEATHPLACE = 20
P_LOCATION = 276
P_HEADQUARTERS = 159
P_NARRATIVE_LOCATION = 840
P_HOME_PORT = 504
P_PORT_OF_REGISTRY = 532
P_RESIDENCE = 551
P_WORK_LOCATION = 937

# pid -> the small integer stored in the tile's flag bits. Values 1..7; 0 means
# "this item has its own P625". Only three bits are spent, so this list cannot
# grow past seven entries without widening the field in build_tiles.
LOC_SOURCE = {
    P_BIRTHPLACE: 1,
    P_DEATHPLACE: 2,
    P_LOCATION: 3,
    P_HEADQUARTERS: 4,
    P_NARRATIVE_LOCATION: 5,
    P_HOME_PORT: 6,
    P_PORT_OF_REGISTRY: 6,   # the same idea; the tooltip says "home port"
    P_RESIDENCE: 7,
    P_WORK_LOCATION: 7,      # "associated with"
}
# Precedence: which property wins when an item has several.
DERIVED_PIDS = [
    P_BIRTHPLACE, P_DEATHPLACE, P_LOCATION, P_HEADQUARTERS,
    P_NARRATIVE_LOCATION, P_HOME_PORT, P_PORT_OF_REGISTRY,
    P_RESIDENCE, P_WORK_LOCATION,
]

# How the tooltip introduces the place, by flag code. Shipped in manifest.json
# so the wording has one home rather than being duplicated in the client.
# Code 0 is an item standing on its own coordinate, which needs no preposition.
LOC_PHRASE = {
    0: "",
    1: "born in",
    2: "died in",
    3: "at",
    4: "headquartered in",
    5: "set in",
    6: "home port",
    7: "associated with",
}

for _d in (WORK_DIR, TRUTHY_OUT, RANKS_OUT, SITELINKS_OUT, DATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)
