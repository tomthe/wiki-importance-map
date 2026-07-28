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
# There are exactly NINE coloured categories, and that is the ceiling rather
# than a preference. A map shows every category at once, so the palette has to
# clear the all-pairs colour-vision gates, not the easier adjacent-pairs ones.
# Nine hues clears them; ten only reaches the 6-8 "floor" band, which would
# have meant degrading every existing category's separation to buy a colour for
# one new layer. So People - which needs its own category because nothing else
# describes a person - took the ninth slot, and the other new layers (events,
# artworks, ships, fiction, companies) became subcategories of what already
# existed. Detail in subcategories is free: it is read as text, not as colour.
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
    "People",
]
CATEGORY_ID = {name: i for i, name in enumerate(CATEGORIES)}

# Subcategory 0 of a category means "the rest of it". "Other" is the right word
# for that almost everywhere, but a human with no recorded occupation is a
# Person, not an "other".
FALLBACK_SUB: dict[str, str] = {"People": "Person"}

# Validated with the dataviz palette validator under --pairs all, which is the
# case a map actually presents. Light on the positron basemap (#f8f8f6), dark
# on dark-matter (#0e0e0e); both clear the lightness band, chroma floor,
# colour-vision separation and normal-vision floor. Do not hand-tweak a hex
# without re-running the validator - the ordering and steps are what pass.
#
#   light  worst all-pairs CVD 9.9 (protan), normal-vision floor 16.4
#   dark   worst all-pairs CVD 8.7 (protan), normal-vision floor 16.3
#
# The eight original hues are untouched. People's hue was chosen by sweeping
# the whole OKLCH space against them: in dark mode it costs the palette
# literally nothing (the binding pairs are the same two as before), and in
# light mode it takes the worst pair from 11.1 to 9.9, still well clear. The
# two themes differ by 25 degrees of hue, which is in line with the drift the
# original palette already had between its own light and dark steps.
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
        "#6d56c0",  # People
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
        "#8729aa",
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
    # Ships. Placed at their home port (P504) or port of registry (P532), so a
    # named vessel shows up where it was based rather than nowhere at all.
    (2031121, "Transport", "Warship", 170),
    (177597, "Transport", "Warship", 172),  # naval vessel
    (2811, "Transport", "Submarine", 174),
    (697196, "Transport", "Ocean liner", 176),
    (170483, "Transport", "Sailing ship", 178),
    (852190, "Transport", "Shipwreck", 180),
    (11446, "Transport", "Ship", 620),
    (1229765, "Transport", "Watercraft", 640),
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
    # Artworks, placed at the institution that holds them (P276): the Mona Lisa
    # in the Louvre. Priorities are tight so a painting is never swallowed by
    # the museum-shaped classes above it.
    (3305213, "Culture & religion", "Painting", 90),
    (860861, "Culture & religion", "Sculpture", 92),
    (219423, "Culture & religion", "Mural", 93),
    (15711026, "Culture & religion", "Altarpiece", 94),
    (4502142, "Culture & religion", "Artwork", 400),   # visual artwork
    (838948, "Culture & religion", "Artwork", 420),    # work of art
    # Fiction, placed at its narrative setting (P840) - a map of where stories
    # happen, which is the one layer here that exists nowhere else.
    (8261, "Culture & religion", "Novel", 96),
    (11424, "Culture & religion", "Film", 97),
    (5398426, "Culture & religion", "Television series", 98),
    (25379, "Culture & religion", "Play", 99),
    (1344, "Culture & religion", "Opera", 101),
    (7889, "Culture & religion", "Video game", 102),
    (49084, "Culture & religion", "Short story", 103),
    (7725634, "Culture & religion", "Literary work", 430),
    (47461344, "Culture & religion", "Written work", 440),
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
    (783794, "Economy & services", "Company", 401),
    (6881511, "Economy & services", "Business", 402),   # enterprise
    (163740, "Economy & services", "Nonprofit organisation", 405),
    (43229, "Economy & services", "Organisation", 880),
    # --- People ----------------------------------------------------------
    # The only P31 anchor People needs: everything below "human" lands in the
    # category, and the subcategory then comes from P106 (occupation) via
    # OCCUPATION_ANCHORS. Naming the subcategory after the category itself is
    # how _subcategory_ids spells "slot 0, the rest of this category" - which
    # FALLBACK_SUB renders as "Person".
    (5, "People", "People", 100),
    # --- Other -----------------------------------------------------------
    # Events are not places, so they get the neutral slot rather than a hue
    # (see the note on CATEGORIES). The subcategories are what make them
    # usable: tick "Battle" alone and the map becomes a war map.
    (178561, "Other", "Battle", 110),
    (188055, "Other", "Siege", 112),
    (198, "Other", "War", 115),
    (645883, "Other", "Military operation", 118),
    (13418847, "Other", "Historical event", 120),
    (131569, "Other", "Treaty", 122),
    (10931, "Other", "Revolution", 124),
    (3199915, "Other", "Massacre", 126),
    (7944, "Other", "Earthquake", 128),
    (8065, "Other", "Natural disaster", 130),
    (3839081, "Other", "Disaster", 135),
    (44512, "Other", "Epidemic", 138),
    (124757, "Other", "Riot", 140),
    (132241, "Other", "Festival", 145),
    (16510064, "Other", "Sporting event", 150),
    (906512, "Other", "Shipwrecking", 155),
    (3895768, "Other", "Fictional location", 160),
    (1190554, "Other", "Event", 800),
]

# (occupation_class_qid, "People", subcategory, priority)
#
# Resolved from P106, not P31, against the same P279 graph: a person is placed
# in the category by being an instance of human, and given a subcategory by
# what they did. Most people carry several occupations - "politician, lawyer,
# writer" is a common shape - so priority decides, and the rule is the same one
# the P31 anchors use: the more specific and more defining wins.
# The ordering principle is *how rarely this occupation is incidental*, not how
# specific it is. Wikidata lists everything anybody ever did: Descartes is a
# "military officer", Lincoln a "farmer", Leonardo a "diplomat", Osama bin Laden
# a "civil engineer". Sorting by specificity puts those first and produces
# nonsense, so the roles that are usually somebody's whole identity go at the
# top and the ones that are usually a footnote go at the bottom.
OCCUPATION_ANCHORS: list[tuple[int, str, str, int]] = [
    # --- power -----------------------------------------------------------
    (82955, "People", "Politician", 100),
    (116, "People", "Monarch & noble", 110),
    (12097, "People", "Monarch & noble", 112),      # king
    (39018, "People", "Monarch & noble", 114),      # emperor
    # Nobility is usually a fact about a writer rather than the writer, so it
    # sits below the title-holders proper.
    (16744001, "People", "Monarch & noble", 160),   # noble
    (2478141, "People", "Monarch & noble", 162),    # aristocrat
    # --- religion --------------------------------------------------------
    (42603, "People", "Religious figure", 150),     # priest
    (29182, "People", "Religious figure", 152),     # bishop
    (43115, "People", "Religious figure", 154),     # saint
    (1234713, "People", "Religious figure", 156),   # theologian
    (42857, "People", "Religious figure", 158),     # prophet
    # --- making things ----------------------------------------------------
    # Above the sciences on purpose. Nobody is listed as a painter or a singer
    # in passing, but the polymaths are all listed as astronomers, anatomists,
    # philosophers and activists too - which is how Leonardo came out as a
    # Scientist, Michelangelo as a Writer and John Lennon as a Philosopher (by
    # way of "peace activist", which the graph hangs off philosopher).
    #
    # The interleaving inside this block is not decoration - each step pins one
    # real person who would otherwise be filed wrongly:
    #   singer  > actor    Bowie, Madonna and Sinatra all act; they are singers
    #   actor   > painter  Chaplin is credited as a composer and a painter
    #   painter > composer Leonardo is credited as a composer too
    #   composer > musician Mozart is credited as both; he is a composer
    (177220, "People", "Musician", 180),            # singer
    (33999, "People", "Actor", 181),
    (1028181, "People", "Artist", 182),             # painter
    (1281618, "People", "Artist", 183),             # sculptor
    # Named explicitly rather than left to the graph: "songwriter" is a
    # subclass of composer, so Michael Jackson inherited Composer from it.
    (753110, "People", "Musician", 184),            # songwriter
    (36834, "People", "Composer", 186),
    (639669, "People", "Musician", 188),
    # --- science and scholarship -----------------------------------------
    # A named science beats plain "mathematician", which beats "philosopher":
    # Einstein is listed as both a mathematician and a physicist, and the
    # physicist is the one people mean.
    (11063, "People", "Scientist", 195),            # astronomer
    (169470, "People", "Scientist", 196),           # physicist
    (593644, "People", "Scientist", 197),           # chemist
    (864503, "People", "Scientist", 198),           # biologist
    (2374149, "People", "Scientist", 199),          # botanist
    (901, "People", "Scientist", 200),
    (170790, "People", "Mathematician", 205),
    (4964182, "People", "Philosopher", 215),
    (201788, "People", "Scholar", 220),             # historian
    (188094, "People", "Scholar", 222),             # economist
    (3621491, "People", "Scholar", 224),            # archaeologist
    (14467526, "People", "Scholar", 226),           # linguist
    # --- words -----------------------------------------------------------
    (49757, "People", "Writer", 295),               # poet
    (6625963, "People", "Writer", 296),             # novelist
    (28389, "People", "Writer", 298),               # screenwriter
    (36180, "People", "Writer", 300),
    (333634, "People", "Writer", 310),              # translator
    (11900058, "People", "Explorer", 312),
    # Chekhov practised medicine and wrote plays; the plays are why anyone has
    # heard of him, so this sits below the writers.
    (39631, "People", "Physician", 315),
    # --- music, stage, art -----------------------------------------------
    # These have to beat the classes that hang off "artist" in the P279 graph -
    # "dancer" and "record producer" both inherit from it, which is how Michael
    # Jackson came out as an Artist rather than a Musician.
    (2526255, "People", "Film director", 332),
    (33231, "People", "Photographer", 345),
    (42973, "People", "Architect", 350),
    (483501, "People", "Artist", 356),
    # The P279 graph hangs all four of these off "historian", so anybody who
    # ever published a memoir inherited Scholar - Chaplin, Marilyn Monroe and
    # Kafka all came out as scholars. They are writing genres, not scholarship,
    # and they are almost always a second credit, so they sit at the bottom of
    # the creative tier: having written an autobiography should not outrank
    # being an actor. Anyone who is only ever one of these still reads Writer.
    (18814623, "People", "Writer", 360),            # autobiographer
    (11774156, "People", "Writer", 361),            # memoirist
    (18939491, "People", "Writer", 362),            # diarist
    (864380, "People", "Writer", 363),              # biographer
    # --- sport -----------------------------------------------------------
    # Playing professionally is never a footnote, and the retired ones collect
    # exactly the credits that used to outrank it: Maradona is listed as an
    # actor, Sharapova as a diplomat, McEnroe as a journalist. Above all three.
    (937857, "People", "Athlete", 280),             # association football player
    (2309784, "People", "Athlete", 281),            # sport cyclist
    (10833314, "People", "Athlete", 282),           # tennis player
    (3665646, "People", "Athlete", 283),            # basketball player
    (12299841, "People", "Athlete", 284),           # cricketer
    (10873124, "People", "Athlete", 285),           # chess player
    (2066131, "People", "Athlete", 286),
    (50995749, "People", "Athlete", 288),           # sportsperson
    # --- roles that are usually somebody's second line -------------------
    (47064, "People", "Military", 375),             # military personnel
    (189290, "People", "Military", 377),            # military officer
    (1930187, "People", "Journalist", 405),
    (193391, "People", "Diplomat", 410),
    # --- professions -----------------------------------------------------
    (40348, "People", "Lawyer & judge", 420),
    (16533, "People", "Lawyer & judge", 422),       # judge
    (43845, "People", "Businessperson", 440),
    (131524, "People", "Businessperson", 442),      # entrepreneur
    (205375, "People", "Inventor", 470),
    (81096, "People", "Engineer", 500),
    # Broad enough to swallow half the dataset if they ranked highly, so they
    # sit at the bottom and only apply when nothing more specific matched.
    (1622272, "People", "Scholar", 700),            # university teacher
    (37226, "People", "Teacher", 720),
    (131512, "People", "Farmer", 800),
]

MAX_DEPTH = 12


def _subcategory_ids() -> dict[tuple[str, str], int]:
    """Stable numeric ids for (category, subcategory) pairs."""
    ids: dict[tuple[str, str], int] = {}
    for cat in CATEGORIES:
        ids[(cat, cat)] = 0  # 0 = "rest of this category"
    for _, cat, sub, _ in ANCHORS + OCCUPATION_ANCHORS:
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
        names[sub_id] = sub if sub_id else FALLBACK_SUB.get(cat, "Other")
    return out


def load_subclass_graph(con, truthy_dir: str) -> dict[int, list[int]]:
    """parent class -> its direct subclasses, from five million P279 edges.

    Read once and handed to every resolve_anchors() call: the P31 map and the
    P106 map walk the same graph, and reading it twice would cost more than
    both searches together.
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
    return children


def resolve_anchors(children: dict[int, list[int]], anchors) -> pa.Table:
    """Resolve every reachable Wikidata class to (cat, sub, priority).

    Anchors seed the search; the P279 graph carries the mapping down to
    subclasses that were never listed by hand.
    """
    # class_qid -> (cat_id, sub_id, priority)
    assigned: dict[int, tuple[int, int, int]] = {}
    queue: deque[tuple[int, int]] = deque()
    pinned: set[int] = set()
    for class_qid, cat, sub, priority in anchors:
        cat_id = CATEGORY_ID[cat]
        sub_id = SUBCATEGORY_ID[(cat, sub)]
        prev = assigned.get(class_qid)
        if prev is None or priority < prev[2]:
            assigned[class_qid] = (cat_id, sub_id, priority)
            queue.append((class_qid, 0))
        pinned.add(class_qid)

    while queue:
        class_qid, depth = queue.popleft()
        if depth >= MAX_DEPTH:
            continue
        cat_id, sub_id, priority = assigned[class_qid]
        for child in children.get(class_qid, ()):
            # A class that was named by hand keeps what it was named as. The
            # BFS only fills in classes nobody listed.
            #
            # Without this, an inherited priority could out-rank an explicit
            # one and quietly overwrite it: "monarch" is pinned to
            # People/Monarch & noble at 110, but monarch is a subclass of
            # politician (100), so the walk reached it at 101 and Elizabeth II
            # came out as a Politician. The same edge turned every philosopher
            # into a Scientist and Mozart into an Artist.
            if child in pinned:
                continue
            # A child inherits, but one step further from the anchor.
            candidate = priority + 1
            prev = assigned.get(child)
            if prev is None or candidate < prev[2]:
                assigned[child] = (cat_id, sub_id, candidate)
                queue.append((child, depth + 1))

    print(f"     mapped {len(assigned):,} classes from {len(anchors)} anchors")
    return pa.table(
        {
            "class_qid": pa.array([q for q in assigned], pa.uint32()),
            "cat": pa.array([v[0] for v in assigned.values()], pa.uint8()),
            "sub": pa.array([v[1] for v in assigned.values()], pa.uint8()),
            "priority": pa.array([v[2] for v in assigned.values()], pa.int32()),
        }
    )


def build_class_map(con, truthy_dir: str) -> pa.Table:
    """The P31 class map on its own, for callers that need nothing else."""
    return resolve_anchors(load_subclass_graph(con, truthy_dir), ANCHORS)


def verify() -> None:
    """Print each anchor with the label the dump actually has for it."""
    import duckdb

    from pipeline import config

    truthy = str(config.TRUTHY_OUT).replace("\\", "/")
    con = duckdb.connect()
    con.execute("PRAGMA threads=16")
    every = [("P31", a) for a in ANCHORS] + [("P106", a) for a in OCCUPATION_ANCHORS]
    qids = sorted({a[0] for _, a in every})
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
    print(f"{'via':>5}  {'anchor':>12}  {'expected':<30}  actual label")
    bad = 0
    for via, (class_qid, cat, sub, _) in every:
        actual = rows.get(class_qid, "*** MISSING ***")
        flag = ""
        if actual == "*** MISSING ***":
            flag = "  <-- not in dump"
            bad += 1
        print(f"{via:>5}  Q{class_qid:>11}  {cat + '/' + sub:<30}  {actual}{flag}")
    print(f"\n{len(every)} anchors, {bad} missing")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    if args.verify:
        verify()
    else:
        for cat, subs in subcategory_names().items():
            print(f"{cat}: {', '.join(s for s in subs if s)}")
