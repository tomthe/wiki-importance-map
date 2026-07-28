"""Category taxonomy: Wikidata classes -> two-level map categories.

Wikidata has no usable "type of thing" field - it has P31 (instance of)
pointing at tens of thousands of classes, from "city" down to "commune of the
Central African Republic". So the mapping works in two steps:

1. ANCHORS pins a few hundred well-known classes to a (category, subcategory).
2. Every other class inherits from the nearest anchor above it in the P279
   (subclass of) graph, found by breadth-first search.

Each anchor carries a priority: when an item is an instance of several mapped
classes, the lowest priority wins, so "castle" beats "building" and "capital"
beats "human settlement". Inherited classes add their BFS depth to the
priority, so a closer anchor is preferred over a more distant one.

Verify anchor ids against the dump with:
    python -m pipeline.taxonomy --verify
Survey which classes actually matter with:
    python -m pipeline.survey_classes --top 400
"""

from __future__ import annotations

import argparse
from collections import deque

import pyarrow as pa

# Top-level categories. Id 0 is reserved for "not categorised".
#
# There are exactly eight coloured categories on purpose. Categorical colour
# stops working past eight hues, and a map shows every category at once, so the
# palette below had to clear the all-pairs colour-vision gates rather than the
# easier adjacent-pairs ones. Detail lives in the subcategories instead, which
# are free - they are read as text, not as colour.
CATEGORIES: list[str] = [
    "Other",
    "Settlements",
    "Nature",
    "Administrative",
    "Transport",
    "Buildings & landmarks",
    "Culture & religion",
    "Education & science",
    "Economy & services",
]
CATEGORY_ID = {name: i for i, name in enumerate(CATEGORIES)}

# Validated with the dataviz palette validator under --pairs all, which is the
# case a map actually presents. Light on the positron basemap (#f8f8f6), dark
# on dark-matter (#0e0e0e); both clear the lightness band, chroma floor,
# colour-vision separation and normal-vision floor. Do not hand-tweak a hex
# without re-running the validator - the ordering and steps are what pass.
PALETTE: dict[str, list[str]] = {
    "light": [
        "#8a8a85",  # Other - neutral, deliberately not a hue
        "#0d1bfb",  # Settlements
        "#578c19",  # Nature
        "#b976f7",  # Administrative
        "#883c00",  # Transport
        "#d8a614",  # Buildings & landmarks
        "#ac158b",  # Culture & religion
        "#46c6cf",  # Education & science
        "#f65b74",  # Economy & services
    ],
    "dark": [
        "#8f8f88",
        "#314fe7",
        "#46ac71",
        "#9077e9",
        "#c98212",
        "#696106",
        "#e5219f",
        "#00809a",
        "#ab545e",
    ],
}

# (class_qid, category, subcategory, priority)
# Priority breaks ties when an item is an instance of several mapped classes:
# lower wins, so a specific class beats a generic one ("castle" over "building").
ANCHORS: list[tuple[int, str, str, int]] = [
    # --- Settlements -----------------------------------------------------
    (5119, "Settlements", "Capital", 60),
    (515, "Settlements", "City", 100),
    (1549591, "Settlements", "City", 105),
    (3957, "Settlements", "Town", 110),
    (532, "Settlements", "Village", 120),
    (5084, "Settlements", "Hamlet", 130),
    (188509, "Settlements", "Suburb", 140),
    (123705, "Settlements", "Neighbourhood", 150),
    (74047, "Settlements", "Ghost town", 160),
    (350895, "Settlements", "Ghost town", 162),
    (3257686, "Settlements", "Locality", 300),
    (486972, "Settlements", "Settlement", 800),
    # --- Nature ----------------------------------------------------------
    (8502, "Nature", "Mountain", 100),
    (207326, "Nature", "Mountain", 104),
    (46831, "Nature", "Mountain range", 106),
    (54050, "Nature", "Hill", 110),
    (8072, "Nature", "Volcano", 115),
    (39816, "Nature", "Valley", 125),
    (150784, "Nature", "Canyon", 128),
    (23397, "Nature", "Lake", 130),
    (131681, "Nature", "Reservoir", 132),
    (4022, "Nature", "River", 140),
    (47521, "Nature", "Stream", 145),
    (34038, "Nature", "Waterfall", 148),
    (23442, "Nature", "Island", 150),
    (34763, "Nature", "Peninsula", 155),
    (185113, "Nature", "Cape", 158),
    (40080, "Nature", "Beach", 160),
    (39594, "Nature", "Bay", 162),
    (165, "Nature", "Sea", 170),
    (9430, "Nature", "Sea", 172),
    (4421, "Nature", "Forest", 175),
    (35509, "Nature", "Cave", 180),
    (107679, "Nature", "Cliff", 185),
    (473972, "Nature", "Protected area", 200),
    (46169, "Nature", "Protected area", 205),
    (22698, "Nature", "Park", 210),
    (1107656, "Nature", "Garden", 215),
    (271669, "Nature", "Landform", 850),
    (618123, "Nature", "Landform", 880),
    # --- Administrative --------------------------------------------------
    (6256, "Administrative", "Country", 50),
    (3624078, "Administrative", "Country", 52),
    (10864048, "Administrative", "Region", 200),
    (34876, "Administrative", "Province", 210),
    (28575, "Administrative", "County", 215),
    (149621, "Administrative", "District", 220),
    (15284, "Administrative", "Municipality", 230),
    (56061, "Administrative", "Administrative area", 860),
    # --- Transport -------------------------------------------------------
    (1248784, "Transport", "Airport", 100),
    (55488, "Transport", "Railway station", 110),
    (928830, "Transport", "Metro station", 115),
    (2175765, "Transport", "Tram stop", 118),
    (12280, "Transport", "Bridge", 120),
    (44377, "Transport", "Tunnel", 125),
    (44782, "Transport", "Port", 130),
    (34442, "Transport", "Road", 140),
    (79007, "Transport", "Street", 145),
    (12284, "Transport", "Canal", 150),
    (205495, "Transport", "Filling station", 160),
    # --- Buildings & landmarks -------------------------------------------
    (23413, "Buildings & landmarks", "Castle", 100),
    (16560, "Buildings & landmarks", "Palace", 105),
    (39715, "Buildings & landmarks", "Lighthouse", 110),
    (11303, "Buildings & landmarks", "Skyscraper", 115),
    (12518, "Buildings & landmarks", "Tower", 120),
    (1785071, "Buildings & landmarks", "Fort", 125),
    (57831, "Buildings & landmarks", "Fort", 128),  # fortress
    (4989906, "Buildings & landmarks", "Monument", 130),
    (5003624, "Buildings & landmarks", "Memorial", 135),
    (179700, "Buildings & landmarks", "Statue", 138),
    (109607, "Buildings & landmarks", "Ruins", 145),
    (174782, "Buildings & landmarks", "Square", 150),
    (38720, "Buildings & landmarks", "Windmill", 155),
    (185187, "Buildings & landmarks", "Watermill", 156),
    (3947, "Buildings & landmarks", "House", 160),
    (41176, "Buildings & landmarks", "Building", 840),
    (811979, "Buildings & landmarks", "Structure", 870),
    # --- Culture & religion ----------------------------------------------
    (207694, "Culture & religion", "Art museum", 95),
    (33506, "Culture & religion", "Museum", 100),
    (2087181, "Culture & religion", "Museum", 104),  # historic house museum
    (24354, "Culture & religion", "Theatre", 110),
    (41253, "Culture & religion", "Cinema", 115),
    (1060829, "Culture & religion", "Concert hall", 120),
    (839954, "Culture & religion", "Archaeological site", 125),
    (358, "Culture & religion", "Heritage site", 130),
    (2065736, "Culture & religion", "Heritage site", 132),  # cultural property
    (2977, "Culture & religion", "Cathedral", 135),
    (16970, "Culture & religion", "Church", 140),
    (108325, "Culture & religion", "Chapel", 145),
    (32815, "Culture & religion", "Mosque", 150),
    (34627, "Culture & religion", "Synagogue", 155),
    (44539, "Culture & religion", "Temple", 160),
    (44613, "Culture & religion", "Monastery", 165),
    (39614, "Culture & religion", "Cemetery", 170),
    (1370598, "Culture & religion", "Place of worship", 700),
    # --- Education & science ---------------------------------------------
    (3918, "Education & science", "University", 100),
    (875538, "Education & science", "University", 102),
    (9842, "Education & science", "Primary school", 110),
    (159334, "Education & science", "Secondary school", 115),
    (3914, "Education & science", "School", 120),
    (7075, "Education & science", "Library", 130),
    (62832, "Education & science", "Observatory", 140),
    (31855, "Education & science", "Research institute", 150),
    (2385804, "Education & science", "Educational institution", 800),
    # --- Economy & services ----------------------------------------------
    (16917, "Economy & services", "Hospital", 100),
    (1774898, "Economy & services", "Clinic", 105),
    (4260475, "Economy & services", "Hospital", 108),  # generic medical facility
    (483110, "Economy & services", "Stadium", 110),
    (1076486, "Economy & services", "Sports venue", 115),
    (27686, "Economy & services", "Hotel", 120),
    (11707, "Economy & services", "Restaurant", 125),
    (11315, "Economy & services", "Shopping centre", 130),
    (159719, "Economy & services", "Power station", 135),
    (12323, "Economy & services", "Dam", 140),
    (820477, "Economy & services", "Mine", 145),
    (83405, "Economy & services", "Factory", 150),
    (131734, "Economy & services", "Brewery", 155),
    (131596, "Economy & services", "Farm", 160),
    (22687, "Economy & services", "Bank", 165),
    (43501, "Economy & services", "Zoo", 170),
    (194195, "Economy & services", "Amusement park", 175),
    (40357, "Economy & services", "Prison", 180),
    (1137809, "Economy & services", "Courthouse", 185),
    (25550691, "Economy & services", "City hall", 190),
    (861951, "Economy & services", "Police station", 195),
    (1195942, "Economy & services", "Fire station", 200),
    (3917681, "Economy & services", "Embassy", 205),
    (245016, "Economy & services", "Military base", 210),
    (35054, "Economy & services", "Post office", 215),
    (4830453, "Economy & services", "Business", 400),
    (43229, "Economy & services", "Organisation", 880),
    # --- Other -----------------------------------------------------------
    (5, "Other", "Person", 100),
    (178561, "Other", "Battle", 110),
    (198, "Other", "War", 115),
    (13418847, "Other", "Historical event", 120),
    (1190554, "Other", "Event", 800),
]

MAX_DEPTH = 12


def _subcategory_ids() -> dict[tuple[str, str], int]:
    """Stable numeric ids for (category, subcategory) pairs."""
    ids: dict[tuple[str, str], int] = {}
    for cat in CATEGORIES:
        ids[(cat, cat)] = 0  # 0 = "rest of this category"
    for _, cat, sub, _ in ANCHORS:
        key = (cat, sub)
        if key not in ids:
            ids[key] = len([k for k in ids if k[0] == cat])
    return ids


SUBCATEGORY_ID = _subcategory_ids()


def subcategory_names() -> dict[str, list[str]]:
    """category name -> list of subcategory names indexed by sub id."""
    out: dict[str, list[str]] = {}
    for (cat, sub), sub_id in SUBCATEGORY_ID.items():
        names = out.setdefault(cat, [])
        while len(names) <= sub_id:
            names.append("")
        names[sub_id] = sub if sub_id else "Other"
    return out


def build_class_map(con, truthy_dir: str) -> pa.Table:
    """Resolve every Wikidata class to (cat, sub, priority).

    Anchors seed the search; the P279 graph carries the mapping down to
    subclasses that were never listed by hand.
    """
    edges = con.execute(
        f"""
        SELECT qid AS child, value AS parent
        FROM read_parquet('{truthy_dir}/claims_item_*.parquet')
        WHERE pid = 279
        """
    ).fetch_arrow_table()
    children: dict[int, list[int]] = {}
    for child, parent in zip(
        edges.column("child").to_pylist(), edges.column("parent").to_pylist()
    ):
        children.setdefault(parent, []).append(child)
    print(f"     P279 graph: {edges.num_rows:,} edges, {len(children):,} parents")

    # class_qid -> (cat_id, sub_id, priority)
    assigned: dict[int, tuple[int, int, int]] = {}
    queue: deque[tuple[int, int]] = deque()
    for class_qid, cat, sub, priority in ANCHORS:
        cat_id = CATEGORY_ID[cat]
        sub_id = SUBCATEGORY_ID[(cat, sub)]
        prev = assigned.get(class_qid)
        if prev is None or priority < prev[2]:
            assigned[class_qid] = (cat_id, sub_id, priority)
            queue.append((class_qid, 0))

    while queue:
        class_qid, depth = queue.popleft()
        if depth >= MAX_DEPTH:
            continue
        cat_id, sub_id, priority = assigned[class_qid]
        for child in children.get(class_qid, ()):
            # A child inherits, but one step further from the anchor.
            candidate = priority + 1
            prev = assigned.get(child)
            if prev is None or candidate < prev[2]:
                assigned[child] = (cat_id, sub_id, candidate)
                queue.append((child, depth + 1))

    print(f"     mapped {len(assigned):,} classes to categories")
    return pa.table(
        {
            "class_qid": pa.array([q for q in assigned], pa.uint32()),
            "cat": pa.array([v[0] for v in assigned.values()], pa.uint8()),
            "sub": pa.array([v[1] for v in assigned.values()], pa.uint8()),
            "priority": pa.array([v[2] for v in assigned.values()], pa.int32()),
        }
    )


def verify() -> None:
    """Print each anchor with the label the dump actually has for it."""
    import duckdb

    from pipeline import config

    truthy = str(config.TRUTHY_OUT).replace("\\", "/")
    con = duckdb.connect()
    con.execute("PRAGMA threads=16")
    qids = [a[0] for a in ANCHORS]
    con.execute("CREATE TABLE want (qid UINTEGER)")
    con.executemany("INSERT INTO want VALUES (?)", [(q,) for q in qids])
    rows = dict(
        con.execute(
            f"""
            SELECT l.qid, any_value(l.label)
            FROM read_parquet('{truthy}/labels_en_*.parquet') l
            JOIN want w ON w.qid = l.qid
            GROUP BY l.qid
            """
        ).fetchall()
    )
    print(f"{'anchor':>12}  {'expected':<28}  actual label")
    bad = 0
    for class_qid, cat, sub, _ in ANCHORS:
        actual = rows.get(class_qid, "*** MISSING ***")
        flag = ""
        if actual == "*** MISSING ***":
            flag = "  <-- not in dump"
            bad += 1
        print(f"Q{class_qid:>11}  {cat + '/' + sub:<28}  {actual}{flag}")
    print(f"\n{len(ANCHORS)} anchors, {bad} missing")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    if args.verify:
        verify()
    else:
        for cat, subs in subcategory_names().items():
            print(f"{cat}: {', '.join(s for s in subs if s)}")
