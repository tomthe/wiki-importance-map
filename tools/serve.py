"""A static file server that understands HTTP Range. Development only.

`python -m http.server` cannot serve this map. The tiles and the search index
live in packed files that the browser reads a few kilobytes at a time with a
`Range` header, and the standard library's handler ignores that header
completely: it answers 200 with the whole file, so one tile request becomes a
90 MB download. Every real host - GitHub Pages, Cloudflare, nginx, Apache -
does this correctly; only the dev server needed fixing.

    python tools/serve.py                 # http://localhost:8000/
    python tools/serve.py --port 9000 --dir .

Also sets Cache-Control to zero, so an edit to a source file shows up on
reload, and CORS to `*`, so the page can be served from one port and the data
from another.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeHandler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".mjs": "text/javascript",
        ".js": "text/javascript",
        ".json": "application/json",
    }

    def end_headers(self) -> None:
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_head(self):
        """Answer 206 for a satisfiable range, and fall back otherwise."""
        header = self.headers.get("Range")
        if not header:
            return super().send_head()
        match = RANGE_RE.match(header.strip())
        if not match:
            return super().send_head()

        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            handle = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None

        try:
            size = os.fstat(handle.fileno()).st_size
            first, last = match.group(1), match.group(2)
            if first == "":
                # "bytes=-500" - the final 500 bytes.
                length = int(last or 0)
                start = max(0, size - length)
                end = size - 1
            else:
                start = int(first)
                end = int(last) if last else size - 1
                end = min(end, size - 1)
            if start > end or start >= size:
                handle.close()
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None

            handle.seek(start)
            self._remaining = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", self.guess_type(path))
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(self._remaining))
            self.end_headers()
            return _Slice(handle, self._remaining)
        except Exception:
            handle.close()
            raise


class _Slice:
    """Just enough of a file object for copyfile() to send one range."""

    def __init__(self, handle, remaining: int) -> None:
        self.handle = handle
        self.remaining = remaining

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        want = self.remaining if size is None or size < 0 else min(size, self.remaining)
        chunk = self.handle.read(want)
        self.remaining -= len(chunk)
        return chunk

    def close(self) -> None:
        self.handle.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--dir", default=".")
    args = ap.parse_args()

    handler = partial(RangeHandler, directory=os.path.abspath(args.dir))
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"serving {os.path.abspath(args.dir)} on http://{args.host}:{args.port}/")
    print("HTTP Range supported - stop with Ctrl-C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
