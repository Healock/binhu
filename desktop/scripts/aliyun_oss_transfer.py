#!/usr/bin/env python3
"""Upload and presign a fixed Binhu client-release object in Aliyun OSS.

This intentionally uses the OSS v1 REST signature instead of an SDK so the
workflow has a small, auditable dependency surface. Credentials are read only
from the process environment and are never printed.
"""

from __future__ import annotations

import argparse
import base64
import email.utils
import hashlib
import hmac
import http.client
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

BUCKET = "binhu-update"
PUBLIC_ENDPOINT = "oss-cn-shanghai.aliyuncs.com"
UPLOAD_ENDPOINT = "binhu-update.oss-cn-shanghai.aliyuncs.com"
INTERNAL_ENDPOINT = "binhu-update.oss-cn-shanghai-internal.aliyuncs.com"
KEY_RE = re.compile(r"^client-transfer/[0-9]+\.[0-9]+\.[0-9]+/[0-9a-f]{40}/binhu-clients-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz$")


def credentials() -> tuple[str, str]:
    access_key = os.environ.get("ALIYUN_OSS_ACCESS_KEY_ID", "")
    secret = os.environ.get("ALIYUN_OSS_ACCESS_KEY_SECRET", "")
    if not access_key or not secret:
        raise SystemExit("ALIYUN_OSS_ACCESS_KEY_ID and ALIYUN_OSS_ACCESS_KEY_SECRET are required")
    return access_key, secret


def validate_key(key: str) -> None:
    if not KEY_RE.fullmatch(key):
        raise SystemExit("object key is outside the fixed client-transfer namespace")


def canonical_resource(key: str) -> str:
    return "/" + BUCKET + "/" + quote(key, safe="/-_.~")


def signature(secret: str, method: str, date_or_expires: str, key: str, content_type: str = "", content_md5: str = "") -> str:
    string_to_sign = "\n".join((method, content_md5, content_type, date_or_expires, canonical_resource(key)))
    return base64.b64encode(hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha1).digest()).decode()


def presigned_url(endpoint: str, key: str, expires_in: int) -> str:
    validate_key(key)
    if endpoint != INTERNAL_ENDPOINT:
        raise SystemExit(f"presign endpoint must be {INTERNAL_ENDPOINT}")
    if not 60 <= expires_in <= 7200:
        raise SystemExit("expires-in must be between 60 and 7200 seconds")
    access_key, secret = credentials()
    expires = str(int(time.time()) + expires_in)
    query = urlencode({
        "OSSAccessKeyId": access_key,
        "Expires": expires,
        "Signature": signature(secret, "GET", expires, key),
    })
    return f"https://{endpoint}/{quote(key, safe='/-_.~')}?{query}"


def upload(path: Path, endpoint: str, key: str) -> None:
    validate_key(key)
    if endpoint != UPLOAD_ENDPOINT:
        raise SystemExit(f"upload endpoint must be {UPLOAD_ENDPOINT}")
    if not path.is_file():
        raise SystemExit(f"bundle does not exist: {path}")
    access_key, secret = credentials()
    size = path.stat().st_size
    if size <= 0:
        raise SystemExit("bundle is empty")
    date = email.utils.formatdate(usegmt=True)
    content_type = "application/gzip"
    auth = "OSS " + access_key + ":" + signature(secret, "PUT", date, key, content_type)
    request_path = "/" + quote(key, safe="/-_.~")
    connection = http.client.HTTPSConnection(endpoint, timeout=300)
    try:
        with path.open("rb") as source:
            connection.putrequest("PUT", request_path, skip_accept_encoding=True)
            connection.putheader("Host", endpoint)
            connection.putheader("Date", date)
            connection.putheader("Content-Type", content_type)
            connection.putheader("Content-Length", str(size))
            connection.putheader("Authorization", auth)
            connection.endheaders()
            while True:
                chunk = source.read(8 * 1024 * 1024)
                if not chunk:
                    break
                connection.send(chunk)
        response = connection.getresponse()
        response.read()
        if not 200 <= response.status < 300:
            raise SystemExit(f"OSS upload failed with HTTP {response.status}")
    finally:
        connection.close()
    print(f"object_key={key}")
    print(f"object_size={size}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    upload_parser = subparsers.add_parser("upload")
    upload_parser.add_argument("--file", type=Path, required=True)
    upload_parser.add_argument("--endpoint", default=UPLOAD_ENDPOINT)
    upload_parser.add_argument("--key", required=True)
    presign_parser = subparsers.add_parser("presign")
    presign_parser.add_argument("--endpoint", default=INTERNAL_ENDPOINT)
    presign_parser.add_argument("--key", required=True)
    presign_parser.add_argument("--expires-in", type=int, default=3600)
    args = parser.parse_args(argv)
    if args.command == "upload":
        upload(args.file, args.endpoint, args.key)
    else:
        print(presigned_url(args.endpoint, args.key, args.expires_in))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
