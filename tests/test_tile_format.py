"""Round-trip check: Python encoder -> JavaScript decoder.

The tile format is written in one language and read in another, so the only
check worth having is one that runs both. This encodes awkward tiles (odd row
counts, which exercise the alignment padding; astral-plane and RTL titles;
items with and without an English article; every optional field present and
absent) and asserts the browser decoder reproduces them exactly.

    python -m tests.test_tile_format

Needs a JS runtime. Set NODE_BIN, or it will look for node on PATH and then
fall back to VS Code's bundled Electron.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

from pipeline.build_tiles import (
    FLAG_HAS_IMAGE,
    FLAG_HAS_WEBSITE,
    FLAG_HAS_WIKI,
    FLAG_LOC_MASK,
    FLAG_LOC_SHIFT,
    NO_INT16,
    NO_POP,
    NO_REF,
    encode_tile,
)


def loc(code: int) -> int:
    """The flag bits for a location source, e.g. loc(1) = "born here"."""
    return code << FLAG_LOC_SHIFT

REPO = Path(__file__).resolve().parent.parent
COUNTRIES = ["United Kingdom", "Japan", "Israel", "Germany", "Philippines", "France"]


def find_node() -> tuple[list[str], dict[str, str]]:
    env = dict(os.environ)
    explicit = env.get("NODE_BIN")
    if explicit:
        return [explicit], env
    on_path = shutil.which("node")
    if on_path:
        return [on_path], env
    vscode = Path(
        env.get("LOCALAPPDATA", "")
    ) / "Programs" / "Microsoft VS Code" / "Code.exe"
    if vscode.exists():
        env["ELECTRON_RUN_AS_NODE"] = "1"
        return [str(vscode)], env
    sys.exit("no JS runtime found; set NODE_BIN to a node executable")


def row(
    qid,
    lon,
    lat,
    score,
    pr,
    qr,
    cat,
    sub,
    flags,
    title,
    wiki,
    *,
    descr="",
    admin="",
    country=NO_REF,
    pop=NO_POP,
    elev=NO_INT16,
    year=NO_INT16,
    sitelinks=0,
) -> dict:
    return dict(
        qid=qid, lon=lon, lat=lat, score=score, pr=pr, qr=qr, cat=cat, sub=sub,
        flags=flags, title=title, wiki=wiki, descr=descr, admin=admin,
        country=country, pop=pop, elev=elev, year=year, sitelinks=sitelinks,
    )


CASES = [
    {
        "name": "unicode, and every optional field used",
        # "en|" = English article whose title equals the drawn label
        "rows": [
            row(90, -0.1276, 51.5072, 65535, 60000, 64000, 1, 2,
                FLAG_HAS_WIKI | FLAG_HAS_IMAGE | FLAG_HAS_WEBSITE, "London", "en|",
                descr="capital of the United Kingdom", admin="Greater London",
                country=0, pop=8866180, elev=11, year=47, sitelinks=255),
            row(1490, 139.6917, 35.6895, 65000, 59000, 65535, 1, 2, FLAG_HAS_WIKI,
                "東京", "en|Tokyo", descr="capital of Japan", admin="関東地方",
                country=1, pop=13929286, elev=40, year=1457, sitelinks=254),
            row(3766, 34.7818, 32.0853, 40000, 30000, 35000, 1, 2, FLAG_HAS_WIKI,
                "תל אביב", "en|Tel Aviv", descr="city in Israel",
                admin="Tel Aviv District", country=2, pop=460613, sitelinks=120),
            # only a non-English article, and the title differs from the label
            row(5678, 11.582, 48.1351, 30000, 20000, 25000, 1, 2, FLAG_HAS_WIKI,
                "Munich", "de|München", descr="capital of Bavaria, Germany",
                admin="Upper Bavaria", country=3, pop=1512491, elev=520, year=1158),
            # a bot-made wiki with a longer language code, and no extras at all
            row(91011, 121.0, 14.0, 900, 400, 500, 2, 1, FLAG_HAS_WIKI,
                "Barangay X", "ceb|", country=4),
            # below sea level, and a founding date BC
            row(4242, 35.5, 31.5, 8000, 4000, 5000, 2, 0, 0, "Dead Sea shore", "",
                descr="lowest land on Earth", elev=-11034, year=-800),
            row(12345, 2.3522, 48.8566, 100, 50, 70, 2, 1, 0, "Sans article 😀", "",
                descr="an item with an emoji in its name", country=5, admin="Île-de-France"),
            row(7, -74.006, 40.7128, 1, 0, 0, 0, 0, 0, "Q7 only", ""),
        ],
    },
    {
        "name": "single row, no admin table",
        "rows": [
            row(1, 0.0, 0.0, 32768, 100, 200, 8, 3, FLAG_HAS_WIKI, "Null Island",
                "en|Null_Island", pop=0)
        ],
    },
    {
        # The location-source bits share the flags byte with the three
        # has-a-thing bits, so the case that matters is both sets at once: a
        # shift or mask that is off by one still passes if only one is used.
        "name": "every location source, alongside the other flag bits",
        "rows": [
            row(937, 10.0, 48.4, 50000, 40000, 30000, 9, 1,
                FLAG_HAS_WIKI | FLAG_HAS_IMAGE | loc(1), "Albert Einstein", "en|",
                descr="theoretical physicist", admin="Ulm", country=3, year=1879,
                sitelinks=255),
            row(859, -74.66, 40.35, 40000, 30000, 20000, 9, 1,
                FLAG_HAS_WIKI | loc(2), "Someone Who Died", "en|",
                admin="Princeton", country=5),
            row(12418, 2.3364, 48.8606, 60000, 50000, 55000, 6, 17,
                FLAG_HAS_WIKI | FLAG_HAS_IMAGE | FLAG_HAS_WEBSITE | loc(3),
                "Mona Lisa", "en|", descr="painting by Leonardo da Vinci",
                admin="Louvre", country=5, year=1503),
            row(95, 0.0, 51.5, 30000, 20000, 25000, 8, 26,
                FLAG_HAS_WIKI | loc(4), "A Company", "en|", admin="London",
                country=0),
            row(42, 1.0, 2.0, 20000, 10000, 15000, 6, 22, loc(5),
                "A Novel", "", admin="Somewhere"),
            row(43, 3.0, 4.0, 20000, 10000, 15000, 4, 12,
                FLAG_HAS_WEBSITE | loc(6), "A Ship", "", admin="Portsmouth"),
            row(44, 5.0, 6.0, 20000, 10000, 15000, 9, 2,
                FLAG_HAS_IMAGE | loc(7), "Someone Associated", "",
                admin="Vienna"),
            # Code 0 next to them: an ordinary item must stay non-derived.
            row(45, 7.0, 8.0, 20000, 10000, 15000, 1, 2, FLAG_HAS_WIKI,
                "An Actual Place", "en|", admin="Vienna"),
        ],
    },
    {
        # Three rows, so the u16 block is 14*3 bytes and the encoder has to pad
        # before the u8 block. An even count hides that bug completely.
        "name": "odd row count, sharing one admin area, values at the limits",
        "rows": [
            row(2, 180.0, -85.0, 5, 1, 2, 3, 0, 0, "Edge A", "", admin="Nowhere"),
            row(3, -180.0, 85.0, 6, 2, 3, 4, 1, 0, "Edge B", "", admin="Nowhere",
                year=32767, elev=32767, pop=NO_POP - 1, sitelinks=255),
            row(4, 0.0, 0.0, 7, 3, 4, 5, 2, 0, "Edge C", "", admin="Nowhere",
                year=NO_INT16 + 1, elev=NO_INT16 + 1, pop=0),
        ],
    },
]


def build(rows: list[dict]) -> tuple[bytes, list[dict]]:
    def col(name, dtype):
        return np.array([r[name] for r in rows], dtype=dtype)

    payload = encode_tile(
        {
            "qid": col("qid", np.uint32),
            "lon": col("lon", np.float64),
            "lat": col("lat", np.float64),
            "score": col("score", np.uint16),
            "pr": col("pr", np.uint16),
            "qr": col("qr", np.uint16),
            "pop": col("pop", np.uint32),
            "country": col("country", np.uint16),
            "elev": col("elev", np.int16),
            "year": col("year", np.int16),
            "sitelinks": col("sitelinks", np.uint8),
            "cat": col("cat", np.uint8),
            "sub": col("sub", np.uint8),
            "flags": col("flags", np.uint8),
            "title": [r["title"] for r in rows],
            "wiki": [r["wiki"] for r in rows],
            "descr": [r["descr"] for r in rows],
            "admin": [r["admin"] for r in rows],
        },
        z=5,
        x=17,
        y=11,
    )
    expected = []
    for r in rows:
        wiki = wiki_lang = None
        if r["flags"] & FLAG_HAS_WIKI:
            lang, _, article = r["wiki"].partition("|")
            wiki_lang = lang
            wiki = article or r["title"]
        expected.append(
            {
                "qid": r["qid"],
                "lon": round(float(np.float32(r["lon"])), 4),
                "lat": round(float(np.float32(r["lat"])), 4),
                "title": r["title"],
                "wiki": wiki,
                "wikiLang": wiki_lang,
                "descr": r["descr"],
                "hasImage": bool(r["flags"] & FLAG_HAS_IMAGE),
                "hasWebsite": bool(r["flags"] & FLAG_HAS_WEBSITE),
                "locSrc": (r["flags"] >> FLAG_LOC_SHIFT) & FLAG_LOC_MASK,
                "derived": ((r["flags"] >> FLAG_LOC_SHIFT) & FLAG_LOC_MASK) != 0,
                "cat": r["cat"],
                "sub": r["sub"],
                "pop": None if r["pop"] == NO_POP else r["pop"],
                "elev": None if r["elev"] == NO_INT16 else r["elev"],
                "year": None if r["year"] == NO_INT16 else r["year"],
                "sitelinks": r["sitelinks"],
                "country": None if r["country"] == NO_REF else COUNTRIES[r["country"]],
                "admin": r["admin"] or None,
                "score": round(r["score"] / 65535, 3),
                "pr": round(r["pr"] / 65535, 3),
                "qr": round(r["qr"] / 65535, 3),
            }
        )
    return payload, expected


def main() -> None:
    # The fixtures are deliberately full of astral-plane and RTL titles, and a
    # cp1252 console cannot print them - so a real mismatch used to die inside
    # the diff instead of showing it.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    node, env = find_node()
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        countries_path = Path(tmp) / "countries.json"
        countries_path.write_text(json.dumps(COUNTRIES), encoding="utf-8")
        for case in CASES:
            payload, expected = build(case["rows"])
            path = Path(tmp) / "tile.bin"
            path.write_bytes(payload)
            proc = subprocess.run(
                node
                + [
                    str(REPO / "tests" / "decode_tile.mjs"),
                    str(path),
                    str(countries_path),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                env=env,
                cwd=REPO,
            )
            if proc.returncode != 0:
                print(f"FAIL {case['name']}: decoder exited {proc.returncode}")
                print(proc.stderr.strip()[:2000])
                failures += 1
                continue
            got = json.loads(proc.stdout)
            if got["z"] != 5 or got["x"] != 17 or got["y"] != 11:
                print(f"FAIL {case['name']}: header {got['z']}/{got['x']}/{got['y']}")
                failures += 1
                continue
            if got["items"] != expected:
                print(f"FAIL {case['name']}: rows differ")
                for a, b in zip(got["items"], expected):
                    if a != b:
                        print(f"   decoded {a}")
                        print(f"   expected {b}")
                failures += 1
                continue
            print(f"ok   {case['name']} ({len(expected)} rows, {len(payload)} bytes)")

    if failures:
        sys.exit(f"{failures} case(s) failed")
    print("tile format round-trips")


if __name__ == "__main__":
    main()
