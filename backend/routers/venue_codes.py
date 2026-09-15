"""场所码：独立的场所目录、二维码和匿名登记功能。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import io
import secrets
import time
import zipfile
import re
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
from openpyxl import Workbook
from pydantic import BaseModel, Field

from config import settings
from database import db_manager
from deps import require_permission
from services.audit import record_admin_audit, request_audit_fields
from services.permissions import VENUE_EXPORT, VENUE_MANAGE, VENUE_VIEW
from services.qmf_config import decrypt_secret, encrypt_secret
from services.registry_security import hmac_digest, normalize_identity, normalize_phone
from services.venue_cloud import enqueue_public_form_outbox, enqueue_venue_cloud_outbox, get_venue_cloud_status


router = APIRouter(tags=["场所码"])
admin_router = APIRouter(prefix="/api", tags=["场所码"])
_rate_state: dict[str, tuple[float, int]] = {}
_PHOTO_MAGIC = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/webp": (b"RIFF",),
}
_FORM_TOKEN_SUFFIX_LENGTH = 22
_VENUE_SELECT = (
    "id,name,venue_type,address,community_id,community_name_snapshot,status,token_hmac,encrypted_token,"
    "created_by,created_at,updated_at,config_revision,token_version,cloud_sync_status,cloud_synced_revision,"
    "cloud_synced_at,cloud_sync_error_code,pending_token_version"
)


async def get_venue_db():
    try:
        pool = db_manager.get_pool("registry")
    except ValueError as exc:
        raise HTTPException(503, "场所码数据库尚未完成初始化") from exc
    conn = await pool.acquire()
    try:
        yield conn
    finally:
        pool.release(conn)


class VenueCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    venue_type: str = Field(default="", max_length=80)
    address: str = Field(default="", max_length=500)
    community_id: int | None = Field(default=None, gt=0)
    community_name: str = Field(default="", max_length=200)
    status: str = Field(default="active", pattern="^(active|inactive)$")


class PublicFormStatus(BaseModel):
    status: str = Field(pattern="^(active|inactive)$")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _token_digest(token: str) -> str:
    return hmac.new(settings.registry_hmac_key.encode(), f"venue-token:{token}".encode(), hashlib.sha256).hexdigest()


def _public_venue_url(token: str) -> str:
    configured_url = (
        settings.VENUE_PUBLIC_BASE_URL
        if settings.VENUE_CLOUD_SYNC_ENABLED
        else settings.PUBLIC_WEB_BASE_URL
    )
    base_url = str(configured_url or "").strip().rstrip("/")
    if not base_url:
        target = "云端场所码公开地址" if settings.VENUE_CLOUD_SYNC_ENABLED else "平台公开访问地址"
        raise HTTPException(503, f"{target}尚未配置")

    parsed = urlsplit(base_url)
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    is_safe_scheme = parsed.scheme == "https" or (
        parsed.scheme == "http" and parsed.hostname in local_hosts
    )
    if (
        not is_safe_scheme
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise HTTPException(503, "场所码公开访问地址配置无效")

    return f"{base_url}/venue/{quote(token, safe='')}"


def _public_form_url(token: str) -> str:
    configured_url = settings.VENUE_PUBLIC_BASE_URL if settings.VENUE_CLOUD_SYNC_ENABLED else settings.PUBLIC_WEB_BASE_URL
    base_url = str(configured_url or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(503, "公开访问地址尚未配置")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise HTTPException(503, "公开访问地址配置无效")
    return f"{base_url}/drinking-report/{quote(token, safe='')}"


def _form_token(venue_id: int, issued: int) -> str:
    payload = f"{venue_id}:{issued}"
    sig = hmac.new(settings.registry_hmac_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{sig}".encode()).decode().rstrip("=")


def _check_form_token(value: str, venue_id: int) -> bool:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        raw_id, issued_text, sig = raw.split(":", 2)
        issued = int(issued_text)
        payload = f"{raw_id}:{issued}"
        expected = hmac.new(settings.registry_hmac_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        return int(raw_id) == venue_id and abs(int(time.time()) - issued) <= 900 and hmac.compare_digest(sig, expected)
    except (ValueError, TypeError, UnicodeError):
        return False


async def _issue_form_token(conn, venue_id: int) -> str:
    issued = int(time.time())
    for _ in range(3):
        token = _form_token(venue_id, issued) + secrets.token_urlsafe(16)
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO _venue_form_tokens (venue_id,token_hmac,issued_at) VALUES (%s,%s,UTC_TIMESTAMP())",
                    (venue_id, _token_digest(f"form:{token}")),
                )
            return token
        except Exception:
            issued += 1
    raise HTTPException(503, "登记页面令牌生成失败，请稍后重试")


async def _consume_form_token(conn, value: str, venue_id: int) -> bool:
    signed_token = value[:-_FORM_TOKEN_SUFFIX_LENGTH]
    if len(value) <= _FORM_TOKEN_SUFFIX_LENGTH or not _check_form_token(signed_token, venue_id):
        return False
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE _venue_form_tokens SET consumed_at=UTC_TIMESTAMP() WHERE venue_id=%s AND token_hmac=%s AND consumed_at IS NULL AND issued_at>=DATE_SUB(UTC_TIMESTAMP(), INTERVAL 15 MINUTE)",
            (venue_id, _token_digest(f"form:{value}")),
        )
        return cur.rowcount == 1


def _rate_limit(key: str, limit: int = 10, window: int = 60) -> None:
    now = time.monotonic()
    start, count = _rate_state.get(key, (now, 0))
    if now - start >= window:
        start, count = now, 0
    count += 1
    _rate_state[key] = (start, count)
    if count > limit:
        raise HTTPException(429, "提交过于频繁，请稍后再试")


def _validate_photo(filename: str, content_type: str, data: bytes) -> tuple[str, int, str]:
    if len(data) == 0 or len(data) > settings.VENUE_PHOTO_MAX_BYTES:
        raise HTTPException(422, "照片大小必须在 1 字节至 5 MB 之间")
    mime = (content_type or "").lower().split(";", 1)[0].strip()
    suffix = Path(filename or "").suffix.lower()
    allowed_suffix = {"image/jpeg": {".jpg", ".jpeg"}, "image/png": {".png"}, "image/webp": {".webp"}}
    if mime not in _PHOTO_MAGIC or suffix not in allowed_suffix[mime] or not any(data.startswith(m) for m in _PHOTO_MAGIC[mime]):
        raise HTTPException(422, "仅支持 JPEG、PNG 或 WebP 照片")
    if mime == "image/webp" and data[8:12] != b"WEBP":
        raise HTTPException(422, "照片文件头无效")
    return mime, len(data), hashlib.sha256(data).hexdigest()


def _venue_payload(row, include_token: bool = False) -> dict:
    payload = {
        "id": int(row[0]), "name": str(row[1]), "venue_type": str(row[2] or ""),
        "address": str(row[3] or ""), "community_id": int(row[4]) if row[4] is not None else None,
        "community_name": str(row[5] or ""), "status": str(row[6]),
        "created_at": row[10].isoformat() if len(row) > 10 and row[10] else None,
        "updated_at": row[11].isoformat() if len(row) > 11 and row[11] else None,
        "config_revision": int(row[12]) if len(row) > 12 and row[12] is not None else 1,
        "token_version": int(row[13]) if len(row) > 13 and row[13] is not None else 1,
        "cloud_sync_status": str(row[14]) if len(row) > 14 and row[14] else "local_only",
        "cloud_synced_revision": int(row[15]) if len(row) > 15 and row[15] is not None else None,
        "cloud_synced_at": row[16].isoformat() if len(row) > 16 and row[16] else None,
        "cloud_sync_error_code": str(row[17]) if len(row) > 17 and row[17] else None,
        "pending_token_version": int(row[18]) if len(row) > 18 and row[18] is not None else None,
    }
    if include_token and len(row) > 8:
        payload["token"] = decrypt_secret(row[8])
    return payload


@admin_router.get("/venue-codes")
async def list_venues(user: dict = Depends(require_permission(VENUE_VIEW)), conn=Depends(get_venue_db)):
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT {_VENUE_SELECT} FROM _venue_codes "
            "WHERE status<>'deleted' OR cloud_sync_status IN ('pending','error') ORDER BY updated_at DESC,id DESC"
        )
        return {"data": [_venue_payload(row) for row in await cur.fetchall()]}


@admin_router.post("/venue-codes")
async def create_venue(data: VenueCreate, request: Request, user: dict = Depends(require_permission(VENUE_MANAGE)), conn=Depends(get_venue_db)):
    token = secrets.token_urlsafe(32)
    public_url = _public_venue_url(token)
    cloud_status = "pending" if settings.VENUE_CLOUD_SYNC_ENABLED else "local_only"
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO _venue_codes (name,venue_type,address,community_id,community_name_snapshot,status,"
                "token_hmac,encrypted_token,cloud_sync_status,created_by,updated_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (data.name.strip(), data.venue_type.strip(), data.address.strip(), data.community_id, data.community_name.strip(), data.status, _token_digest(token), encrypt_secret(token), cloud_status, user["id"], user["id"]),
            )
            venue_id = int(cur.lastrowid)
            await enqueue_venue_cloud_outbox(cur, venue_id, 1, "create")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(user, "venue.create", target_type="venue", target_name=str(venue_id), detail={"status": data.status}, **request_audit_fields(request))
    result = {"id": venue_id, "cloud_sync_status": cloud_status}
    if not settings.VENUE_CLOUD_SYNC_ENABLED:
        result.update({"token": token, "url": public_url})
    return result


@admin_router.put("/venue-codes/{venue_id}")
async def update_venue(venue_id: int, data: VenueCreate, request: Request, user: dict = Depends(require_permission(VENUE_MANAGE)), conn=Depends(get_venue_db)):
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute("SELECT config_revision,pending_token_version FROM _venue_codes WHERE id=%s AND status<>'deleted' FOR UPDATE", (venue_id,))
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "场所不存在")
            if len(row) > 1 and row[1] is not None:
                raise HTTPException(409, "二维码轮换尚未完成云端确认，暂不能编辑场所")
            revision = int(row[0]) + 1
            cloud_status = "pending" if settings.VENUE_CLOUD_SYNC_ENABLED else "local_only"
            await cur.execute(
                "UPDATE _venue_codes SET name=%s,venue_type=%s,address=%s,community_id=%s,community_name_snapshot=%s,"
                "status=%s,config_revision=%s,cloud_sync_status=%s,cloud_sync_error_code=NULL,updated_by=%s WHERE id=%s",
                (data.name.strip(), data.venue_type.strip(), data.address.strip(), data.community_id, data.community_name.strip(), data.status, revision, cloud_status, user["id"], venue_id),
            )
            await enqueue_venue_cloud_outbox(cur, venue_id, revision, "disable" if data.status == "inactive" else "update")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(user, "venue.update", target_type="venue", target_name=str(venue_id), detail={"status": data.status}, **request_audit_fields(request))
    return {"message": "场所已更新"}


@admin_router.delete("/venue-codes/{venue_id}")
async def delete_venue(venue_id: int, request: Request, user: dict = Depends(require_permission(VENUE_MANAGE)), conn=Depends(get_venue_db)):
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute("SELECT config_revision,pending_token_version FROM _venue_codes WHERE id=%s AND status<>'deleted' FOR UPDATE", (venue_id,))
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "场所不存在")
            if len(row) > 1 and row[1] is not None:
                raise HTTPException(409, "二维码轮换尚未完成云端确认，暂不能移除场所")
            revision = int(row[0]) + 1
            cloud_status = "pending" if settings.VENUE_CLOUD_SYNC_ENABLED else "local_only"
            await cur.execute(
                "UPDATE _venue_codes SET status='deleted',config_revision=%s,cloud_sync_status=%s,"
                "cloud_sync_error_code=NULL,updated_by=%s WHERE id=%s",
                (revision, cloud_status, user["id"], venue_id),
            )
            await enqueue_venue_cloud_outbox(cur, venue_id, revision, "delete")
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "venue.delete",
        target_type="venue",
        target_name=str(venue_id),
        detail={"history_retained": True},
        **request_audit_fields(request),
    )
    return {
        "message": "云端删除待确认" if settings.VENUE_CLOUD_SYNC_ENABLED else "场所已移除",
        "cloud_sync_status": cloud_status,
    }


@admin_router.post("/venue-codes/{venue_id}/rotate-token")
async def rotate_token(venue_id: int, request: Request, user: dict = Depends(require_permission(VENUE_MANAGE)), conn=Depends(get_venue_db)):
    token = secrets.token_urlsafe(32)
    public_url = _public_venue_url(token)
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute("SELECT config_revision,token_version,pending_token_version FROM _venue_codes WHERE id=%s AND status<>'deleted' FOR UPDATE", (venue_id,))
            row = await cur.fetchone()
            if not row:
                raise HTTPException(404, "场所不存在")
            if len(row) > 2 and row[2] is not None:
                raise HTTPException(409, "二维码轮换尚未完成云端确认，不能重复轮换")
            revision = int(row[0]) + 1
            token_version = int(row[1]) + 1
            if settings.VENUE_CLOUD_SYNC_ENABLED:
                await cur.execute(
                    "UPDATE _venue_codes SET pending_token_hmac=%s,pending_encrypted_token=%s,pending_token_version=%s,"
                    "config_revision=%s,cloud_sync_status='pending',cloud_sync_error_code=NULL,updated_by=%s WHERE id=%s",
                    (_token_digest(token), encrypt_secret(token), token_version, revision, user["id"], venue_id),
                )
                await enqueue_venue_cloud_outbox(cur, venue_id, revision, "rotate")
            else:
                await cur.execute(
                    "UPDATE _venue_codes SET token_hmac=%s,encrypted_token=%s,token_version=%s,config_revision=%s,"
                    "cloud_sync_status='local_only',updated_by=%s WHERE id=%s",
                    (_token_digest(token), encrypt_secret(token), token_version, revision, user["id"], venue_id),
                )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(user, "venue.rotate_token", target_type="venue", target_name=str(venue_id), **request_audit_fields(request))
    if settings.VENUE_CLOUD_SYNC_ENABLED:
        return {"cloud_sync_status": "pending", "message": "新二维码正在同步，云端确认前旧二维码继续有效"}
    return {"token": token, "url": public_url, "cloud_sync_status": "local_only"}


@admin_router.get("/venue-codes/{venue_id}/qrcode")
async def venue_qrcode(venue_id: int, format: str = Query(default="json", pattern="^(json|png)$"), user: dict = Depends(require_permission(VENUE_VIEW)), conn=Depends(get_venue_db)):
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT {_VENUE_SELECT} FROM _venue_codes WHERE id=%s AND status<>'deleted'", (venue_id,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "场所不存在")
    if str(row[6]) != "active":
        raise HTTPException(409, "场所未启用，不能生成二维码")
    if settings.VENUE_CLOUD_SYNC_ENABLED:
        synced_revision = int(row[15]) if row[15] is not None else None
        current_revision = int(row[12])
        if str(row[14]) != "confirmed" or synced_revision != current_revision:
            raise HTTPException(409, "场所当前版本尚未完成云端同步，暂不能生成二维码")
    token = decrypt_secret(row[8])
    url = _public_venue_url(token)
    if format == "png":
        try:
            import qrcode
            image = qrcode.make(url)
            output = io.BytesIO(); image.save(output, format="PNG"); output.seek(0)
            return StreamingResponse(output, media_type="image/png")
        except ImportError as exc:
            raise HTTPException(503, "二维码图片生成依赖尚未安装") from exc
    return {
        "venue": _venue_payload(row), "token": token, "url": url,
        "image_url": f"/api/venue-codes/{venue_id}/qrcode?format=png",
        "rotation_pending": row[18] is not None,
    }


@admin_router.get("/venue-cloud/status")
async def venue_cloud_status(user: dict = Depends(require_permission(VENUE_VIEW))):
    return await get_venue_cloud_status()


@admin_router.get("/public-forms/drinking-report")
async def get_drinking_form(user: dict = Depends(require_permission(VENUE_VIEW)), conn=Depends(get_venue_db)):
    async with conn.cursor() as cur:
        await cur.execute("SELECT form_key,display_name,status,config_revision,token_version,cloud_sync_status,cloud_synced_revision,cloud_synced_at,cloud_sync_error_code,pending_token_version FROM _public_form_codes WHERE form_key='drinking_report'")
        row = await cur.fetchone()
    if not row:
        return {"exists": False, "form_key": "drinking_report", "display_name": "全所饮酒报备", "status": "inactive", "cloud_sync_status": "local_only"}
    return {"exists": True, "form_key": row[0], "display_name": row[1], "status": row[2], "config_revision": int(row[3]), "token_version": int(row[4]), "cloud_sync_status": row[5], "cloud_synced_revision": int(row[6]) if row[6] is not None else None, "cloud_synced_at": row[7].isoformat() if row[7] else None, "cloud_sync_error_code": row[8], "pending_token_version": int(row[9]) if row[9] is not None else None}


@admin_router.post("/public-forms/drinking-report")
async def create_drinking_form(request: Request, user: dict = Depends(require_permission(VENUE_MANAGE)), conn=Depends(get_venue_db)):
    token = secrets.token_urlsafe(32)
    status = "active"
    cloud_status = "pending" if settings.VENUE_CLOUD_SYNC_ENABLED else "local_only"
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM _public_form_codes WHERE form_key='drinking_report' FOR UPDATE")
            if await cur.fetchone():
                raise HTTPException(409, "饮酒报备二维码已存在，请使用轮换或启停操作")
            await cur.execute("INSERT INTO _public_form_codes (form_key,display_name,status,token_hmac,encrypted_token,cloud_sync_status,created_by,updated_by) VALUES ('drinking_report','全所饮酒报备',%s,%s,%s,%s,%s,%s)", (status, _token_digest(f"public-form:{token}"), encrypt_secret(token), cloud_status, user["id"], user["id"]))
            await enqueue_public_form_outbox(cur, "drinking_report", 1, "create")
        await conn.commit()
    except Exception:
        await conn.rollback(); raise
    await record_admin_audit(user, "public_form.drinking_report.create", target_type="public_form", target_name="drinking_report", **request_audit_fields(request))
    result = {"form_key": "drinking_report", "cloud_sync_status": cloud_status}
    if not settings.VENUE_CLOUD_SYNC_ENABLED:
        result.update({"token": token, "url": _public_form_url(token)})
    return result


@admin_router.patch("/public-forms/drinking-report/status")
async def update_drinking_form_status(data: PublicFormStatus, request: Request, user: dict = Depends(require_permission(VENUE_MANAGE)), conn=Depends(get_venue_db)):
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute("SELECT config_revision FROM _public_form_codes WHERE form_key='drinking_report' FOR UPDATE")
            row = await cur.fetchone()
            if not row: raise HTTPException(404, "饮酒报备二维码尚未创建")
            revision = int(row[0]) + 1; cloud_status = "pending" if settings.VENUE_CLOUD_SYNC_ENABLED else "local_only"
            await cur.execute("UPDATE _public_form_codes SET status=%s,config_revision=%s,cloud_sync_status=%s,cloud_sync_error_code=NULL,updated_by=%s WHERE form_key='drinking_report'", (data.status, revision, cloud_status, user["id"]))
            await enqueue_public_form_outbox(cur, "drinking_report", revision, "update")
        await conn.commit()
    except Exception:
        await conn.rollback(); raise
    await record_admin_audit(user, "public_form.drinking_report.status", target_type="public_form", target_name=data.status, **request_audit_fields(request))
    return {"status": data.status, "cloud_sync_status": cloud_status}


@admin_router.post("/public-forms/drinking-report/rotate")
async def rotate_drinking_form(request: Request, user: dict = Depends(require_permission(VENUE_MANAGE)), conn=Depends(get_venue_db)):
    token = secrets.token_urlsafe(32)
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute("SELECT config_revision,token_version,pending_token_version FROM _public_form_codes WHERE form_key='drinking_report' AND status<>'deleted' FOR UPDATE")
            row = await cur.fetchone()
            if not row: raise HTTPException(404, "饮酒报备二维码尚未创建")
            if row[2] is not None: raise HTTPException(409, "二维码轮换尚未完成云端确认")
            revision = int(row[0]) + 1; version = int(row[1]) + 1; cloud_status = "pending" if settings.VENUE_CLOUD_SYNC_ENABLED else "local_only"
            if settings.VENUE_CLOUD_SYNC_ENABLED:
                await cur.execute("UPDATE _public_form_codes SET pending_token_hmac=%s,pending_encrypted_token=%s,pending_token_version=%s,config_revision=%s,cloud_sync_status=%s,updated_by=%s WHERE form_key='drinking_report'", (_token_digest(f"public-form:{token}"), encrypt_secret(token), version, revision, cloud_status, user["id"]))
                await enqueue_public_form_outbox(cur, "drinking_report", revision, "rotate")
            else:
                await cur.execute("UPDATE _public_form_codes SET token_hmac=%s,encrypted_token=%s,token_version=%s,config_revision=%s,cloud_sync_status=%s,updated_by=%s WHERE form_key='drinking_report'", (_token_digest(f"public-form:{token}"), encrypt_secret(token), version, revision, cloud_status, user["id"]))
        await conn.commit()
    except Exception:
        await conn.rollback(); raise
    await record_admin_audit(user, "public_form.drinking_report.rotate", target_type="public_form", target_name="drinking_report", **request_audit_fields(request))
    return {"cloud_sync_status": cloud_status, "url": None if settings.VENUE_CLOUD_SYNC_ENABLED else _public_form_url(token)}


@admin_router.get("/public-forms/drinking-report/qrcode")
async def drinking_form_qrcode(format: str = Query(default="json", pattern="^(json|png)$"), user: dict = Depends(require_permission(VENUE_VIEW)), conn=Depends(get_venue_db)):
    async with conn.cursor() as cur:
        await cur.execute("SELECT form_key,display_name,status,encrypted_token,config_revision,cloud_sync_status,cloud_synced_revision,pending_token_version FROM _public_form_codes WHERE form_key='drinking_report'")
        row = await cur.fetchone()
    if not row: raise HTTPException(404, "饮酒报备二维码尚未创建")
    if row[2] != "active": raise HTTPException(409, "饮酒报备二维码未启用")
    if settings.VENUE_CLOUD_SYNC_ENABLED and (row[5] != "confirmed" or row[6] != row[4]): raise HTTPException(409, "二维码尚未完成云端同步")
    token = decrypt_secret(row[3]); url = _public_form_url(token)
    if format == "png":
        try:
            import qrcode
            output = io.BytesIO(); qrcode.make(url).save(output, format="PNG"); output.seek(0); return StreamingResponse(output, media_type="image/png")
        except ImportError as exc: raise HTTPException(503, "二维码图片生成依赖尚未安装") from exc
    return {"form_key": row[0], "display_name": row[1], "url": url, "image_url": "/api/public-forms/drinking-report/qrcode?format=png", "rotation_pending": row[7] is not None}


@admin_router.get("/venue-visits")
async def list_visits(user: dict = Depends(require_permission(VENUE_VIEW)), conn=Depends(get_venue_db), venue_id: int | None = Query(default=None), keyword: str = Query(default="", max_length=100), start: datetime | None = None, end: datetime | None = None, page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200)):
    where = ["visit.deleted_at IS NULL"]
    params: list[object] = []
    if venue_id is not None:
        where.append("visit.venue_id=%s"); params.append(venue_id)
    if start:
        where.append("visit.submitted_at >= %s"); params.append(start)
    if end:
        where.append("visit.submitted_at < %s"); params.append(end)
    where_sql = " AND ".join(where)
    offset = (page - 1) * page_size
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT COUNT(*) FROM _venue_visits visit JOIN _venue_codes venue ON venue.id=visit.venue_id WHERE {where_sql}", tuple(params))
        total = int((await cur.fetchone())[0])
        visit_sql = f"SELECT visit.id,visit.venue_id,visit.encrypted_name,visit.encrypted_identity,visit.encrypted_phone,visit.encrypted_address,visit.submitted_at,venue.name,photo.mime_type,photo.size_bytes FROM _venue_visits visit JOIN _venue_codes venue ON venue.id=visit.venue_id LEFT JOIN _venue_visit_photos photo ON photo.visit_id=visit.id AND photo.deleted_at IS NULL WHERE {where_sql} ORDER BY visit.submitted_at DESC"
        if keyword.strip():
            await cur.execute(visit_sql, tuple(params))
        else:
            await cur.execute(visit_sql + " LIMIT %s OFFSET %s", tuple(params + [page_size, offset]))
        rows = await cur.fetchall()
    data = [{"id": int(r[0]), "venue_id": int(r[1]), "venue_name": str(r[7]), "name": decrypt_secret(r[2]), "identity_number": decrypt_secret(r[3]), "phone": decrypt_secret(r[4]), "address": decrypt_secret(r[5]), "submitted_at": r[6].isoformat() if r[6] else None, "photo": {"mime_type": r[8], "size_bytes": int(r[9])} if r[8] else None} for r in rows]
    if keyword.strip():
        needle = keyword.strip().casefold()
        data = [item for item in data if any(needle in str(item[field]).casefold() for field in ("venue_name", "name", "identity_number", "phone", "address"))]
        total = len(data)
        data = data[(page - 1) * page_size: page * page_size]
    return {"data": data, "total": total, "page": page, "page_size": page_size}


def _safe_export_filename(name: str, identity: str) -> str:
    safe_name = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", name).strip(" ._") or "未知姓名"
    safe_identity = re.sub(r"[^0-9Xx]", "_", identity).strip(" ._") or "未知证件"
    return f"{safe_name}_{safe_identity}.jpg"


def _signature_svg(strokes_text: str) -> str:
    try:
        strokes = json.loads(decrypt_secret(strokes_text))
    except (TypeError, ValueError, json.JSONDecodeError):
        return ""
    paths = []
    for stroke in strokes if isinstance(strokes, list) else []:
        if not isinstance(stroke, list) or not stroke:
            continue
        points = [p for p in stroke if isinstance(p, dict) and isinstance(p.get("x"), (int, float)) and isinstance(p.get("y"), (int, float))]
        if not points:
            continue
        coordinates = [f"{max(0,min(1,float(p['x'])))*240:.2f},{max(0,min(1,float(p['y'])))*90:.2f}" for p in points]
        path = f"M {coordinates[0]}" + "".join(f" L {coordinate}" for coordinate in coordinates[1:])
        paths.append(f'<path d="{html.escape(path, quote=True)}" fill="none" stroke="#111827" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>')
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 90" width="240" height="90">{"".join(paths)}</svg>'


@admin_router.get("/drinking-reports")
async def list_drinking_reports(user: dict = Depends(require_permission(VENUE_VIEW)), conn=Depends(get_venue_db), keyword: str = Query(default="", max_length=100), start: datetime | None = None, end: datetime | None = None, page: int = Query(default=1, ge=1), page_size: int = Query(default=50, ge=1, le=200)):
    if start and end and (end - start).days > 366:
        raise HTTPException(422, "查询时间范围不能超过366天")
    if not start:
        start = _now() - timedelta(days=30)
    if not end:
        end = _now() + timedelta(seconds=1)
    where = ["drinking_at >= %s", "drinking_at < %s"]
    params: list[object] = [start, end]
    needle = keyword.strip().casefold()
    async with conn.cursor() as cur:
        await cur.execute("SELECT COUNT(*) FROM _drinking_reports WHERE " + " AND ".join(where), tuple(params))
        candidate_count = int((await cur.fetchone())[0])
        if needle and candidate_count > 5000:
            raise HTTPException(422, "当前时间范围内记录超过5000条，请缩小时间范围后再搜索")
        sql = "SELECT id,encrypted_name,encrypted_unit_position,drinking_at,encrypted_drinking_place,encrypted_reason,encrypted_inviter,encrypted_travel_method,encrypted_responsible_leader_name,submitted_at FROM _drinking_reports WHERE " + " AND ".join(where) + " ORDER BY drinking_at DESC,id DESC"
        if needle:
            await cur.execute(sql, tuple(params))
        else:
            await cur.execute(sql + " LIMIT %s OFFSET %s", tuple(params + [page_size, (page - 1) * page_size]))
        rows = await cur.fetchall()
    data = [{"id": int(r[0]), "name": decrypt_secret(r[1]), "unit_position": decrypt_secret(r[2]), "drinking_at": r[3].isoformat() if r[3] else None, "drinking_place": decrypt_secret(r[4]), "reason": decrypt_secret(r[5]), "inviter": decrypt_secret(r[6]), "travel_method": decrypt_secret(r[7]), "responsible_leader_name": decrypt_secret(r[8]), "submitted_at": r[9].isoformat() if r[9] else None} for r in rows]
    if needle:
        data = [item for item in data if any(needle in str(item[field]).casefold() for field in ("name", "unit_position", "drinking_place", "reason", "inviter", "travel_method", "responsible_leader_name"))]
        total = len(data)
        data = data[(page - 1) * page_size: page * page_size]
    else:
        total = candidate_count
    return {"data": data, "total": total, "page": page, "page_size": page_size}


@admin_router.get("/drinking-reports/{report_id}")
async def get_drinking_report(report_id: int, user: dict = Depends(require_permission(VENUE_VIEW)), conn=Depends(get_venue_db)):
    async with conn.cursor() as cur:
        await cur.execute("SELECT id,encrypted_name,encrypted_unit_position,drinking_at,encrypted_drinking_place,encrypted_reason,encrypted_inviter,encrypted_travel_method,encrypted_notes,encrypted_responsible_leader_name,encrypted_reporter_signature,encrypted_leader_signature,rules_version,rules_acknowledged_at,submitted_at FROM _drinking_reports WHERE id=%s", (report_id,))
        row = await cur.fetchone()
    if not row: raise HTTPException(404, "饮酒报备不存在")
    return {"id": int(row[0]), "name": decrypt_secret(row[1]), "unit_position": decrypt_secret(row[2]), "drinking_at": row[3].isoformat() if row[3] else None, "drinking_place": decrypt_secret(row[4]), "reason": decrypt_secret(row[5]), "inviter": decrypt_secret(row[6]), "travel_method": decrypt_secret(row[7]), "notes": decrypt_secret(row[8]), "responsible_leader_name": decrypt_secret(row[9]), "reporter_signature": json.loads(decrypt_secret(row[10])), "leader_signature": json.loads(decrypt_secret(row[11])), "rules_version": row[12], "rules_acknowledged_at": row[13].isoformat() if row[13] else None, "submitted_at": row[14].isoformat() if row[14] else None}


@admin_router.get("/drinking-reports/{report_id}/pdf")
async def export_drinking_report_pdf(report_id: int, request: Request, user: dict = Depends(require_permission(VENUE_EXPORT)), conn=Depends(get_venue_db)):
    async with conn.cursor() as cur:
        await cur.execute("SELECT id,encrypted_name,encrypted_unit_position,drinking_at,encrypted_drinking_place,encrypted_reason,encrypted_inviter,encrypted_travel_method,encrypted_notes,encrypted_responsible_leader_name,encrypted_reporter_signature,encrypted_leader_signature,rules_version,submitted_at FROM _drinking_reports WHERE id=%s", (report_id,))
        row = await cur.fetchone()
    if not row: raise HTTPException(404, "饮酒报备不存在")
    values = {"name": decrypt_secret(row[1]), "unit_position": decrypt_secret(row[2]), "drinking_at": row[3].strftime("%Y年%m月%d日 %H:%M") if row[3] else "", "drinking_place": decrypt_secret(row[4]), "reason": decrypt_secret(row[5]), "inviter": decrypt_secret(row[6]), "travel_method": decrypt_secret(row[7]), "notes": decrypt_secret(row[8]), "responsible_leader_name": decrypt_secret(row[9])}
    submitted_date = row[13].strftime("%Y年%m月%d日") if row[13] else ""
    try:
        from weasyprint import HTML
        pdf = HTML(string=f'''<html><head><meta charset="utf-8"><style>@page{{size:A4;margin:18mm}}body{{font-family:"Noto Sans CJK SC","Microsoft YaHei",sans-serif;color:#111}}h1{{text-align:center;font-size:20pt}}table{{width:100%;border-collapse:collapse}}td{{border:1px solid #222;padding:9px;vertical-align:top;min-height:28px}}.label{{width:18%;font-weight:bold;background:#f7f7f7}}.rules{{line-height:1.7}}.sig{{height:100px}}</style></head><body><div>附件</div><h1>苏州市公安局非工作日饮酒报备单</h1><table><tr><td class="label">姓名</td><td>{html.escape(values["name"])}</td><td class="label">单位职务</td><td>{html.escape(values["unit_position"])}</td></tr><tr><td class="label">饮酒时间</td><td>{html.escape(values["drinking_at"])}</td><td class="label">饮酒地点</td><td>{html.escape(values["drinking_place"])}</td></tr><tr><td class="label">饮酒事由</td><td colspan="3">{html.escape(values["reason"])}</td></tr><tr><td class="label">邀约人</td><td colspan="3">{html.escape(values["inviter"])}</td></tr><tr><td class="label">出行方式</td><td colspan="3">{html.escape(values["travel_method"])}</td></tr><tr><td class="label">备注说明</td><td colspan="3">{html.escape(values["notes"])}</td></tr></table><div class="rules"><p>公安部关于严禁违规宴请饮酒的规定</p><ol><li>严禁在工作日、值班备勤、安保任务、公务出差、学习培训期间和其他工作时间饮酒。</li><li>严禁领导干部之间、单位之间相互宴请。</li><li>严禁接受下属或地方公安机关宴请。</li><li>严禁接受管理和服务对象宴请。</li><li>严禁出入私人会所或参与“一桌餐”。</li><li>严禁在任何时间、任何场合酗酒。</li></ol></div><p>我承诺本次饮酒不违反中央八项规定及其实施细则精神和公安部“六项规定”。</p><table><tr><td class="sig">报备人（签名）：{_signature_svg(row[10])}<br>日期：{html.escape(submitted_date)}</td><td class="sig">责任领导：{html.escape(values["responsible_leader_name"])}<br>责任领导（签名）：{_signature_svg(row[11])}<br>日期：{html.escape(submitted_date)}</td></tr></table></body></html>''').write_pdf()
    except ImportError as exc:
        raise HTTPException(503, "PDF 生成依赖尚未安装") from exc
    await record_admin_audit(user, "drinking_report.export_pdf", target_type="drinking_report", target_name=str(report_id), **request_audit_fields(request))
    safe_name = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", values["name"]).strip("._") or "饮酒报备"
    return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(f'饮酒报备单-{safe_name}.pdf')}"})


@admin_router.get("/venue-visits/export-zip")
async def export_visits_zip(request: Request, user: dict = Depends(require_permission(VENUE_EXPORT)), conn=Depends(get_venue_db), venue_id: int | None = None, keyword: str = Query(default="", max_length=100), start: datetime | None = None, end: datetime | None = None):
    where = ["visit.deleted_at IS NULL"]; params: list[object] = []
    if venue_id is not None: where.append("visit.venue_id=%s"); params.append(venue_id)
    if start: where.append("visit.submitted_at >= %s"); params.append(start)
    if end: where.append("visit.submitted_at < %s"); params.append(end)
    async with conn.cursor() as cur:
        await cur.execute("SELECT visit.id,venue.name,visit.encrypted_name,visit.encrypted_identity,visit.encrypted_phone,visit.encrypted_address,visit.submitted_at,photo.storage_key,photo.mime_type FROM _venue_visits visit JOIN _venue_codes venue ON venue.id=visit.venue_id LEFT JOIN _venue_visit_photos photo ON photo.visit_id=visit.id AND photo.deleted_at IS NULL AND photo.retention_until>UTC_TIMESTAMP() WHERE " + " AND ".join(where) + " ORDER BY visit.submitted_at DESC", tuple(params))
        rows = await cur.fetchall()
    records = []
    needle = keyword.strip().casefold()
    for row in rows:
        record = {"id": int(row[0]), "venue_name": str(row[1]), "name": decrypt_secret(row[2]), "identity_number": decrypt_secret(row[3]), "phone": decrypt_secret(row[4]), "address": decrypt_secret(row[5]), "submitted_at": row[6].isoformat() if row[6] else "", "storage_key": row[7], "mime_type": row[8]}
        if not needle or any(needle in str(record[field]).casefold() for field in ("venue_name", "name", "identity_number", "phone", "address")):
            records.append(record)
    wb = Workbook(); ws = wb.active; ws.title = "场所登记"; ws.append(["编号", "场所", "姓名", "公民身份号码", "手机号", "地址", "登记时间", "照片文件"])
    archive = io.BytesIO(); photo_dir = Path(settings.VENUE_PHOTO_DIR).resolve()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        used: set[str] = set()
        for record in records:
            filename = None
            if record["storage_key"]:
                candidate = _safe_export_filename(record["name"], record["identity_number"])
                if candidate in used:
                    candidate = candidate[:-4] + f"_{record['id']}.jpg"
                used.add(candidate)
                photo_path = (photo_dir / str(record["storage_key"])).resolve()
                if photo_dir in photo_path.parents and photo_path.is_file():
                    zf.write(photo_path, arcname=f"photos/{candidate}"); filename = f"photos/{candidate}"
            ws.append([record["id"], record["venue_name"], record["name"], record["identity_number"], record["phone"], record["address"], record["submitted_at"], filename or ""])
        xlsx = io.BytesIO(); wb.save(xlsx); zf.writestr("登记信息.xlsx", xlsx.getvalue())
    archive.seek(0)
    await record_admin_audit(user, "venue.export_zip", target_type="venue_visits", target_name="filtered", detail={"rows": len(records)}, **request_audit_fields(request))
    return StreamingResponse(archive, media_type="application/zip", headers={"Content-Disposition": "attachment; filename=venue-visits.zip"})


@admin_router.get("/venue-visits/export")
async def export_visits(request: Request, user: dict = Depends(require_permission(VENUE_EXPORT)), conn=Depends(get_venue_db), venue_id: int | None = None, start: datetime | None = None, end: datetime | None = None):
    where = ["visit.deleted_at IS NULL"]; params: list[object] = []
    if venue_id is not None: where.append("visit.venue_id=%s"); params.append(venue_id)
    if start: where.append("visit.submitted_at >= %s"); params.append(start)
    if end: where.append("visit.submitted_at < %s"); params.append(end)
    async with conn.cursor() as cur:
        await cur.execute("SELECT visit.id,venue.name,visit.encrypted_name,visit.encrypted_identity,visit.encrypted_phone,visit.encrypted_address,visit.submitted_at FROM _venue_visits visit JOIN _venue_codes venue ON venue.id=visit.venue_id WHERE " + " AND ".join(where) + " ORDER BY visit.submitted_at DESC", tuple(params))
        rows = await cur.fetchall()
    wb = Workbook(); ws = wb.active; ws.title = "场所登记"; ws.append(["编号", "场所", "姓名", "公民身份号码", "手机号", "地址", "登记时间"])
    for r in rows: ws.append([int(r[0]), str(r[1]), decrypt_secret(r[2]), decrypt_secret(r[3]), decrypt_secret(r[4]), decrypt_secret(r[5]), r[6].isoformat() if r[6] else ""])
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    await record_admin_audit(user, "venue.export", target_type="venue_visits", target_name="filtered", detail={"rows": len(rows)}, **request_audit_fields(request))
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=venue-visits.xlsx"})


@router.get("/venue/{token}", response_class=HTMLResponse)
async def public_venue_form(token: str, conn=Depends(get_venue_db)):
    if not settings.VENUE_LOCAL_PUBLIC_ENTRY_ENABLED:
        return HTMLResponse(
            "<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>二维码已更换</title><h2>二维码已更换</h2><p>请联系工作人员获取新的场所码。</p>",
            status_code=410,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    digest = _token_digest(token)
    async with conn.cursor() as cur:
        await cur.execute("SELECT id,name,status FROM _venue_codes WHERE token_hmac=%s AND status='active'", (digest,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "场所不存在或二维码已停用")
    form_token = await _issue_form_token(conn, int(row[0]))
    venue_name = html.escape(str(row[1]), quote=True)
    return HTMLResponse(f"""<!doctype html><meta name='viewport' content='width=device-width,initial-scale=1'><title>场所登记</title><h2>{venue_name}</h2><form method='post' action='/api/public/venue-visits' enctype='multipart/form-data'><input type='hidden' name='venue_id' value='{int(row[0])}'><input type='hidden' name='form_token' value='{html.escape(form_token, quote=True)}'><label>姓名<input name='name' required maxlength='100'></label><br><label>公民身份号码<input name='identity_number' required maxlength='18'></label><br><label>手机号<input name='phone' required maxlength='20'></label><br><label>地址<input name='address' required maxlength='500'></label><br><label>照片<input type='file' name='photo' accept='image/jpeg,image/png,image/webp' required></label><br><button type='submit'>提交登记</button></form>""")


@router.get("/api/public/venue-codes/{token}")
async def public_venue_info(token: str, conn=Depends(get_venue_db)):
    if not settings.VENUE_LOCAL_PUBLIC_ENTRY_ENABLED:
        raise HTTPException(410, "二维码已更换，请联系工作人员获取新场所码")
    _rate_limit(f"venue-token:{_token_digest(token)}", limit=60)
    digest = _token_digest(token)
    async with conn.cursor() as cur:
        await cur.execute("SELECT id,name FROM _venue_codes WHERE token_hmac=%s AND status='active'", (digest,))
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "场所不存在或二维码已停用")
    return {"venue_id": int(row[0]), "name": str(row[1]), "form_token": await _issue_form_token(conn, int(row[0]))}


@router.post("/api/public/venue-visits")
async def public_submit_venue_visit(request: Request, venue_id: int = Form(...), form_token: str = Form(...), name: str = Form(...), identity_number: str = Form(...), phone: str = Form(...), address: str = Form(...), photo: UploadFile = File(...), conn=Depends(get_venue_db)):
    if not settings.VENUE_LOCAL_PUBLIC_ENTRY_ENABLED:
        raise HTTPException(410, "本地匿名登记入口已停止使用")
    client_ip = request.client.host if request.client else "unknown"
    _rate_limit(f"venue:{venue_id}:ip:{client_ip}")
    _rate_limit(f"venue:{venue_id}:device:{hashlib.sha256((request.headers.get('user-agent', '') + '|' + client_ip).encode()).hexdigest()}")
    if not await _consume_form_token(conn, form_token, venue_id):
        raise HTTPException(400, "登记页面已过期，请重新扫码")
    identity = normalize_identity(identity_number); phone_norm = normalize_phone(phone)
    if not (len(identity) == 18 and identity[:17].isdigit() and (identity[17].isdigit() or identity[17] == "X")):
        raise HTTPException(422, "公民身份号码格式无效")
    if not (len(phone_norm) == 11 and phone_norm.isdigit() and phone_norm[0] == "1"):
        raise HTTPException(422, "手机号格式无效")
    name = name.strip(); address = address.strip()
    if not name:
        raise HTTPException(422, "姓名不能为空")
    if not address:
        raise HTTPException(422, "地址不能为空")
    photo_data = await photo.read(settings.VENUE_PHOTO_MAX_BYTES + 1)
    mime, size, digest = _validate_photo(photo.filename or "photo", photo.content_type or "", photo_data)
    identity_hmac, _ = hmac_digest(identity, kind="identity"); phone_hmac, _ = hmac_digest(phone_norm, kind="phone")
    storage_dir = Path(settings.VENUE_PHOTO_DIR).resolve(); storage_dir.mkdir(parents=True, exist_ok=True)
    storage_key = f"{secrets.token_hex(16)}{Path(photo.filename or '.jpg').suffix.lower()}"; target = (storage_dir / storage_key).resolve()
    if storage_dir not in target.parents: raise HTTPException(500, "照片存储路径无效")
    target.write_bytes(photo_data)
    retention = _now() + timedelta(days=90)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM _venue_codes WHERE id=%s AND status='active'", (venue_id,))
            venue = await cur.fetchone()
            if not venue: raise HTTPException(404, "场所不存在或二维码已停用")
            await cur.execute("INSERT INTO _venue_visits (venue_id,encrypted_name,encrypted_identity,identity_hmac,encrypted_phone,phone_hmac,encrypted_address,source_ip_hash,retention_until) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (venue_id, encrypt_secret(name), encrypt_secret(identity), identity_hmac, encrypt_secret(phone_norm), phone_hmac, encrypt_secret(address), hashlib.sha256(f"{settings.registry_hmac_key}:{client_ip}".encode()).hexdigest(), retention))
            visit_id = int(cur.lastrowid)
            await cur.execute("INSERT INTO _venue_visit_photos (visit_id,storage_key,mime_type,size_bytes,sha256,retention_until) VALUES (%s,%s,%s,%s,%s,%s)", (visit_id, storage_key, mime, size, digest, retention))
    except Exception:
        target.unlink(missing_ok=True); raise
    return {"message": "登记成功", "submitted_at": _now().isoformat()}
@admin_router.get("/venue-visits/{visit_id}/photo")
async def venue_visit_photo(visit_id: int, user: dict = Depends(require_permission(VENUE_VIEW)), conn=Depends(get_venue_db)):
    async with conn.cursor() as cur:
        await cur.execute("SELECT storage_key,mime_type FROM _venue_visit_photos WHERE visit_id=%s AND deleted_at IS NULL AND retention_until>UTC_TIMESTAMP()", (visit_id,))
        row = await cur.fetchone()
    if not row: raise HTTPException(404, "照片不存在或已过期")
    path = (Path(settings.VENUE_PHOTO_DIR).resolve() / str(row[0])).resolve()
    if Path(settings.VENUE_PHOTO_DIR).resolve() not in path.parents or not path.is_file(): raise HTTPException(404, "照片文件不存在")
    return FileResponse(
        path,
        media_type=str(row[1]),
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@admin_router.delete("/venue-visits/{visit_id}")
async def delete_venue_visit(visit_id: int, request: Request, user: dict = Depends(require_permission(VENUE_MANAGE)), conn=Depends(get_venue_db)):
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM _venue_visits WHERE id=%s AND deleted_at IS NULL", (visit_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "登记记录不存在")
        await cur.execute("UPDATE _venue_visits SET deleted_at=UTC_TIMESTAMP() WHERE id=%s AND deleted_at IS NULL", (visit_id,))
        await cur.execute("UPDATE _venue_visit_photos SET deleted_at=UTC_TIMESTAMP() WHERE visit_id=%s AND deleted_at IS NULL", (visit_id,))
    await conn.commit()
    await record_admin_audit(user, "venue.visit.delete", target_type="venue_visit", target_name=str(visit_id), detail={"history_retained": True}, **request_audit_fields(request))
    return {"message": "登记记录已删除"}
