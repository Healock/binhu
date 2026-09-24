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
from xml.etree import ElementTree

BUCKET = "binhu-update"
PUBLIC_ENDPOINT = "oss-cn-shanghai.aliyuncs.com"
UPLOAD_ENDPOINT = "binhu-update.oss-cn-shanghai.aliyuncs.com"
INTERNAL_ENDPOINT = "binhu-update.oss-cn-shanghai-internal.aliyuncs.com"
KEY_RE = re.compile(r"^client-transfer/[0-9]+\.[0-9]+\.[0-9]+/[0-9a-f]{40}/binhu-clients-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz$")
UPLOAD_ID_RE = re.compile(r"^[A-Za-z0-9+/_=-]{1,256}$")
# The GitHub-hosted runner has a very low and variable upload rate to this
# bucket. Keep each request small enough to finish before an idle write timeout;
# the server-side pull remains a single streaming download from the ECS-local
# endpoint.
PART_SIZE = 1 * 1024 * 1024
PART_ATTEMPTS = 4
PART_TIMEOUT = 300


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


def signature(secret: str, method: str, date_or_expires: str, key: str, content_type: str = "", content_md5: str = "", subresource: str = "") -> str:
    resource = canonical_resource(key) + ("?" + subresource if subresource else "")
    string_to_sign = "\n".join((method, content_md5, content_type, date_or_expires, resource))
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


def oss_request(endpoint: str, key: str, method: str, subresource: str = "", body: bytes | None = None, content_type: str = "", timeout: int = PART_TIMEOUT) -> tuple[int, dict[str, str], bytes]:
    access_key, secret = credentials()
    date = email.utils.formatdate(usegmt=True)
    request_path = "/" + quote(key, safe="/-_.~") + ("?" + subresource if subresource else "")
    headers = {
        "Date": date,
        "Authorization": "OSS " + access_key + ":" + signature(secret, method, date, key, content_type, subresource=subresource),
    }
    if content_type:
        headers["Content-Type"] = content_type
    connection = http.client.HTTPSConnection(endpoint, timeout=timeout)
    try:
        connection.request(method, request_path, body=body, headers=headers)
        response = connection.getresponse()
        # OSS error documents are deliberately not logged: they can contain
        # request identifiers and signed object details.
        content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise RuntimeError("OSS response exceeded size limit")
        return response.status, {name.lower(): value for name, value in response.getheaders()}, content
    finally:
        connection.close()


def retryable_request(endpoint: str, key: str, method: str, subresource: str = "", body: bytes | None = None, content_type: str = "") -> tuple[int, dict[str, str], bytes]:
    for attempt in range(1, PART_ATTEMPTS + 1):
        try:
            status, headers, content = oss_request(endpoint, key, method, subresource, body, content_type)
        except (OSError, TimeoutError) as exc:
            if attempt == PART_ATTEMPTS:
                raise RuntimeError(f"OSS {method} transport failed after {attempt} attempts") from exc
        else:
            if status in (408, 429) or 500 <= status <= 599:
                if attempt == PART_ATTEMPTS:
                    raise RuntimeError(f"OSS {method} failed with HTTP {status} after {attempt} attempts")
            else:
                return status, headers, content
        time.sleep(min(2 ** (attempt - 1), 8))
    raise AssertionError("unreachable")


def require_status(status: int, expected: int, operation: str) -> None:
    if status != expected:
        raise RuntimeError(f"OSS {operation} failed with HTTP {status}")


def upload(path: Path, endpoint: str, key: str) -> None:
    validate_key(key)
    if endpoint != UPLOAD_ENDPOINT:
        raise SystemExit(f"upload endpoint must be {UPLOAD_ENDPOINT}")
    if not path.is_file():
        raise SystemExit(f"bundle does not exist: {path}")
    credentials()
    size = path.stat().st_size
    if size <= 0:
        raise SystemExit("bundle is empty")
    status, _, content = oss_request(endpoint, key, "POST", "uploads")
    require_status(status, 200, "multipart initialization")
    try:
        upload_id = ElementTree.fromstring(content).findtext("{*}UploadId", "")
    except ElementTree.ParseError as exc:
        raise RuntimeError("OSS multipart initialization returned invalid XML") from exc
    if not UPLOAD_ID_RE.fullmatch(upload_id):
        raise RuntimeError("OSS multipart initialization returned invalid upload ID")
    parts: list[tuple[int, str]] = []
    try:
        with path.open("rb") as source:
            for part_number in range(1, (size + PART_SIZE - 1) // PART_SIZE + 1):
                chunk = source.read(PART_SIZE)
                if not chunk:
                    raise RuntimeError("bundle ended during multipart upload")
                query = urlencode({"partNumber": part_number, "uploadId": upload_id})
                status, headers, _ = retryable_request(endpoint, key, "PUT", query, chunk)
                require_status(status, 200, f"part {part_number}")
                etag = headers.get("etag", "")
                if not re.fullmatch(r'"?[0-9a-fA-F]{32}"?', etag):
                    raise RuntimeError(f"OSS part {part_number} returned invalid ETag")
                parts.append((part_number, etag))
                print(f"uploaded_part={part_number}/{(size + PART_SIZE - 1) // PART_SIZE}", flush=True)
        root = ElementTree.Element("CompleteMultipartUpload")
        for number, etag in parts:
            part = ElementTree.SubElement(root, "Part")
            ElementTree.SubElement(part, "PartNumber").text = str(number)
            ElementTree.SubElement(part, "ETag").text = etag
        body = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
        status, _, content = oss_request(endpoint, key, "POST", urlencode({"uploadId": upload_id}), body, "application/xml")
        require_status(status, 200, "multipart completion")
        try:
            response_root = ElementTree.fromstring(content)
        except ElementTree.ParseError as exc:
            raise RuntimeError("OSS multipart completion returned invalid XML") from exc
        if response_root.tag.rsplit("}", 1)[-1] != "CompleteMultipartUploadResult":
            raise RuntimeError("OSS multipart completion did not confirm the object")
    except BaseException:
        # A failed transfer must not leave billable parts behind. The abort is
        # best effort; it must never replace the original error.
        try:
            oss_request(endpoint, key, "DELETE", urlencode({"uploadId": upload_id}), timeout=20)
        except (OSError, RuntimeError):
            pass
        raise
    print(f"object_key={key}")
    print(f"object_size={size}")


def delete_object(endpoint: str, key: str) -> None:
    """Delete one completed transfer object from the fixed private bucket."""
    validate_key(key)
    if endpoint != UPLOAD_ENDPOINT:
        raise SystemExit(f"delete endpoint must be {UPLOAD_ENDPOINT}")
    credentials()
    status, _, _ = oss_request(endpoint, key, "DELETE", timeout=60)
    if status not in (200, 204):
        raise RuntimeError(f"OSS object deletion failed with HTTP {status}")
    print(f"deleted_object_key={key}")


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
    delete_parser = subparsers.add_parser("delete")
    delete_parser.add_argument("--endpoint", default=UPLOAD_ENDPOINT)
    delete_parser.add_argument("--key", required=True)
    args = parser.parse_args(argv)
    if args.command == "upload":
        upload(args.file, args.endpoint, args.key)
    elif args.command == "presign":
        print(presigned_url(args.endpoint, args.key, args.expires_in))
    else:
        delete_object(args.endpoint, args.key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
