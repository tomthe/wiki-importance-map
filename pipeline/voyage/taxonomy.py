"""Categories for the travel map.

Wikivoyage already sorts everything a traveller wants into seven sections -
See, Do, Buy, Eat, Drink, Sleep, Go - and every listing lives in one of them, so
the categories are not an invention here: they are the guide's own table of
contents, plus the destinations themselves and a bucket for the practical
listings (embassies, pharmacies, tourist offices) that belong to none.

That is nine, which is exactly the ceiling the Wikipedia map measured for
colours that stay distinguishable *all pairs at once* under colour-vision
deficiency. The hues are a subset of that validated palette - a subset of a
set that passes an all-pairs test passes it too - assigned so the obvious
associations hold: green for Do (outdoors), the warm brown for Eat, the gold
of landmarks for See.

Subcategories are text, so they cost nothing to add and nothing to tell apart.
They come from three places, best first:

1. German vCards carry an explicit `type=` (museum, church, hotel, ...).
2. A listing linked to Wikidata inherits the Wikipedia map's class for that
   item - "Castle", "Art museum" - mapped onto this smaller list.
3. Otherwise a keyword in the listing's name: "Hostel Mitte", "Café Einstein".
"""

from __future__ import annotations

import re

from pipeline.taxonomy import PALETTE as WIKIMAP_PALETTE

CATEGORIES: list[str] = [
    "Practical",
    "Destination",
    "See",
    "Do",
    "Buy",
    "Eat",
    "Drink",
    "Sleep",
    "Go",
]
CATEGORY_ID = {name: i for i, name in enumerate(CATEGORIES)}
KIND_CATEGORY = {
    "other": "Practical", "see": "See", "do": "Do", "buy": "Buy", "eat": "Eat",
    "drink": "Drink", "sleep": "Sleep", "go": "Go",
}

# Indices into the Wikipedia map's validated palette (see pipeline/taxonomy.py
# for the numbers it clears). Index 0 is its neutral grey.
_FROM_WIKIMAP = [
    0,  # Practical       <- Other (neutral)
    1,  # Destination     <- Settlements, blue
    5,  # See             <- Buildings & landmarks, gold
    2,  # Do              <- Nature, green
    8,  # Buy             <- Economy & services, coral
    4,  # Eat             <- Transport, brown
    6,  # Drink           <- Culture & religion, magenta
    9,  # Sleep           <- People, violet
    7,  # Go              <- Education & science, teal
]
PALETTE = {
    theme: [colours[i] for i in _FROM_WIKIMAP] for theme, colours in WIKIMAP_PALETTE.items()
}

SUBCATEGORIES: dict[str, list[str]] = {
    "Practical": ["Other", "Tourist information", "Health", "Embassy", "Money & post", "Police"],
    "Destination": [
        "Other", "City", "District", "Region", "Country", "Park", "Rural area",
        "Airport", "Dive site", "Itinerary & topic",
    ],
    "See": [
        "Other", "Museum", "Religious site", "Castle & palace", "Monument & memorial",
        "Historic building", "Archaeological site", "Park & garden", "Nature & viewpoint",
        "Square & street", "Zoo & aquarium",
    ],
    "Do": [
        "Other", "Beach & swimming", "Outdoors & sport", "Theatre & music", "Tours",
        "Festival & event", "Amusement & leisure", "Course",
    ],
    "Buy": ["Other", "Market", "Mall & department store", "Shop"],
    "Eat": ["Other", "Restaurant", "Café & bakery", "Fast food & snack"],
    "Drink": ["Other", "Bar & pub", "Club", "Winery & brewery"],
    "Sleep": ["Other", "Hotel", "Hostel", "Guesthouse & B&B", "Camping", "Apartment & resort"],
    "Go": ["Other", "Airport", "Station", "Port & ferry", "Rental", "Parking & fuel"],
}
SUB_ID = {
    (cat, sub): i for cat, subs in SUBCATEGORIES.items() for i, sub in enumerate(subs)
}

# English Wikivoyage's article types -> Destination subcategory.
DEST_TYPE_SUB = {
    "city": "City", "district": "District", "region": "Region", "continent": "Region",
    "country": "Country", "park": "Park", "ruralarea": "Rural area", "airport": "Airport",
    "diveguide": "Dive site", "itinerary": "Itinerary & topic", "topic": "Itinerary & topic",
    "event": "Itinerary & topic",
}
# For destinations English has no article on: the Wikipedia map's category.
WIKIMAP_DEST_SUB = {
    ("Settlements", None): "City",
    ("Administrative", "Country"): "Country",
    ("Administrative", None): "Region",
    ("Nature", "Island"): "Region",
    ("Nature", "Protected area"): "Park",
    ("Nature", "Park"): "Park",
    ("Nature", None): "Park",
    ("Transport", "Airport"): "Airport",
}

# German vCard `type=` -> (category, subcategory). The category is only used
# when the section heading did not already say; the subcategory always is.
VCARD_TYPE = {
    "museum": ("See", "Museum"), "gallery": ("See", "Museum"), "art gallery": ("See", "Museum"),
    "church": ("See", "Religious site"), "temple": ("See", "Religious site"),
    "mosque": ("See", "Religious site"), "synagogue": ("See", "Religious site"),
    "monastery": ("See", "Religious site"), "cathedral": ("See", "Religious site"),
    "chapel": ("See", "Religious site"), "shrine": ("See", "Religious site"),
    "castle": ("See", "Castle & palace"), "palace": ("See", "Castle & palace"),
    "fortress": ("See", "Castle & palace"), "fort": ("See", "Castle & palace"),
    "manor house": ("See", "Castle & palace"),
    "monument": ("See", "Monument & memorial"), "memorial": ("See", "Monument & memorial"),
    "statue": ("See", "Monument & memorial"), "fountain": ("See", "Monument & memorial"),
    "building": ("See", "Historic building"), "tower": ("See", "Historic building"),
    "lighthouse": ("See", "Historic building"), "bridge": ("See", "Historic building"),
    "ruin": ("See", "Archaeological site"), "archaeological site": ("See", "Archaeological site"),
    "park": ("See", "Park & garden"), "garden": ("See", "Park & garden"),
    "landscape": ("See", "Nature & viewpoint"), "national park": ("See", "Nature & viewpoint"),
    "nature reserve": ("See", "Nature & viewpoint"), "viewpoint": ("See", "Nature & viewpoint"),
    "mountain": ("See", "Nature & viewpoint"), "lake": ("See", "Nature & viewpoint"),
    "waterfall": ("See", "Nature & viewpoint"), "cave": ("See", "Nature & viewpoint"),
    "square": ("See", "Square & street"), "street": ("See", "Square & street"),
    "zoo": ("See", "Zoo & aquarium"), "aquarium": ("See", "Zoo & aquarium"),
    "beach": ("Do", "Beach & swimming"), "swimming pool": ("Do", "Beach & swimming"),
    "spa": ("Do", "Beach & swimming"), "sports": ("Do", "Outdoors & sport"),
    "golf": ("Do", "Outdoors & sport"), "hiking": ("Do", "Outdoors & sport"),
    "skiing": ("Do", "Outdoors & sport"), "diving": ("Do", "Outdoors & sport"),
    "stadium": ("Do", "Outdoors & sport"), "theater": ("Do", "Theatre & music"),
    "cinema": ("Do", "Theatre & music"), "concert hall": ("Do", "Theatre & music"),
    "opera": ("Do", "Theatre & music"), "tour operator": ("Do", "Tours"),
    "guide": ("Do", "Tours"), "festival": ("Do", "Festival & event"),
    "event": ("Do", "Festival & event"), "amusement park": ("Do", "Amusement & leisure"),
    "playground": ("Do", "Amusement & leisure"), "school": ("Do", "Course"),
    "market": ("Buy", "Market"), "mall": ("Buy", "Mall & department store"),
    "department store": ("Buy", "Mall & department store"), "shop": ("Buy", "Shop"),
    "supermarket": ("Buy", "Shop"), "bookstore": ("Buy", "Shop"),
    "restaurant": ("Eat", "Restaurant"), "cafe": ("Eat", "Café & bakery"),
    "bakery": ("Eat", "Café & bakery"), "fast food": ("Eat", "Fast food & snack"),
    "snack bar": ("Eat", "Fast food & snack"), "bar": ("Drink", "Bar & pub"),
    "pub": ("Drink", "Bar & pub"), "nightclub": ("Drink", "Club"), "club": ("Drink", "Club"),
    "winery": ("Drink", "Winery & brewery"), "brewery": ("Drink", "Winery & brewery"),
    "hotel": ("Sleep", "Hotel"), "hostel": ("Sleep", "Hostel"),
    "boarding house": ("Sleep", "Guesthouse & B&B"), "guest house": ("Sleep", "Guesthouse & B&B"),
    "bed and breakfast": ("Sleep", "Guesthouse & B&B"), "campsite": ("Sleep", "Camping"),
    "apartment": ("Sleep", "Apartment & resort"), "resort": ("Sleep", "Apartment & resort"),
    "holiday flat": ("Sleep", "Apartment & resort"),
    "airport": ("Go", "Airport"), "train": ("Go", "Station"), "station": ("Go", "Station"),
    "bus": ("Go", "Station"), "bus station": ("Go", "Station"), "metro": ("Go", "Station"),
    "port": ("Go", "Port & ferry"), "ferry": ("Go", "Port & ferry"),
    "car rental": ("Go", "Rental"), "bicycle rental": ("Go", "Rental"),
    "parking": ("Go", "Parking & fuel"), "gas station": ("Go", "Parking & fuel"),
    "tourist information": ("Practical", "Tourist information"),
    "pharmacy": ("Practical", "Health"), "hospital": ("Practical", "Health"),
    "doctor": ("Practical", "Health"), "dentist": ("Practical", "Health"),
    "embassy": ("Practical", "Embassy"), "consulate": ("Practical", "Embassy"),
    "bank": ("Practical", "Money & post"), "atm": ("Practical", "Money & post"),
    "post": ("Practical", "Money & post"), "police": ("Practical", "Police"),
}
# vCard types that are not points of interest but links to other destinations.
VCARD_SKIP = {"city", "town", "village", "region", "district", "country", "island"}

# The Wikipedia map's subcategory for a linked item -> ours, within See and Do.
WIKIMAP_SUB = {
    "Art museum": "Museum", "Museum": "Museum",
    "Cathedral": "Religious site", "Church": "Religious site", "Chapel": "Religious site",
    "Mosque": "Religious site", "Synagogue": "Religious site", "Temple": "Religious site",
    "Monastery": "Religious site", "Place of worship": "Religious site",
    "Castle": "Castle & palace", "Palace": "Castle & palace", "Fort": "Castle & palace",
    "Monument": "Monument & memorial", "Memorial": "Monument & memorial",
    "Statue": "Monument & memorial", "Cemetery": "Monument & memorial",
    "Tower": "Historic building", "Lighthouse": "Historic building",
    "Skyscraper": "Historic building", "House": "Historic building",
    "Building": "Historic building", "Bridge": "Historic building",
    "Windmill": "Historic building", "Watermill": "Historic building",
    "Ruins": "Archaeological site", "Archaeological site": "Archaeological site",
    "Heritage site": "Archaeological site",
    "Park": "Park & garden", "Garden": "Park & garden",
    "Mountain": "Nature & viewpoint", "Hill": "Nature & viewpoint",
    "Volcano": "Nature & viewpoint", "Lake": "Nature & viewpoint",
    "Waterfall": "Nature & viewpoint", "Cave": "Nature & viewpoint",
    "Protected area": "Nature & viewpoint", "Canyon": "Nature & viewpoint",
    "Valley": "Nature & viewpoint", "Island": "Nature & viewpoint",
    "Square": "Square & street", "Street": "Square & street",
    "Zoo": "Zoo & aquarium", "Beach": "Beach & swimming",
    "Theatre": "Theatre & music", "Concert hall": "Theatre & music",
    "Cinema": "Theatre & music", "Stadium": "Outdoors & sport",
    "Sports venue": "Outdoors & sport", "Amusement park": "Amusement & leisure",
    "Airport": "Airport", "Railway station": "Station", "Metro station": "Station",
    "Port": "Port & ferry", "Hotel": "Hotel", "Restaurant": "Restaurant",
    "Brewery": "Winery & brewery", "Shopping centre": "Mall & department store",
}

# Name keywords, English and German, checked in order; the first hit wins.
_KEYWORDS: list[tuple[str, str, str]] = [
    (r"\bhostel|jugendherberge|backpacker", "Sleep", "Hostel"),
    (r"camping|campground|campsite|zeltplatz", "Sleep", "Camping"),
    (r"\bb ?& ?b\b|bed and breakfast|guest ?house|gasthof|gästehaus|pension\b|inn\b|homestay",
     "Sleep", "Guesthouse & B&B"),
    (r"apartment|ferienwohnung|resort|villa\b|lodge\b|bungalow", "Sleep", "Apartment & resort"),
    (r"hotel|motel", "Sleep", "Hotel"),
    (r"café|cafe\b|coffee|bakery|bäckerei|konditorei|patisserie", "Eat", "Café & bakery"),
    (r"pizza|burger|kebab|döner|snack|imbiss|street food|food court|deli\b", "Eat",
     "Fast food & snack"),
    (r"restaurant|bistro|trattoria|osteria|taverna|brasserie|grill\b|kitchen|steakhouse",
     "Eat", "Restaurant"),
    (r"winery|vineyard|weingut|brewery|brauerei|distillery", "Drink", "Winery & brewery"),
    (r"\bclub\b|disco|nightclub", "Drink", "Club"),
    (r"\bpub\b|\bbar\b|kneipe|tavern|biergarten|beer garden|lounge", "Drink", "Bar & pub"),
    (r"museum|gallery|galerie|kunsthalle", "See", "Museum"),
    (r"cathedral|church|kirche|dom\b|münster|chapel|kapelle|mosque|moschee|temple|tempel|"
     r"synagog|monastery|kloster|abbey|abtei|shrine|basilica|basilika|pagoda|wat\b",
     "See", "Religious site"),
    (r"castle|schloss|burg\b|palace|palast|palais|fortress|festung|citadel|chateau|château",
     "See", "Castle & palace"),
    (r"monument|memorial|denkmal|gedenk|statue|mausoleum|cemetery|friedhof", "See",
     "Monument & memorial"),
    (r"ruins|ruine|archaeolog|archäolog|ancient", "See", "Archaeological site"),
    (r"garden|garten|gärten|\bpark\b", "See", "Park & garden"),
    (r"viewpoint|aussicht|lookout|waterfall|wasserfall|\bcave\b|höhle|lake\b|\bsee\b|mount\b",
     "See", "Nature & viewpoint"),
    (r"square|platz\b|plaza|piazza|street|straße|strasse", "See", "Square & street"),
    (r"\bzoo\b|tierpark|aquarium", "See", "Zoo & aquarium"),
    (r"beach|strand|\bpool\b|schwimmbad|therme|\bspa\b|bath", "Do", "Beach & swimming"),
    (r"theatre|theater|opera|oper\b|concert|konzert|philharmon|cinema|kino", "Do",
     "Theatre & music"),
    (r"tour|führung|excursion|cruise", "Do", "Tours"),
    (r"festival|fest\b|carnival|karneval", "Do", "Festival & event"),
    (r"market|markt|bazaar|souk|basar", "Buy", "Market"),
    (r"\bmall\b|shopping cent|einkaufszentrum|department store|kaufhaus", "Buy",
     "Mall & department store"),
    (r"airport|flughafen", "Go", "Airport"),
    (r"station|bahnhof|terminal|busbahnhof", "Go", "Station"),
    (r"ferry|fähre|harbou?r|hafen|\bport\b|pier\b", "Go", "Port & ferry"),
    (r"rental|rent a|verleih|vermietung", "Go", "Rental"),
    (r"tourist info|touristinfo|visitor cent|information cent", "Practical",
     "Tourist information"),
    (r"pharmacy|apotheke|hospital|krankenhaus|klinik|clinic", "Practical", "Health"),
    (r"embassy|botschaft|consulate|konsulat", "Practical", "Embassy"),
    (r"police|polizei", "Practical", "Police"),
]
KEYWORDS = [(re.compile(p, re.I), cat, sub) for p, cat, sub in _KEYWORDS]


def keyword_sub(name: str, category: str) -> str | None:
    """A subcategory of `category` that the listing's name gives away."""
    for pattern, cat, sub in KEYWORDS:
        if cat == category and pattern.search(name):
            return sub
    return None


def subcategory_names() -> dict[str, list[str]]:
    return SUBCATEGORIES
