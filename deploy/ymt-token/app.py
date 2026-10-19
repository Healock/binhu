"""Minimal Ed25519 token issuer for the YMT browser extension.

This service deliberately has no Binhu imports, database access, proxying, or
CORS. Release metadata and the legacy build allowlist are read from local,
operator-managed files on each request so publication and version disablement
take effect without a restart.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


PRIVATE_KEY_PATH = Path("/etc/ymt/signing-private.pem")
BUILDS_PATH = Path("/etc/ymt/builds.json")
RELEASE_PUBLIC_KEY_PATH = Path("/etc/ymt/release-public.pem")
RELEASE_POLICY_PATH = Path("/etc/ymt/release-policy.json")
RELEASE_MANIFEST_PATH = Path("/var/www/ymt/release.json")
RELEASE_ARTIFACT_PATH = Path("/var/www/ymt/ymt-latest.js")
RATE_DB_PATH = Path("/var/log/ymt-token/rate.sqlite3")
KID = os.environ.get("YMT_TOKEN_KID", "zwzz-2026-02")
ISSUANCE_ENABLED = os.environ.get("YMT_TOKEN_ISSUANCE_ENABLED", "false").lower() == "true"
ISSUER = "zwzz-gateway"
AUDIENCE = "binhu-house-candidate"
SCOPE = "house:search:read"
BUILD_RE = re.compile(r"^[0-9a-f]{64}$")
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
IP_RATE_LIMIT = 60
IP_RATE_WINDOW_SECONDS = 60.0
GLOBAL_DAILY_LIMIT = 20000
GLOBAL_DAY_SECONDS = 86400.0

log = logging.getLogger("ymt-token")
_rate_lock = threading.Lock()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build: str


def _response(status: int, code: str, message: str, headers: dict[str, str] | None = None):
    merged = {"Cache-Control": "no-store"}
    if headers:
        merged.update(headers)
    return JSONResponse(status_code=status, content={"code": code, "message": message}, headers=merged)


def _version_tuple(value: Any) -> tuple[int, int, int]:
    if not isinstance(value, str):
        raise ValueError("invalid release version")
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise ValueError("invalid release version")
    return tuple(int(part) for part in match.groups())


def _normalize_community_scope(communities: Any, names: Any) -> tuple[list[str], list[dict[str, str]]]:
    if not isinstance(communities, list) or not isinstance(names, list):
        raise ValueError("invalid community scope")
    if any(not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", code) for code in communities):
        raise ValueError("invalid community code")
    normalized_codes = [code.strip() for code in communities]
    if len(set(normalized_codes)) != len(normalized_codes):
        raise ValueError("duplicate community code")
    normalized_names: list[dict[str, str]] = []
    for community in names:
        if (
            not isinstance(community, dict)
            or set(community) != {"code", "name"}
            or not isinstance(community.get("code"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", community["code"])
            or not isinstance(community.get("name"), str)
            or not community["name"].strip()
        ):
            raise ValueError("invalid community mapping")
        normalized_names.append({"code": community["code"].strip(), "name": community["name"].strip()})
    mapped_codes = [community["code"] for community in normalized_names]
    if len(set(mapped_codes)) != len(mapped_codes) or set(mapped_codes) != set(normalized_codes):
        raise ValueError("community mapping must match authorized communities")
    return normalized_codes, normalized_names


def _load_release_policy() -> dict[str, Any]:
    try:
        data = json.loads(RELEASE_POLICY_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != {"minimum_version", "comm", "communities"}:
            raise ValueError("invalid release policy")
        minimum_version = _version_tuple(data.get("minimum_version"))
        comm, communities = _normalize_community_scope(data.get("comm"), data.get("communities"))
        if not comm:
            raise ValueError("release community scope is empty")
        return {"minimum_version": minimum_version, "comm": comm, "communities": communities}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
        raise RuntimeError("release policy unavailable") from exc


def _load_release_public_key() -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(RELEASE_PUBLIC_KEY_PATH.read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("release key is not Ed25519")
        return key
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm) as exc:
        raise RuntimeError("release public key unavailable") from exc


def _load_signed_release() -> dict[str, Any]:
    try:
        release = json.loads(RELEASE_MANIFEST_PATH.read_text(encoding="utf-8"))
        if not isinstance(release, dict) or set(release) != {"format", "version", "sha256", "bytes", "issued", "sig"}:
            raise ValueError("invalid release manifest")
        if release["format"] != "zwzz-release-v1":
            raise ValueError("unsupported release format")
        version = release["version"]
        _version_tuple(version)
        digest = release["sha256"]
        if not isinstance(digest, str) or not BUILD_RE.fullmatch(digest):
            raise ValueError("invalid release digest")
        size = release["bytes"]
        if type(size) is not int or size <= 0:
            raise ValueError("invalid release size")
        if not isinstance(release["issued"], str) or not release["issued"].strip():
            raise ValueError("invalid release timestamp")
        signature_text = release["sig"]
        if not isinstance(signature_text, str):
            raise ValueError("invalid release signature")
        signature = base64.b64decode(signature_text, validate=True)
        if len(signature) != 64:
            raise ValueError("invalid release signature size")
        message = "\n".join((
            "zwzz-release-v1",
            f"version={version}",
            f"sha256={digest}",
            f"bytes={size}",
        )).encode("utf-8")
        _load_release_public_key().verify(signature, message)
        artifact = RELEASE_ARTIFACT_PATH.read_bytes()
        if len(artifact) != size or not hashlib.sha256(artifact).hexdigest() == digest:
            raise ValueError("release artifact does not match manifest")
        return {"version": version, "version_tuple": _version_tuple(version), "sha256": digest}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, binascii.Error, InvalidSignature) as exc:
        raise RuntimeError("signed release unavailable") from exc


def _load_builds() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(BUILDS_PATH.read_text(encoding="utf-8"))
        builds = data.get("builds", data) if isinstance(data, dict) else None
        if not isinstance(builds, dict):
            raise ValueError("builds must be an object")
        result: dict[str, dict[str, Any]] = {}
        for build, item in builds.items():
            if not isinstance(build, str) or not BUILD_RE.fullmatch(build) or not isinstance(item, dict):
                raise ValueError("invalid build entry")
            version = item.get("version")
            communities = item.get("comm")
            community_names = item.get("communities", [])
            if (
                not isinstance(version, str)
                or not version.strip()
                or not isinstance(communities, list)
                or not isinstance(community_names, list)
            ):
                raise ValueError("invalid build entry")
            if any(not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", code) for code in communities):
                raise ValueError("invalid community entry")
            normalized_communities = [code.strip() for code in communities]
            if len(set(normalized_communities)) != len(normalized_communities):
                raise ValueError("duplicate community code")
            normalized_names: list[dict[str, str]] = []
            for community in community_names:
                if (
                    not isinstance(community, dict)
                    or set(community) != {"code", "name"}
                    or not isinstance(community.get("code"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", community["code"])
                    or not isinstance(community.get("name"), str)
                    or not community["name"].strip()
                ):
                    raise ValueError("invalid community mapping")
                normalized_names.append({"code": community["code"].strip(), "name": community["name"].strip()})
            mapped_codes = [community["code"] for community in normalized_names]
            if len(set(mapped_codes)) != len(mapped_codes) or set(mapped_codes) != set(normalized_communities):
                raise ValueError("community mapping must match authorized communities")
            result[build] = {
                "version": version.strip(),
                "comm": normalized_communities,
                "communities": normalized_names,
                "enabled": item.get("enabled") is True,
            }
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, AttributeError, TypeError) as exc:
        raise RuntimeError("build allowlist unavailable") from exc


def _load_private_key() -> Ed25519PrivateKey:
    try:
        raw = PRIVATE_KEY_PATH.read_bytes()
        key = serialization.load_pem_private_key(raw, password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("private key is not Ed25519")
        return key
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("signing key unavailable") from exc


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _jwt(private_key: Ed25519PrivateKey, build: str, communities: list[str]) -> tuple[str, int]:
    issued = int(time.time())
    payload = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": f"build:{build}",
        "scope": SCOPE,
        "comm": communities,
        "iat": issued,
        "exp": issued + 300,
        "jti": str(uuid.uuid4()),
    }
    header = {"alg": "EdDSA", "typ": "JWT", "kid": KID}
    encoded = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}.{_b64(json.dumps(payload, separators=(',', ':')).encode())}"
    return f"{encoded}.{_b64(private_key.sign(encoded.encode('ascii')))}", issued + 300


def _take_rate(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        with sqlite3.connect(RATE_DB_PATH, timeout=2) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS rate_events (ip TEXT NOT NULL, at REAL NOT NULL)")
            conn.execute("CREATE INDEX IF NOT EXISTS rate_ip_time ON rate_events (ip, at)")
            conn.execute("CREATE INDEX IF NOT EXISTS rate_time ON rate_events (at)")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM rate_events WHERE at <= ?", (now - GLOBAL_DAY_SECONDS,))
            ip_count = conn.execute("SELECT COUNT(*) FROM rate_events WHERE ip=? AND at>?", (ip, now - IP_RATE_WINDOW_SECONDS)).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM rate_events").fetchone()[0]
            if ip_count >= IP_RATE_LIMIT or total >= GLOBAL_DAILY_LIMIT:
                return False
            conn.execute("INSERT INTO rate_events (ip, at) VALUES (?, ?)", (ip, now))
            return True


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, __: RequestValidationError):
    return _response(400, "INVALID_REQUEST", "invalid request")


@app.exception_handler(FastAPIHTTPException)
async def http_error(_: Request, exc: FastAPIHTTPException):
    if exc.status_code == 405:
        return _response(405, "METHOD_NOT_ALLOWED", "method not allowed")
    return _response(exc.status_code, "ERROR", "request failed")


@app.get("/ymt/token/health")
async def health():
    try:
        _load_private_key()
        _load_release_policy()
        _load_signed_release()
    except RuntimeError:
        return _response(500, "SIGNING_UNAVAILABLE", "signing unavailable")
    return JSONResponse(status_code=200, content={"status": "ok", "kid": KID}, headers={"Cache-Control": "no-store"})


@app.post("/ymt/token")
async def issue_token(request: Request, body: TokenRequest):
    if not ISSUANCE_ENABLED:
        return _response(503, "SIGNING_UNAVAILABLE", "signing not enabled")
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    ip = ip[:64]
    try:
        allowed = _take_rate(ip)
    except (sqlite3.Error, OSError):
        return _response(503, "SIGNING_UNAVAILABLE", "rate limiter unavailable")
    if not allowed:
        return _response(429, "RATE_LIMITED", "rate limited", {"Retry-After": "60"})
    build = body.build
    if not BUILD_RE.fullmatch(build):
        log.info("token result=400 ip=%s", ip)
        return _response(400, "INVALID_REQUEST", "invalid request")
    try:
        policy = _load_release_policy()
        release = _load_signed_release()
    except RuntimeError:
        log.error("token result=500 build=%s ip=%s", build, ip)
        return _response(500, "SIGNING_UNAVAILABLE", "signing unavailable")
    if build == release["sha256"]:
        if release["version_tuple"] < policy["minimum_version"]:
            log.info("token result=403 build=%s version=%s ip=%s", build, release["version"], ip)
            return _response(403, "BUILD_NOT_ALLOWED", "build not allowed")
        version = release["version"]
        communities = policy["comm"]
        community_names = policy["communities"]
    else:
        try:
            builds = _load_builds()
        except RuntimeError:
            log.error("token result=500 build=%s ip=%s", build, ip)
            return _response(500, "SIGNING_UNAVAILABLE", "signing unavailable")
        entry = builds.get(build)
        try:
            legacy_version = _version_tuple(entry["version"]) if entry else None
        except ValueError:
            legacy_version = None
        if not entry or not entry["enabled"] or legacy_version is None or legacy_version < policy["minimum_version"]:
            log.info("token result=403 build=%s ip=%s", build, ip)
            return _response(403, "BUILD_NOT_ALLOWED", "build not allowed")
        version = entry["version"]
        communities = entry["comm"]
        community_names = entry["communities"]
    if not communities:
        return _response(503, "NO_COMMUNITY", "no community")
    try:
        token, exp = _jwt(_load_private_key(), build, communities)
    except RuntimeError:
        log.error("token result=500 build=%s ip=%s", build, ip)
        return _response(500, "SIGNING_UNAVAILABLE", "signing unavailable")
    log.info("token result=ok build=%s version=%s ip=%s", build, version, ip)
    return JSONResponse(
        status_code=200,
        content={
            "token": token,
            "exp": exp,
            "comm": communities,
            "communities": community_names,
            "kid": KID,
        },
        headers={"Cache-Control": "no-store"},
    )
