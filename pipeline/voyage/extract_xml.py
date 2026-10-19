"""Stage 2 - articles, redirects and listings out of the Wikivoyage XML dumps.

Input: <lang>wikivoyage-<date>-pages-articles.xml.bz2, one per edition, from
dumps.wikimedia.org (or any mirror). All of them are read for three things:

* **Redirects.** Pageviews are counted against the title somebody typed, so
  "Munich" and "München" are separate rows in the pageview dump. Without
  following redirects the view count of every destination is split across its
  spellings, and the one with the most redirects loses the most.
* **The article itself**: its lead sentence (the line a hover box shows), the
  `{{geo}}` coordinate English uses, and English's status-and-type banner -
  `{{usablecity}}`, `{{outlineregion}}` - which says both what kind of place it
  is and how complete the guide is. German and English also record where an
  article sits in the hierarchy (`{{IsPartOf}}`, `{{IstIn}}`).
* **Listings**, from the editions in config.LISTING_LANGS only: every See / Do /
  Buy / Eat / Drink / Sleep / Go entry with a position. English writes them as
  `{{see|...}}`, `{{listing|type=...}}` and so on; German as `{{vCard|...}}`,
  where the section heading says which of the seven it is.

Wikitext is not parsed properly here, only scanned: templates are matched by
counting braces, which is enough for listings (whose fields routinely hold
nested templates and links) and costs nothing like a real parser would.

Output:
    WORK/pages.parquet      one row per main-namespace page, redirects included
    WORK/listings.parquet   one row per listing

Usage:
    python -m pipeline.voyage.extract_xml [--workers 8]
"""

from __future__ import annotations

import argparse
import bz2
import functools
import html
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import polars as pl

from pipeline.voyage import config

print = functools.partial(print, flush=True)

RE_DUMP = re.compile(r"^([a-z_]+)wikivoyage-\d{8}-pages-articles\.xml\.bz2$")

# ------------------------------------------------------------------ wikitext

RE_TEMPLATE_START = re.compile(r"\{\{\s*([^|{}\n]+?)\s*(?=\||\}\})")
RE_BRACES = re.compile(r"\{\{|\}\}")
RE_PARAM_TOKENS = re.compile(r"\{\{|\}\}|\[\[|\]\]|\|")
RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
RE_REF = re.compile(r"<ref[^>/]*/>|<ref[^>]*>.*?</ref>", re.S | re.I)
RE_TAG = re.compile(r"<[^>]+>")
RE_HEADING = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.M)
RE_EXTLINK = re.compile(r"\[(?:https?:)?//[^\s\]]+\s*([^\]]*)\]")
RE_BOLD = re.compile(r"'{2,5}")
RE_SPACE = re.compile(r"\s+")
RE_SENTENCE_END = re.compile(r"(?<=[.!?。])\s+")


def matching_close(text: str, start: int) -> int:
    """Index just past the `}}` that closes the `{{` at `start`, or -1.

    Counts both braces and square brackets, because a listing's content holds
    links whose `|` must not be read as a field separator.
    """
    depth = 0
    for m in RE_BRACES.finditer(text, start):
        if m.group() == "{{":
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return m.end()
    return -1


def split_params(body: str) -> tuple[list[str], dict[str, str]]:
    """Top-level `|` split of a template body (without its braces and name)."""
    parts, depth_t, depth_l, last = [], 0, 0, 0
    for m in RE_PARAM_TOKENS.finditer(body):
        tok = m.group()
        if tok == "{{":
            depth_t += 1
        elif tok == "}}":
            depth_t -= 1
        elif tok == "[[":
            depth_l += 1
        elif tok == "]]":
            depth_l -= 1
        elif depth_t <= 0 and depth_l <= 0:
            parts.append(body[last : m.start()])
            last = m.end()
    parts.append(body[last:])
    positional, named = [], {}
    for part in parts:
        key, eq, value = part.partition("=")
        if eq and re.fullmatch(r"\s*[\w -]+\s*", key):
            named[key.strip().lower()] = value.strip()
        else:
            positional.append(part.strip())
    return positional, named


def iter_templates(text: str, wanted=None):
    """(name, positional, named, start, end) for each top-level template whose
    lower-cased name is in `wanted` (all of them if None)."""
    pos = 0
    while True:
        m = RE_TEMPLATE_START.search(text, pos)
        if not m:
            return
        start = m.start()
        end = matching_close(text, start)
        if end < 0:
            return
        name = m.group(1).strip().lower().replace("_", " ")
        if wanted is None or name in wanted:
            positional, named = split_params(text[m.end() : end - 2].lstrip("|"))
            yield name, positional, named, start, end
            pos = end
        else:
            # Not one we want: step inside it, since a wanted template can be
            # nested in something else (a listing inside a table cell).
            pos = m.end()


def strip_templates(text: str) -> str:
    """Drop every template, balanced. Used on prose only."""
    out, pos = [], 0
    while True:
        i = text.find("{{", pos)
        if i < 0:
            out.append(text[pos:])
            return "".join(out)
        out.append(text[pos:i])
        end = matching_close(text, i)
        if end < 0:
            return "".join(out)
        pos = end


def strip_links(text: str) -> str:
    """[[a|b]] -> b, [[a]] -> a, and namespaced links (files, categories,
    interwiki) dropped whole - their captions are not prose."""
    out, pos = [], 0
    while True:
        i = text.find("[[", pos)
        if i < 0:
            out.append(text[pos:])
            break
        out.append(text[pos:i])
        depth, j = 0, i
        while j < len(text) - 1:
            two = text[j : j + 2]
            if two == "[[":
                depth += 1; j += 2
            elif two == "]]":
                depth -= 1; j += 2
                if depth == 0:
                    break
            else:
                j += 1
        inner = text[i + 2 : j - 2]
        target, _, label = inner.partition("|")
        if ":" in target and not target.startswith(":"):
            pass  # File:, Category:, interwiki
        else:
            out.append(label or target.lstrip(":"))
        pos = j
    return "".join(out)


def plain(text: str) -> str:
    """Wikitext fragment -> one line of readable text."""
    text = RE_COMMENT.sub("", text)
    text = RE_REF.sub("", text)
    text = strip_templates(text)
    text = strip_links(text)
    text = RE_EXTLINK.sub(r"\1", text)
    text = RE_TAG.sub("", text)
    text = RE_BOLD.sub("", text)
    text = html.unescape(text)
    return RE_SPACE.sub(" ", text).strip()


def clip(text: str, limit: int) -> str:
    """Whole sentences up to `limit` characters, or a word boundary and an
    ellipsis when even the first sentence is longer."""
    if len(text) <= limit:
        return text
    out = ""
    for sentence in RE_SENTENCE_END.split(text):
        if len(out) + len(sentence) + 1 > limit:
            break
        out = f"{out} {sentence}" if out else sentence
    if out:
        return out
    cut = text.rfind(" ", 0, limit - 1)
    return text[: cut if cut > limit // 2 else limit - 1].rstrip(",;:") + "…"


def lead_of(text: str, limit: int = 240) -> str:
    """The first real paragraph before the first heading."""
    head = RE_HEADING.search(text)
    lead = text[: head.start()] if head else text
    lead = RE_COMMENT.sub("", lead)
    lead = strip_templates(lead)
    for para in re.split(r"\n\s*\n", lead):
        para = para.strip()
        if not para or para.startswith(("{|", "|", "!", "*", "#", ":", "<")):
            continue
        line = plain(para)
        if sum(ch.isalpha() for ch in line) >= 30:
            return clip(line, limit)
    return ""


def to_float(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip().replace(",", ".").replace("−", "-")
    try:
        f = float(value)
    except ValueError:
        return None
    return f if f == f else None


def to_qid(value: str | None) -> int | None:
    if not value:
        return None
    m = re.fullmatch(r"\s*[Qq](\d+)\s*", value)
    return int(m.group(1)) if m else None


# ------------------------------------------------------------- article level

# English: {{<status><type>}}, e.g. {{usablecity}}. The status is how complete
# the guide is; the type is what kind of destination the article is about.
STATUSES = ("outline", "usable", "guide", "star")
RE_EN_STATUS = re.compile(
    r"\{\{\s*(outline|usable|guide|star)\s*"
    r"(city|district|region|ruralarea|country|continent|park|airport|diveguide|"
    r"itinerary|topic|phrasebook|event)\s*\}\}",
    re.I,
)
EN_DISAMB = {"disamb", "disambiguation", "dab", "surnames"}
DE_DISAMB = {"begriffsklärung", "bkl"}
PARENT_TEMPLATES = {"ispartof", "istin"}

# ------------------------------------------------------------ listing level

KINDS = ("see", "do", "buy", "eat", "drink", "sleep", "go")
EN_LISTING_TEMPLATES = {"see", "do", "buy", "eat", "drink", "sleep", "go", "listing", "marker"}
# Section headings, for a listing that does not say what it is.
SECTION_KIND = {
    "en": {
        "see": "see", "do": "do", "buy": "buy", "eat": "eat", "drink": "drink",
        "sleep": "sleep", "get in": "go", "get around": "go",
    },
    "de": {
        "sehenswürdigkeiten": "see", "aktivitäten": "do", "einkaufen": "buy",
        "küche": "eat", "nachtleben": "drink", "unterkunft": "sleep",
        "anreise": "go", "mobilität": "go",
    },
}
# English marker types that are not points of interest: a marker of type
# "city" in a region article is a link to another destination, which is
# already on the map as itself.
MARKER_SKIP = {"city", "vicinity", "around", "nearby"}


def section_at(headings: list[tuple[int, str]], pos: int) -> str:
    """Name of the level-2 heading a position falls under."""
    name = ""
    for start, title in headings:
        if start > pos:
            break
        name = title
    return name


def parse_listings(lang: str, title: str, text: str) -> list[dict]:
    headings = [
        (m.start(), plain(m.group(2)).lower())
        for m in RE_HEADING.finditer(text)
        if len(m.group(1)) == 2
    ]
    section_kind = SECTION_KIND.get(lang, {})
    wanted = EN_LISTING_TEMPLATES if lang == "en" else {"vcard"}
    out = []
    for name, _pos, f, start, _end in iter_templates(text, wanted):
        section = section_at(headings, start)
        if lang == "en":
            kind = name if name in KINDS else (f.get("type") or "").lower()
            if name == "marker" and kind in MARKER_SKIP:
                continue
            if kind not in KINDS:
                kind = section_kind.get(section, "other")
            content = f.get("content") or f.get("description") or ""
            vtype = ""
        else:  # de vCard
            vtype = (f.get("type") or "").lower()
            kind = section_kind.get(section, "other")
            content = f.get("description") or ""
        label = plain(f.get("name") or "")
        if not label:
            continue
        out.append(
            {
                "lang": lang,
                "article": title,
                "idx": len(out),
                "section": section,
                "kind": kind,
                "vtype": vtype,
                "name": clip(label, 120),
                "alt": clip(plain(f.get("alt") or ""), 80),
                "lat": to_float(f.get("lat")),
                "lon": to_float(f.get("long") or f.get("lon")),
                "wikidata": to_qid(f.get("wikidata")),
                "has_url": bool((f.get("url") or "").strip()),
                "has_image": bool((f.get("image") or "").strip()),
                "has_wikipedia": bool((f.get("wikipedia") or "").strip()),
                "content": clip(plain(content), 260),
            }
        )
    return out


def parse_article(lang: str, page_id: int, title: str, text: str) -> dict:
    row = {
        "lang": lang,
        "page_id": page_id,
        "title": title,
        "redirect": None,
        "status": None,
        "dest_type": None,
        "parent": None,
        "geo_lat": None,
        "geo_lon": None,
        "disamb": False,
        "lead": "",
        "n_listings": 0,
    }
    if lang == "en":
        m = RE_EN_STATUS.search(text)
        if m:
            row["status"] = m.group(1).lower()
            row["dest_type"] = m.group(2).lower()
    disamb = EN_DISAMB | DE_DISAMB
    for name, positional, named, _s, _e in iter_templates(text):
        if name == "geo" and row["geo_lat"] is None and len(positional) >= 2:
            lat, lon = to_float(positional[0]), to_float(positional[1])
            if lat is not None and lon is not None and abs(lat) <= 90 and abs(lon) <= 180:
                row["geo_lat"], row["geo_lon"] = lat, lon
        elif name in PARENT_TEMPLATES and row["parent"] is None and positional:
            row["parent"] = plain(positional[0]) or None
        elif name in disamb:
            row["disamb"] = True
    row["lead"] = lead_of(text)
    return row


# ------------------------------------------------------------------- driver


def scan_dump(path: str) -> tuple[str, list[dict], list[dict], float]:
    started = time.perf_counter()
    lang = RE_DUMP.match(Path(path).name).group(1)
    take_listings = lang in config.LISTING_LANGS
    pages: list[dict] = []
    listings: list[dict] = []
    ns = ""
    with bz2.open(path, "rb") as fh:
        for event, elem in ET.iterparse(fh, events=("end",)):
            tag = elem.tag
            if not ns and "}" in tag:
                ns = tag[: tag.index("}") + 1]
            if tag != f"{ns}page":
                continue
            if elem.findtext(f"{ns}ns") != "0":
                elem.clear()
                continue
            title = elem.findtext(f"{ns}title") or ""
            page_id = int(elem.findtext(f"{ns}id") or 0)
            redirect = elem.find(f"{ns}redirect")
            if redirect is not None:
                pages.append(
                    {"lang": lang, "page_id": page_id, "title": title,
                     "redirect": redirect.get("title")}
                )
            else:
                text = elem.findtext(f"{ns}revision/{ns}text") or ""
                row = parse_article(lang, page_id, title, text)
                if take_listings and not row["disamb"]:
                    found = parse_listings(lang, title, text)
                    row["n_listings"] = len(found)
                    listings.extend(found)
                pages.append(row)
            elem.clear()
    return lang, pages, listings, time.perf_counter() - started


PAGE_SCHEMA = {
    "lang": pl.String, "page_id": pl.Int64, "title": pl.String, "redirect": pl.String,
    "status": pl.String, "dest_type": pl.String, "parent": pl.String,
    "geo_lat": pl.Float64, "geo_lon": pl.Float64, "disamb": pl.Boolean,
    "lead": pl.String, "n_listings": pl.Int32,
}
LISTING_SCHEMA = {
    "lang": pl.String, "article": pl.String, "idx": pl.Int32, "section": pl.String,
    "kind": pl.String, "vtype": pl.String, "name": pl.String, "alt": pl.String,
    "lat": pl.Float64, "lon": pl.Float64, "wikidata": pl.Int64,
    "has_url": pl.Boolean, "has_image": pl.Boolean, "has_wikipedia": pl.Boolean,
    "content": pl.String,
}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    dumps = sorted(p for p in config.XML_DIR.glob("*.xml.bz2") if RE_DUMP.match(p.name))
    if not dumps:
        raise SystemExit(f"no <lang>wikivoyage-*-pages-articles.xml.bz2 in {config.XML_DIR}")
    print(f"{len(dumps)} editions; listings from {', '.join(config.LISTING_LANGS)}")
    started = time.perf_counter()
    all_pages, all_listings = [], []
    # Biggest first, so the long poles start immediately.
    dumps.sort(key=lambda p: -p.stat().st_size)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        for lang, pages, listings, seconds in pool.map(scan_dump, map(str, dumps)):
            articles = sum(1 for p in pages if p.get("redirect") is None)
            print(
                f"  {lang:4s} {articles:7,d} articles {len(pages) - articles:7,d} redirects "
                f"{len(listings):8,d} listings  {seconds:5.0f} s"
            )
            all_pages.extend(pages)
            all_listings.extend(listings)

    pages_df = pl.DataFrame(all_pages, schema=PAGE_SCHEMA, strict=False)
    listings_df = pl.DataFrame(all_listings, schema=LISTING_SCHEMA, strict=False)
    pages_df.write_parquet(config.PAGES_PARQUET)
    listings_df.write_parquet(config.LISTINGS_PARQUET)
    print(f"\nwrote {len(pages_df):,} pages and {len(listings_df):,} listings")
    print(listings_df.group_by("lang", "kind").len().sort("lang", "len", descending=[False, True]))
    print(f"  {(time.perf_counter() - started) / 60:.1f} min")


if __name__ == "__main__":
    main()
