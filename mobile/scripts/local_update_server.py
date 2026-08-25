#!/usr/bin/env python3
"""Local Android update feed with byte-range support for POC validation."""

from __future__ import annotations

import argparse
import re
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


class UpdateRequestHandler(SimpleHTTPRequestHandler):
    server_version = "BinhuLocalUpdate/1"

    def translate_path(self, path: str) -> str:
        relative = Path(unquote(urlparse(path).path).lstrip("/"))
        if relative.is_absolute() or ".." in relative.parts:
            return str(Path(self.directory) / ".invalid")
        return str(Path(self.directory).joinpath(*relative.parts))

    def end_headers(self) -> None:
        if self.path.endswith(("manifest.stable.json", "policy.stable.json")):
            self.send_header("Cache-Control", "no-store")
        else:
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def send_head(self):
        path = Path(self.translate_path(self.path))
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return None
        size = path.stat().st_size
        content_type = self.guess_type(str(path))
        range_header = self.headers.get("Range")
        if not range_header:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            self._remaining = size
            return path.open("rb")
        match = RANGE_RE.fullmatch(range_header.strip())
        if not match or not match.group(1):
            self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            return None
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else size - 1
        if start >= size or start > end:
            self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        end = min(end, size - 1)
        self.send_response(HTTPStatus.PARTIAL_CONTENT)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        stream = path.open("rb")
        stream.seek(start)
        self._remaining = end - start + 1
        return stream

    def copyfile(self, source, outputfile) -> None:
        remaining = getattr(self, "_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        while remaining:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not args.root.is_dir():
        raise SystemExit(f"update root does not exist: {args.root}")
    handler = lambda *values, **keywords: UpdateRequestHandler(  # noqa: E731
        *values,
        directory=str(args.root),
        **keywords,
    )
    with ThreadingHTTPServer((args.host, args.port), handler) as server:
        print(f"Serving {args.root} at http://{args.host}:{args.port}/", flush=True)
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
