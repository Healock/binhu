"""Minimal Ed25519 token issuer for the YMT browser extension.

This service deliberately has no Binhu imports, database access, proxying, or
CORS. The build allowlist is read from a root-owned file on each request so
version disablement takes effect without a restart.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict


PRIVATE_KEY_PATH = Path("/etc/ymt/signing-private.pem")
BUILDS_PATH = Path("/etc/ymt/builds.json")
KID = "zwzz-2026-01"
ISSUER = "zwzz-gateway"
AUDIENCE = "binhu-house-candidate"
SCOPE = "house:search:read"
BUILD_RE = re.compile(r"^[0-9a-f]{64}$")
IP_RATE_LIMIT = 60
IP_RATE_WINDOW_SECONDS = 60.0
GLOBAL_DAILY_LIMIT = 20000
GLOBAL_DAY_SECONDS = 86400.0

log = logging.getLogger("ymt-token")
_rate_lock = threading.Lock()
_rate_events: dict[str, deque[float]] = defaultdict(deque)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    build: str


def _response(status: int, code: str, message: str, headers: dict[str, str] | None = None):
    merged = {"Cache-Control": "no-store"}
    if headers:
        merged.update(headers)
    return JSONResponse(status_code=status, content={"code": code, "message": message}, headers=merged)


def _load_builds() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(BUILDS_PATH.read_text(encoding="utf-8"))
        builds = data.get("builds") if isinstance(data, dict) else None
        if not isinstance(builds, dict):
            raise ValueError("builds must be an object")
        result: dict[str, dict[str, Any]] = {}
        for build, item in builds.items():
            if not isinstance(build, str) or not BUILD_RE.fullmatch(build) or not isinstance(item, dict):
                raise ValueError("invalid build entry")
            version = item.get("version")
            communities = item.get("comm")
            if not isinstance(version, str) or not version.strip() or not isinstance(communities, list):
                raise ValueError("invalid build entry")
            if any(not isinstance(code, str) or not code.strip() for code in communities):
                raise ValueError("invalid community entry")
            result[build] = {"version": version.strip(), "comm": [code.strip() for code in communities], "enabled": item.get("enabled") is True}
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
    now = time.monotonic()
    with _rate_lock:
        events = _rate_events[f"ip:{ip}"]
        cutoff = now - IP_RATE_WINDOW_SECONDS
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= IP_RATE_LIMIT:
            return False
        events.append(now)
        global_events = _rate_events["global-day"]
        day_cutoff = now - GLOBAL_DAY_SECONDS
        while global_events and global_events[0] <= day_cutoff:
            global_events.popleft()
        if len(global_events) >= GLOBAL_DAILY_LIMIT:
            return False
        global_events.append(now)
        if len(_rate_events) > 2048:
            for key, queued in list(_rate_events.items()):
                while queued and queued[0] <= cutoff:
                    queued.popleft()
                if not queued:
                    del _rate_events[key]
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
    except RuntimeError:
        return _response(500, "SIGNING_UNAVAILABLE", "signing unavailable")
    return JSONResponse(status_code=200, content={"status": "ok", "kid": KID}, headers={"Cache-Control": "no-store"})


@app.post("/ymt/token")
async def issue_token(request: Request, body: TokenRequest):
    ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    ip = ip[:64]
    build = body.build.strip().lower()
    if not BUILD_RE.fullmatch(build):
        log.info("token result=400 build=%s ip=%s", build or "invalid", ip)
        return _response(400, "INVALID_REQUEST", "invalid request")
    try:
        builds = _load_builds()
    except RuntimeError:
        log.error("token result=500 build=%s ip=%s", build, ip)
        return _response(500, "SIGNING_UNAVAILABLE", "signing unavailable")
    entry = builds.get(build)
    if not entry or not entry["enabled"]:
        log.info("token result=403 build=%s ip=%s", build, ip)
        return _response(403, "BUILD_NOT_ALLOWED", "build not allowed")
    if not entry["comm"]:
        return _response(503, "NO_COMMUNITY", "no community")
    if not _take_rate(ip):
        log.info("token result=429 build=%s ip=%s", build, ip)
        return _response(429, "RATE_LIMITED", "rate limited", {"Retry-After": "5"})
    try:
        token, exp = _jwt(_load_private_key(), build, entry["comm"])
    except RuntimeError:
        log.error("token result=500 build=%s ip=%s", build, ip)
        return _response(500, "SIGNING_UNAVAILABLE", "signing unavailable")
    log.info("token result=ok build=%s version=%s ip=%s", build, entry["version"], ip)
    return JSONResponse(status_code=200, content={"token": token, "exp": exp, "comm": entry["comm"], "kid": KID}, headers={"Cache-Control": "no-store"})
