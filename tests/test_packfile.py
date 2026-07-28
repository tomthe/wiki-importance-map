"""The packing layer, Python writer against JavaScript reader.

Same reasoning as test_tile_format: the offsets are computed twice, once when
writing and once when reading, in two languages. An off-by-one here does not
break loudly - it hands the decoder somebody else's bytes.

Covers the three places that arithmetic lives:

  * PackWriter splitting into parts without letting a blob straddle one, and
    the flat offset space staying continuous across the split
  * the per-zoom tile index, where the client recovers offsets by prefix-summing
    lengths onto a base
  * the search directory, which does the same for prefixes
  * coalesce(), which decides how many requests a viewport costs

    python -m tests.test_packfile
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from pipeline.build_search import directory_blob
from pipeline.build_tiles import zoom_index
from pipeline.packfile import PackWriter, part_starts
from tests.test_tile_format import find_node

REPO = Path(__file__).resolve().parent.parent


def run_js(node, env, args: list[str]):
    proc = subprocess.run(
        node + [str(REPO / "tests" / "pack_check.mjs"), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=REPO,
    )
    if proc.returncode != 0:
        raise AssertionError(f"pack_check exited {proc.returncode}: {proc.stderr[:800]}")
    return json.loads(proc.stdout)


def check_writer(tmp: Path) -> list[str]:
    """Blobs come back byte for byte, and no blob crosses a part boundary."""
    problems = []
    blobs = [bytes([i % 251]) * (7 + i * 13) for i in range(40)]
    with PackWriter(tmp, "unit", part_bytes=200) as pack:
        placed = [pack.add(blob) for blob in blobs]
    info = pack.info()
    starts = part_starts(info["parts"])

    if info["bytes"] != sum(len(b) for b in blobs):
        problems.append("total byte count wrong")
    if len(info["parts"]) < 3:
        problems.append(f"expected several parts at 200 bytes each, got {info['parts']}")

    # Spelled out rather than taken from PackWriter, because src/pack.js builds
    # the same name independently in urlOf() - and the `.bin` is load-bearing:
    # Cloudflare caches by extension, so renaming these silently stops the CDN
    # caching them.
    name_of = lambda part: f"unit.{part:03d}.bin"
    for part in range(len(info["parts"])):
        if not (tmp / name_of(part)).exists():
            problems.append(f"expected a part named {name_of(part)}")
    if problems:
        return problems

    # Read every blob back the way the client does: pick the part, seek, read.
    for blob, (offset, length) in zip(blobs, placed):
        part = max(i for i, s in enumerate(starts) if offset >= s)
        local = offset - starts[part]
        if local + length > info["parts"][part]:
            problems.append(f"blob at {offset} straddles the end of part {part}")
            continue
        data = (tmp / name_of(part)).read_bytes()[local : local + length]
        if data != blob:
            problems.append(f"blob at {offset} came back wrong")
    return problems


def check_zoom_index(node, env, tmp: Path) -> list[str]:
    keys = [0, 1, (3 << 16) | 4, (255 << 16) | 255, 0xFFFF0000]
    lengths = [11, 2044, 7, 300000, 1]
    base = 5_000_000_000  # past 2^32, where a u32 offset table would wrap
    path = tmp / "zoom.gz"
    path.write_bytes(zoom_index(keys, lengths))
    got = run_js(node, env, ["zoomindex", str(path), str(base)])

    problems = []
    if got["keys"] != keys:
        problems.append(f"keys differ: {got['keys']}")
    if got["lengths"] != lengths:
        problems.append(f"lengths differ: {got['lengths']}")
    expected, running = [], base
    for length in lengths:
        expected.append(running)
        running += length
    if got["offsets"] != expected:
        problems.append(f"offsets differ: {got['offsets']} vs {expected}")
    return problems


def check_directory(node, env, tmp: Path) -> list[str]:
    prefixes = ["a", "ab", "abc", "ähn", "北京", "z"]
    lengths = [10, 20, 30, 44, 55, 6]
    base = 1234
    path = tmp / "dir.gz"
    path.write_bytes(directory_blob(prefixes, lengths))
    got = run_js(node, env, ["directory", str(path), str(base)])

    problems = []
    running = base
    for prefix, length in zip(prefixes, lengths):
        if got.get(prefix) != [running, length]:
            problems.append(f"{prefix!r} -> {got.get(prefix)}, expected [{running}, {length}]")
        running += length
    if len(got) != len(prefixes):
        problems.append(f"{len(got)} entries, expected {len(prefixes)}")
    return problems


def check_coalesce(node, env, tmp: Path) -> list[str]:
    spec = {
        "gap": 100,
        "maxRun": 10_000,
        "partStarts": [0, 1000],
        "items": [
            # Out of order on purpose: the caller hands over whatever the
            # viewport scan produced.
            {"key": "d", "offset": 560, "length": 10},
            {"key": "a", "offset": 0, "length": 50},
            {"key": "b", "offset": 60, "length": 40},     # 10-byte gap: merge
            {"key": "c", "offset": 500, "length": 10},    # 400-byte gap: split
            {"key": "f", "offset": 1000, "length": 10},   # next part: split
            {"key": "e", "offset": 990, "length": 10},    # 420-byte gap: split
        ],
    }
    path = tmp / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    runs = run_js(node, env, ["coalesce", str(path)])

    expected = [
        {"start": 0, "length": 100, "keys": ["a", "b"]},
        {"start": 500, "length": 70, "keys": ["c", "d"]},
        {"start": 990, "length": 10, "keys": ["e"]},
        # Adjacent to e, but in the other part, so it cannot share a request.
        {"start": 1000, "length": 10, "keys": ["f"]},
    ]
    return [] if runs == expected else [f"runs differ:\n  got {runs}\n  want {expected}"]


def main() -> None:
    node, env = find_node()
    failures = 0
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        for name, check in [
            ("pack writer parts and offsets", lambda: check_writer(tmp)),
            ("zoom index round trip", lambda: check_zoom_index(node, env, tmp)),
            ("search directory round trip", lambda: check_directory(node, env, tmp)),
            ("range coalescing", lambda: check_coalesce(node, env, tmp)),
        ]:
            problems = check()
            if problems:
                failures += 1
                print(f"FAIL {name}")
                for line in problems:
                    print(f"   {line}")
            else:
                print(f"ok   {name}")

    if failures:
        sys.exit(f"{failures} check(s) failed")
    print("pack layer round-trips")


if __name__ == "__main__":
    main()
