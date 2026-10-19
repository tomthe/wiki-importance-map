"""The wikitext scanner the travel map's listings come from.

Listings are templates whose fields hold other templates and links - a phone
number in `{{phone|...}}`, a price in `{{EUR|5}}`, `[[Sanssouci|the palace]]`
in the description - and every `|` inside those must not be read as a field
separator. A regex cannot count braces, which is why extract_xml.py does, and
this checks it on the shapes that break naive splitting.

    python -m tests.test_voyage_wikitext
"""

from __future__ import annotations

import sys

from pipeline.voyage.extract_xml import (
    clip,
    lead_of,
    parse_article,
    parse_listings,
    plain,
    split_params,
)

EN_ARTICLE = """{{pagebanner|Potsdam banner.jpg}}
'''Potsdam''' is the capital of [[Brandenburg]] and borders [[Berlin|the capital]]. It is widely known for its palaces.

==Get in==
For central Potsdam, travel to {{marker|type=go|name=Potsdam Hbf|lat=52.3917|long=13.0667}}.
A region marker that is not a listing: {{marker|type=city|name=Berlin|lat=52.5|long=13.4}}.

==See==
* {{see
| name=Sanssouci Palace | alt=Schloss Sanssouci | url=https://example.org | email=
| lat=52.4043 | long=13.0384 | phone={{phone|+49 331|9694-200}}
| wikipedia=Sanssouci | wikidata=Q151910
| content=A [[rococo]] palace with a {{EUR|19}} ticket; see [[Park Sanssouci|the park]] too.
}}
* {{listing | type=do | name=Boat tour | lat= | long= | content=Round trip on the Havel. }}
* {{listing | name=Untyped in See | lat=52.40 | long=13.05 }}

==Eat==
* {{eat | name=Café ''Heider'' | lat=52,4015 | long=13.06 | content= }}

{{usablecity}}
{{geo|52.4|13.067|zoom=12}}
{{IsPartOf|Brandenburg}}
"""

DE_ARTICLE = """'''Potsdam''' ist die Landeshauptstadt Brandenburgs. Die Stadt ist für ihre Schlösser bekannt.

== Sehenswürdigkeiten ==
{{vCard | type = park | name = Park Sanssouci | wikidata = Q449256 | description = Der Park zählt zu den schönsten Gärten. }}

== Unterkunft ==
{{vCard | type = hotel | name = Hotel am Luisenplatz | lat = 52.4 | long = 13.04 }}

== Praktische Hinweise ==
{{vCard | type = pharmacy | name = Apotheke | lat = 52.39 | long = 13.06 }}
{{IstIn|Havelland}}
"""


def check(name: str, ok: bool, detail: str = "") -> int:
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f": {detail}"))
    return 0 if ok else 1


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    failures = 0

    positional, named = split_params(
        " a | b=[[x|y]] | c={{t|1|2}} | d = e=f "
    )
    failures += check(
        "split respects nested links and templates, and '=' inside a value",
        positional == ["a"] and named == {"b": "[[x|y]]", "c": "{{t|1|2}}", "d": "e=f"},
        f"{positional} {named}",
    )

    failures += check(
        "plain() keeps link labels and drops templates and files",
        plain("A [[rococo]] palace, [[Park|the park]], {{EUR|19}} [[File:x.jpg|thumb|cap]] '''bold'''")
        == "A rococo palace, the park, bold",
        plain("A [[rococo]] palace, [[Park|the park]], {{EUR|19}} [[File:x.jpg|thumb|cap]] '''bold'''"),
    )

    failures += check(
        "clip() stops at a sentence, or at a word with an ellipsis",
        clip("One. Two three. Four.", 16) == "One. Two three."
        and clip("abcdefghij klmnopqrstuvwxyz", 15).endswith("…"),
        f"{clip('One. Two three. Four.', 16)!r}",
    )

    lead = lead_of(EN_ARTICLE)
    failures += check(
        "lead is the first paragraph, as text",
        lead.startswith("Potsdam is the capital of Brandenburg and borders the capital."),
        lead,
    )

    art = parse_article("en", 1, "Potsdam", EN_ARTICLE)
    failures += check(
        "English status, type, parent and {{geo}}",
        (art["status"], art["dest_type"], art["parent"], art["geo_lat"], art["geo_lon"])
        == ("usable", "city", "Brandenburg", 52.4, 13.067),
        str(art),
    )

    en = parse_listings("en", "Potsdam", EN_ARTICLE)
    names = [l["name"] for l in en]
    failures += check(
        "English listings found, region markers skipped",
        names == ["Potsdam Hbf", "Sanssouci Palace", "Boat tour", "Untyped in See", "Café Heider"],
        str(names),
    )
    by = {l["name"]: l for l in en}
    s = by.get("Sanssouci Palace", {})
    failures += check(
        "listing fields survive nested templates in other fields",
        s.get("lat") == 52.4043 and s.get("lon") == 13.0384 and s.get("wikidata") == 151910
        and s.get("alt") == "Schloss Sanssouci" and s.get("has_url") and s.get("has_wikipedia")
        and s.get("content") == "A rococo palace with a ticket; see the park too.",
        str(s),
    )
    failures += check(
        "kind from the template, from type=, and from the section heading",
        [by[n]["kind"] for n in ("Potsdam Hbf", "Sanssouci Palace", "Boat tour", "Untyped in See",
                                 "Café Heider")]
        == ["go", "see", "do", "see", "eat"],
        str([(l["name"], l["kind"]) for l in en]),
    )
    failures += check(
        "an empty lat is no coordinate, and a decimal comma is read",
        by["Boat tour"]["lat"] is None and by["Café Heider"]["lat"] == 52.4015,
        f"{by['Boat tour']['lat']} {by['Café Heider']['lat']}",
    )

    de = parse_listings("de", "Potsdam", DE_ARTICLE)
    failures += check(
        "German vCards take their kind from the section and keep type=",
        [(l["name"], l["kind"], l["vtype"]) for l in de]
        == [("Park Sanssouci", "see", "park"), ("Hotel am Luisenplatz", "sleep", "hotel"),
            ("Apotheke", "other", "pharmacy")],
        str([(l["name"], l["kind"], l["vtype"]) for l in de]),
    )
    failures += check(
        "German parent from {{IstIn}}",
        parse_article("de", 2, "Potsdam", DE_ARTICLE)["parent"] == "Havelland",
    )

    if failures:
        sys.exit(f"{failures} check(s) failed")
    print("\nwikitext scanning behaves")


if __name__ == "__main__":
    main()
