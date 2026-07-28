"""One file instead of a hundred thousand: a range-addressed blob pack.

Both the tile pyramid and the search index used to be tens of thousands of
tiny files. That is the worst possible shape for this data. It breaks shared
hosting inode quotas, makes `git status` crawl, and - because a 4 KiB
filesystem cluster is bigger than most of the blobs - it inflated 816 MB of
bytes into 1.1 GB of disk.

So blobs go into a handful of big files instead, and the client asks for one
with an HTTP `Range` header. Every static host worth using serves ranges:
GitHub Pages (Fastly), Cloudflare, nginx, Apache, and the dev server in
tools/serve.py. Python's own `http.server` does *not*, which is why that dev
server exists.

Why parts rather than literally one file
----------------------------------------
GitHub blocks any single file over 100 MiB on push - a hard limit, not a
warning - and GitHub Pages does not resolve Git LFS pointers, so LFS is not a
way out. One 400 MB pack would rule out the host the author actually wants.
The pack is therefore split into parts of at most `part_bytes` (90 MiB by
default), which keeps every part comfortably under the limit while still
collapsing 98,786 files into five.

Addressing
----------
Callers see one flat address space: `add()` returns a global offset, and the
parts are just that space cut into pieces. A blob never straddles a cut, so a
read is always one request. The client resolves an offset to a part by walking
the (very short) list of part sizes in the manifest.

    tiles.000.bin   tiles.001.bin   ...
    |<--- part 0 --->|<--- part 1 --->|
    0            94371840         188743680      global offsets
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_PART_BYTES = 90 * 1024 * 1024  # under GitHub's 100 MiB hard limit


class PackWriter:
    """Append-only writer over a numbered set of part files.

    Usage:
        with PackWriter(out_dir, "tiles") as pack:
            offset, length = pack.add(blob)
        info = pack.info()      # goes straight into manifest.json
    """

    def __init__(
        self,
        out_dir: Path,
        stem: str,
        part_bytes: int = DEFAULT_PART_BYTES,
    ) -> None:
        self.dir = Path(out_dir)
        self.stem = stem
        self.part_bytes = part_bytes
        self.dir.mkdir(parents=True, exist_ok=True)
        self.part_sizes: list[int] = []
        self.total = 0
        self._handle = None
        self._current = 0
        self._open_part()

    # ------------------------------------------------------------------ parts

    def _part_path(self, index: int) -> Path:
        # The .bin matters. Cloudflare caches by file extension, not MIME type,
        # and `bin` is on its default list where `000` is not - so a name like
        # tiles.000.bin is cached in front of the origin with no configuration,
        # and tiles.pack.000 would need a Cache Rule to get the same thing.
        return self.dir / f"{self.stem}.{index:03d}.bin"

    def _open_part(self) -> None:
        self._handle = self._part_path(len(self.part_sizes)).open("wb")
        self._current = 0

    def _close_part(self) -> None:
        if self._handle is None:
            return
        self._handle.close()
        self._handle = None
        self.part_sizes.append(self._current)

    # ------------------------------------------------------------------ blobs

    def add(self, blob: bytes) -> tuple[int, int]:
        """Append one blob. Returns its (global offset, length)."""
        size = len(blob)
        # Roll over rather than straddle, so one blob is always one request.
        if self._current and self._current + size > self.part_bytes:
            self._close_part()
            self._open_part()
        offset = self.total
        self._handle.write(blob)
        self._current += size
        self.total += size
        return offset, size

    def close(self) -> None:
        self._close_part()

    def __enter__(self) -> "PackWriter":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --------------------------------------------------------------- manifest

    def info(self) -> dict:
        if self._handle is not None:
            raise RuntimeError("call close() before info()")
        oversized = [
            f"{self._part_path(i).name} is {n / 1e6:.0f} MB"
            for i, n in enumerate(self.part_sizes)
            if n > 100 * 1024 * 1024
        ]
        if oversized:
            # Only reachable if a single blob is bigger than part_bytes.
            print(f"  WARNING: over GitHub's 100 MiB limit: {', '.join(oversized)}")
        return {
            "stem": self.stem,
            "parts": self.part_sizes,
            "bytes": self.total,
        }


def remove_parts(out_dir: Path, stem: str) -> int:
    """Delete a previous build's parts.

    A rebuild with fewer parts than last time would otherwise leave orphans
    behind, and an orphan here is 90 MB that nothing references. `.pack.NNN` is
    the naming this used before the parts were renamed to end in `.bin`; it is
    matched so upgrading does not silently strand the old set.
    """
    removed = 0
    for pattern in (f"{stem}.*.bin", f"{stem}.pack.*"):
        for path in Path(out_dir).glob(pattern):
            if path.is_file():
                path.unlink()
                removed += 1
    return removed


def part_starts(part_sizes: list[int]) -> list[int]:
    """Cumulative start offset of each part - the client does the same sum."""
    starts, running = [], 0
    for size in part_sizes:
        starts.append(running)
        running += size
    return starts
