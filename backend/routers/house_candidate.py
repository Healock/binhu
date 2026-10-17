"""Narrow, read-only machine endpoint for address-based house candidates."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import uuid
from typing import Any

import redis.asyncio as redis
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from fastapi import APIRouter, Body, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from config import settings
from database import db_manager


router = APIRouter(prefix="/api/house-candidate", tags=["房屋地址只读候选"])
ISSUER = "zwzz-gateway"
AUDIENCE = "binhu-house-candidate"
SCOPE = "house:search:read"
KID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}")
BUILD_PATTERN = re.compile(r"build:[0-9a-f]{64}")
CODE_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,40}")
log = logging.getLogger("binhu.house-candidate")

_LIMIT_SCRIPT = """
local replay = redis.call('SET', KEYS[1], '1', 'EX', ARGV[1], 'NX')
if not replay then return 1 end
local device_second = redis.call('INCR', KEYS[2])
if device_second == 1 then redis.call('EXPIRE', KEYS[2], 2) end
local device_day = redis.call('INCR', KEYS[3])
if device_day == 1 then redis.call('EXPIRE', KEYS[3], 90000) end
if device_second > 5 or device_day > 3000 then return 2 end
return 0
"""


class SearchBody(BaseModel):
    keyword: str
    community: str
    limit: int = 20

    @field_validator("keyword", "community")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("keyword")
    @classmethod
    def validate_keyword_length(cls, value: str) -> str:
        if not 3 <= len(value) <= 40:
            raise ValueError("keyword length must be between 3 and 40")
        return value

    @field_validator("community")
    @classmethod
    def validate_community_length(cls, value: str) -> str:
        if not 1 <= len(value) <= 40:
            raise ValueError("community length must be between 1 and 40")
        return value


class InvalidBearer(Exception):
    pass


def _b64url_decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise InvalidBearer
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _configured_public_key(kid: str):
    try:
        keys = json.loads(settings.HOUSE_CANDIDATE_PUBLIC_KEYS_JSON)
        pem = keys.get(kid) if isinstance(keys, dict) else None
        if not isinstance(pem, str) or not KID_PATTERN.fullmatch(kid):
            raise InvalidBearer
        key = serialization.load_pem_public_key(pem.encode("ascii"))
        if key.__class__.__name__ != "Ed25519PublicKey":
            raise InvalidBearer
        return key
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidBearer from exc


def _verify_bearer(authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise InvalidBearer
    parts = authorization[7:].split(".")
    if len(parts) != 3:
        raise InvalidBearer
    try:
        header = json.loads(_b64url_decode(parts[0]))
        claims = json.loads(_b64url_decode(parts[1]))
        if not isinstance(header, dict) or header.get("alg") != "EdDSA":
            raise InvalidBearer
        if header.get("typ") != "JWT" or not isinstance(header.get("kid"), str):
            raise InvalidBearer
        key = _configured_public_key(header["kid"])
        key.verify(_b64url_decode(parts[2]), f"{parts[0]}.{parts[1]}".encode("ascii"))
    except (InvalidBearer, InvalidSignature, ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise InvalidBearer from exc

    now = int(time.time())
    if not isinstance(claims, dict):
        raise InvalidBearer
    iat, exp = claims.get("iat"), claims.get("exp")
    if (isinstance(iat, bool) or not isinstance(iat, int)
            or isinstance(exp, bool) or not isinstance(exp, int)
            or iat > now + 60 or exp < now - 60 or exp <= iat
            or exp - iat > 300):
        raise InvalidBearer
    if claims.get("iss") != ISSUER or claims.get("aud") != AUDIENCE:
        raise InvalidBearer
    if not isinstance(claims.get("jti"), str):
        raise InvalidBearer
    try:
        parsed_jti = uuid.UUID(claims["jti"])
        if parsed_jti.version != 4 or str(parsed_jti) != claims["jti"].lower():
            raise InvalidBearer
    except (ValueError, AttributeError) as exc:
        raise InvalidBearer from exc
    if not isinstance(claims.get("sub"), str) or not BUILD_PATTERN.fullmatch(claims["sub"]):
        raise InvalidBearer
    if not isinstance(claims.get("comm"), list) or not claims["comm"]:
        raise InvalidBearer
    if any(not isinstance(code, str) or not CODE_PATTERN.fullmatch(code) for code in claims["comm"]):
        raise InvalidBearer
    return claims


async def _enforce_replay_and_limits(claims: dict[str, Any], source_ip: str) -> None:
    client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    now = int(time.time())
    ttl = max(1, int(claims["exp"]) - now)
    ip_key = hashlib.sha256(source_ip.encode("utf-8", "replace")).hexdigest()
    day = time.strftime("%Y%m%d", time.gmtime(now))
    keys = [
        f"binhu:house-candidate:jti:{claims['jti']}",
        f"binhu:house-candidate:ip-second:{ip_key}:{now}",
        f"binhu:house-candidate:ip-day:{ip_key}:{day}",
    ]
    error: Exception | None = None
    try:
        result = await client.eval(_LIMIT_SCRIPT, len(keys), *keys, ttl)
    except Exception as exc:
        error = exc
    finally:
        try:
            await client.aclose()
        except Exception as exc:
            if error is None:
                error = exc
    if error is not None:
        raise HTTPException(503, detail={"code": "AUTHORIZATION_UNAVAILABLE"}) from error
    if result == 1:
        raise HTTPException(401, detail={"code": "TOKEN_REPLAYED"})
    if result == 2:
        raise HTTPException(
            429,
            detail={"code": "RATE_LIMITED"},
            headers={"Retry-After": "1"},
        )


async def _resolve_community_id(code: str) -> int:
    try:
        pool = db_manager.get_pool("online_data")
    except ValueError as exc:
        raise HTTPException(502, detail={"code": "DATA_UNAVAILABLE"}) from exc
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT c.id FROM _qmf_organization_codes code "
                "JOIN _communities c ON c.id=code.community_id "
                "WHERE code.organization_code=%s AND code.is_active=1 AND c.is_active=1",
                (code,),
            )
            rows = await cur.fetchall()
    except Exception as exc:
        raise HTTPException(502, detail={"code": "DATA_UNAVAILABLE"}) from exc
    finally:
        pool.release(conn)
    if len(rows) != 1:
        raise HTTPException(403, detail={"code": "COMMUNITY_NOT_AUTHORIZED"})
    return int(rows[0][0])


@router.post("/search")
async def search_house_candidates(
    request: Request,
    response: Response,
    payload: dict[str, Any] = Body(...),
    authorization: str | None = Header(default=None),
):
    if not settings.HOUSE_CANDIDATE_ENABLED:
        raise HTTPException(503, detail={"code": "ENDPOINT_DISABLED"})
    try:
        body = SearchBody.model_validate(payload)
    except Exception as exc:
        raise HTTPException(400, detail={"code": "INVALID_REQUEST"}) from exc
    if not 1 <= body.limit <= 20:
        raise HTTPException(400, detail={"code": "INVALID_LIMIT"})
    try:
        claims = _verify_bearer(authorization)
    except InvalidBearer as exc:
        raise HTTPException(401, detail={"code": "INVALID_TOKEN"}) from exc
    if claims.get("scope") != SCOPE:
        raise HTTPException(403, detail={"code": "INSUFFICIENT_SCOPE"})
    if body.community not in claims["comm"]:
        raise HTTPException(403, detail={"code": "COMMUNITY_NOT_AUTHORIZED"})
    source_ip = request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")
    source_ip = source_ip.strip()[:64] or "unknown"
    await _enforce_replay_and_limits(claims, source_ip)
    community_id = await _resolve_community_id(body.community)

    try:
        pool = db_manager.get_pool("registry")
    except ValueError as exc:
        raise HTTPException(502, detail={"code": "DATA_UNAVAILABLE"}) from exc
    conn = await pool.acquire()
    escaped = body.keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT COUNT(*) FROM registry_properties "
                "WHERE community_id=%s AND status='active' "
                "AND source_house_no REGEXP '^[A-Za-z0-9]{6}$' "
                "AND (normalized_address LIKE %s OR natural_address LIKE %s)",
                (community_id, pattern, pattern),
            )
            total = int((await cur.fetchone())[0])
            await cur.execute(
                "SELECT source_house_no, "
                "COALESCE(NULLIF(normalized_address,''),natural_address), "
                "community_name_snapshot "
                "FROM registry_properties "
                "WHERE community_id=%s AND status='active' "
                "AND source_house_no REGEXP '^[A-Za-z0-9]{6}$' "
                "AND (normalized_address LIKE %s OR natural_address LIKE %s) "
                "ORDER BY (normalized_address=%s) DESC, "
                "CHAR_LENGTH(COALESCE(NULLIF(normalized_address,''),natural_address)), id "
                "LIMIT %s",
                (community_id, pattern, pattern, body.keyword, body.limit + 1),
            )
            rows = await cur.fetchall()
    except Exception as exc:
        raise HTTPException(502, detail={"code": "DATA_UNAVAILABLE"}) from exc
    finally:
        pool.release(conn)

    truncated = len(rows) > body.limit
    items = [
        {
            "house_no": str(row[0]),
            "address": str(row[1] or ""),
            "community": str(row[2] or ""),
        }
        for row in rows[:body.limit]
    ]
    build = claims["sub"].split(":", 1)[1]
    log.info(
        "house_candidate result=ok jti=%s build=%s keyword=%s returned=%d total=%d ip=%s",
        claims["jti"], build, json.dumps(body.keyword, ensure_ascii=True), len(items), total, source_ip,
    )
    response.headers["Cache-Control"] = "no-store"
    return {"items": items, "total": total, "truncated": truncated}
