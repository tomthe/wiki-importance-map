"""N-Triples literal helpers.

Wikidata's .nt serialisation escapes every non-ASCII character as \\uXXXX, so
literals need unescaping before they are worth storing.
"""

import re

_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\U([0-9a-fA-F]{8})|\\(.)")
_SIMPLE = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
    '"': '"',
    "'": "'",
    "\\": "\\",
}


def _replace(m: "re.Match") -> str:
    short, long, simple = m.group(1), m.group(2), m.group(3)
    if short is not None:
        return chr(int(short, 16))
    if long is not None:
        return chr(int(long, 16))
    return _SIMPLE.get(simple, simple)


def unescape(raw: bytes) -> str:
    """Decode an N-Triples literal body to text."""
    text = raw.decode("utf-8", "replace")
    if "\\" not in text:
        return text
    text = _ESCAPE.sub(_replace, text)
    # Astral characters arrive as a 😀 surrogate pair and only join
    # into one code point after a utf-16 round trip. Anything still holding a
    # surrogate cannot be encoded, which is also what parquet would choke on,
    # so use the failed encode as the (cheap, C-level) detector.
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        try:
            text = text.encode("utf-16", "surrogatepass").decode("utf-16")
        except UnicodeDecodeError:
            text = text.encode("utf-8", "replace").decode("utf-8")
    return text


_PERCENT = re.compile(r"%([0-9a-fA-F]{2})")


def percent_decode(text: str) -> str:
    """Decode %XX sequences in Commons/Wikipedia URLs."""
    if "%" not in text:
        return text
    raw = _PERCENT.sub(lambda m: chr(int(m.group(1), 16)), text)
    try:
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return raw
