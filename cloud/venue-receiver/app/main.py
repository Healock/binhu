from __future__ import annotations

import asyncio
import hashlib
import html
import io
import json
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field

from .config import Settings, settings as default_settings
from .repository import MySQLRepository
from .security import (
    EnvelopeEncryptor,
    canonical_json,
    canonical_request,
    keyed_digest,
    load_ed25519_private_key,
    load_ed25519_public_key,
    request_fingerprint,
    response_signature_headers,
    verify_request_signature,
)


class VenueUpdate(BaseModel):
    request_id: uuid.UUID
    display_name: str = Field(min_length=1, max_length=200)
    status: Literal["active", "inactive", "deleted"]
    token: str = Field(min_length=32, max_length=200)
    token_version: int = Field(ge=1)
    config_revision: int = Field(ge=1)


class VenueSummary(BaseModel):
    local_venue_id: int = Field(gt=0)
    status: Literal["active", "inactive", "deleted"]
    token: str = Field(min_length=32, max_length=200)
    token_version: int = Field(ge=1)
    config_revision: int = Field(ge=1)


class ReconcileRequest(BaseModel):
    request_id: uuid.UUID
    venues: list[VenueSummary] = Field(max_length=10000)


class PullRequest(BaseModel):
    request_id: uuid.UUID
    worker_id: str = Field(min_length=8, max_length=100)
    limit: int = Field(default=50, ge=1, le=50)
    supported_encryption_versions: list[str] = Field(min_length=1, max_length=10)


class AckItem(BaseModel):
    submission_id: uuid.UUID
    status: Literal["accepted", "rejected", "retry_later", "uncertain"]
    reason_code: str | None = Field(default=None, max_length=100, pattern=r"^[a-z0-9_]*$")


class AckRequest(BaseModel):
    request_id: uuid.UUID
    lease_id: uuid.UUID
    results: list[AckItem] = Field(min_length=1, max_length=50)


class RenewLeaseRequest(BaseModel):
    request_id: uuid.UUID
    lease_id: uuid.UUID
    worker_id: str = Field(min_length=8, max_length=100)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def _normalize_photo(filename: str, content_type: str, data: bytes, config: Settings) -> tuple[str, bytes]:
    if not data or len(data) > config.PHOTO_MAX_BYTES:
        raise HTTPException(422, "照片大小必须在 1 字节至 5 MB 之间")
    declared = content_type.lower().split(";", 1)[0].strip()
    suffix = Path(filename or "").suffix.lower()
    allowed = {
        "JPEG": ("image/jpeg", {".jpg", ".jpeg"}),
        "PNG": ("image/png", {".png"}),
        "WEBP": ("image/webp", {".webp"}),
    }
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            image_format = str(image.format or "").upper()
            if image_format not in allowed:
                raise HTTPException(422, "仅支持 JPEG、PNG 或 WebP 照片")
            expected_mime, expected_suffixes = allowed[image_format]
            if declared != expected_mime or suffix not in expected_suffixes:
                raise HTTPException(422, "照片扩展名、类型和内容不一致")
            if image.width * image.height > config.PHOTO_MAX_PIXELS:
                raise HTTPException(422, "照片像素尺寸过大")
            normalized = ImageOps.exif_transpose(image)
            if image_format == "JPEG" and normalized.mode not in {"RGB", "L"}:
                normalized = normalized.convert("RGB")
            output = io.BytesIO()
            save_options: dict[str, Any] = {"format": image_format}
            if image_format == "JPEG":
                save_options.update(quality=92, optimize=True)
            elif image_format == "PNG":
                save_options.update(optimize=True)
            normalized.save(output, **save_options)
            result = output.getvalue()
    except HTTPException:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise HTTPException(422, "照片内容无效") from exc
    if len(result) > config.PHOTO_MAX_BYTES:
        raise HTTPException(422, "规范化后的照片超过 5 MB")
    return expected_mime, result


def _registration_page() -> str:
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer"><title>场所登记</title><style>
:root{font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif;color:#17212b;background:#f3f6f8}*{box-sizing:border-box}
body{margin:0;padding:max(24px,env(safe-area-inset-top)) 16px max(24px,env(safe-area-inset-bottom));min-height:100vh}
main{max-width:560px;margin:auto;background:#fff;border:1px solid #dce4e8;border-radius:8px;padding:24px}h1{font-size:22px;margin:0 0 6px}
p{color:#60717d;margin:0 0 20px}form{display:grid;gap:14px}label{display:grid;gap:6px;font-size:14px;font-weight:600}
input{width:100%;min-height:44px;border:1px solid #b8c5cc;border-radius:6px;padding:10px 12px;font:inherit}button{min-height:46px;border:0;border-radius:6px;background:#176b5b;color:#fff;font:inherit;font-weight:700}
#message{min-height:22px;color:#b42318}button:disabled{opacity:.6}</style></head><body><main><h1 id="venue">场所登记</h1>
<p>请如实填写登记信息。</p><form id="form"><label>姓名<input name="name" maxlength="100" required autocomplete="name"></label>
<label>公民身份号码<input name="identity_number" maxlength="18" required inputmode="text"></label>
<label>手机号<input name="phone" maxlength="20" required inputmode="tel" autocomplete="tel"></label>
<label>地址<input name="address" maxlength="500" required autocomplete="street-address"></label>
<label>照片<input name="photo" type="file" accept="image/jpeg,image/png,image/webp" required></label>
<button type="submit">提交登记</button><div id="message" role="status"></div></form></main><script>
const token=decodeURIComponent(location.pathname.split('/').pop()||'');const form=document.querySelector('#form');const message=document.querySelector('#message');let formToken='';
const makeUuid=()=>{if(globalThis.crypto&&typeof globalThis.crypto.randomUUID==='function')return globalThis.crypto.randomUUID();const bytes=new Uint8Array(16);if(globalThis.crypto&&typeof globalThis.crypto.getRandomValues==='function')globalThis.crypto.getRandomValues(bytes);else for(let i=0;i<bytes.length;i++)bytes[i]=Math.floor(Math.random()*256);bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;const hex=[...bytes].map(value=>value.toString(16).padStart(2,'0')).join('');return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`};
const submissionId=makeUuid();
fetch(`/api/public/venues/${encodeURIComponent(token)}`,{credentials:'omit'}).then(async r=>{const d=await r.json();if(!r.ok)throw new Error(d.detail||'二维码不可用');document.querySelector('#venue').textContent=d.name;formToken=d.form_token}).catch(e=>{message.textContent=e.message;form.hidden=true});
let deviceId=localStorage.getItem('binhuVenueDeviceId');if(!deviceId){deviceId=makeUuid();localStorage.setItem('binhuVenueDeviceId',deviceId)}
form.addEventListener('submit',async e=>{e.preventDefault();const button=form.querySelector('button');button.disabled=true;message.textContent='正在提交…';const body=new FormData(form);body.set('submission_id',submissionId);body.set('form_token',formToken);body.set('venue_token',token);body.set('device_id',deviceId);try{const r=await fetch('/api/public/submissions',{method:'POST',body,credentials:'omit'});const d=await r.json();if(!r.ok)throw new Error(d.detail||'提交失败');message.textContent='提交成功';form.querySelectorAll('input,button').forEach(x=>x.disabled=true)}catch(err){message.textContent=err.message||'提交失败，请稍后重试';button.disabled=false}});
</script></body></html>"""


def _retired_registration_page() -> str:
    return """<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'><meta name='referrer' content='no-referrer'><title>二维码已更换</title><style>body{font-family:system-ui,-apple-system,'Microsoft YaHei',sans-serif;background:#f3f6f8;color:#17212b;margin:0;padding:24px}main{max-width:560px;margin:10vh auto;background:#fff;border:1px solid #dce4e8;border-radius:8px;padding:24px}h1{font-size:22px;margin:0 0 12px}p{color:#60717d;margin:0}</style></head><body><main><h1>二维码已更换</h1><p>请联系工作人员获取新的场所码。</p></main></body></html>"""


def create_app(*, repo=None, config: Settings | None = None) -> FastAPI:
    app_config = config or default_settings

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        owned_repo = None
        if repo is None:
            app_config.validate_runtime()
            owned_repo = await MySQLRepository.connect(app_config)
            application.state.repo = owned_repo
        else:
            application.state.repo = repo
        app_config.PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        application.state.encryptor = EnvelopeEncryptor(
            app_config.ENCRYPTION_PUBLIC_KEY_DIR,
            app_config.ACTIVE_ENCRYPTION_KEY_ID,
        )
        if app_config.INTERNAL_REQUEST_PUBLIC_KEY_PATH.is_file():
            application.state.request_public_key = load_ed25519_public_key(app_config.INTERNAL_REQUEST_PUBLIC_KEY_PATH)
        if app_config.INTERNAL_RESPONSE_PRIVATE_KEY_PATH.is_file():
            application.state.response_private_key = load_ed25519_private_key(app_config.INTERNAL_RESPONSE_PRIVATE_KEY_PATH)

        async def cleanup_loop() -> None:
            while True:
                await asyncio.sleep(3600)
                keys = await application.state.repo.expire_records(
                    app_config.ACCEPTED_RETENTION_HOURS,
                    app_config.AUDIT_RETENTION_DAYS,
                )
                for key in keys:
                    path = (app_config.PHOTO_DIR / key).resolve()
                    if app_config.PHOTO_DIR.resolve() in path.parents:
                        path.unlink(missing_ok=True)

        cleanup_task = asyncio.create_task(cleanup_loop())
        try:
            yield
        finally:
            cleanup_task.cancel()
            with suppress(asyncio.CancelledError):
                await cleanup_task
            if owned_repo is not None:
                await owned_repo.close()

    application = FastAPI(title="滨湖场所码云端接收服务", version="1.0", lifespan=lifespan)
    application.state.config = app_config

    async def internal_request(request: Request) -> str:
        if app_config.ALLOW_INSECURE_INTERNAL_TESTS:
            return request.headers.get("X-Binhu-Request-Id", str(uuid.uuid4()))
        if app_config.REQUIRE_MTLS_HEADER and request.headers.get("X-Binhu-Client-Verify") != "SUCCESS":
            raise HTTPException(401, "client_certificate_required")
        timestamp = request.headers.get("X-Binhu-Timestamp", "")
        nonce = request.headers.get("X-Binhu-Nonce", "")
        request_id = request.headers.get("X-Binhu-Request-Id", "")
        signature = request.headers.get("X-Binhu-Signature", "")
        try:
            uuid.UUID(request_id)
            timestamp_value = int(timestamp)
        except (ValueError, TypeError) as exc:
            raise HTTPException(401, "invalid_request_identity") from exc
        if abs(int(time.time()) - timestamp_value) > 300 or not 16 <= len(nonce) <= 200:
            raise HTTPException(401, "expired_or_invalid_signature")
        body = await request.body()
        try:
            verify_request_signature(
                request.app.state.request_public_key,
                signature,
                canonical_request(request.method, request.url.path, timestamp, nonce, request_id, body),
            )
        except (InvalidSignature, ValueError, AttributeError) as exc:
            raise HTTPException(401, "invalid_signature") from exc
        if not await request.app.state.repo.claim_nonce(nonce, request_id):
            raise HTTPException(409, "replayed_request")
        return request_id

    def signed_json(request: Request, payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
        body = canonical_json(payload)
        headers: dict[str, str] = {}
        private_key = getattr(request.app.state, "response_private_key", None)
        request_id = request.headers.get("X-Binhu-Request-Id", "")
        if private_key and request_id:
            headers.update(response_signature_headers(private_key, request_id=request_id, timestamp=str(int(time.time())), body=body))
        return JSONResponse(content=json.loads(body), status_code=status_code, headers=headers)

    @application.get("/health")
    async def health():
        return {"status": "ok", "service": "binhu-venue-cloud"}

    @application.get("/health/ready")
    async def readiness(request: Request):
        """Return ready only when the receiver can reach its database."""
        try:
            await request.app.state.repo.ping()
        except Exception as exc:
            raise HTTPException(503, "receiver_not_ready") from exc
        return {"status": "ready", "service": "binhu-venue-cloud"}

    @application.get("/venue/{token}", response_class=HTMLResponse)
    async def venue_page(token: str, request: Request):
        if not 32 <= len(token) <= 200:
            raise HTTPException(404, "二维码不可用")
        digest = keyed_digest(app_config.PUBLIC_TOKEN_HMAC_KEY, "venue-token", token)
        venue = await request.app.state.repo.get_venue_by_token(digest)
        if not venue:
            raise HTTPException(404, "二维码不可用")
        if venue["status"] != "active":
            return HTMLResponse(
                _retired_registration_page(),
                status_code=410,
                headers={
                    "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; img-src 'self'; connect-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                    "Cache-Control": "no-store",
                },
            )
        return HTMLResponse(
            _registration_page(),
            headers={
                "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    @application.get("/api/public/venues/{token}")
    async def public_venue(token: str, request: Request):
        digest = keyed_digest(app_config.PUBLIC_TOKEN_HMAC_KEY, "venue-token", token)
        venue = await request.app.state.repo.get_venue_by_token(digest)
        if not venue:
            raise HTTPException(404, "场所不存在或二维码已停用")
        if venue["status"] != "active":
            raise HTTPException(410, "二维码已更换，请联系工作人员获取新场所码")
        form_token = secrets.token_urlsafe(32)
        form_digest = keyed_digest(app_config.FORM_TOKEN_HMAC_KEY, "form-token", form_token)
        await request.app.state.repo.issue_form_token(
            form_digest,
            int(venue["local_venue_id"]),
            _utcnow() + timedelta(seconds=app_config.FORM_TOKEN_TTL_SECONDS),
        )
        return JSONResponse(
            {"name": str(venue["display_name"]), "form_token": form_token, "photo_max_bytes": app_config.PHOTO_MAX_BYTES},
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @application.post("/api/public/submissions", status_code=202)
    async def public_submission(
        request: Request,
        submission_id: str = Form(...),
        venue_token: str = Form(...),
        form_token: str = Form(...),
        device_id: str = Form(..., min_length=16, max_length=200),
        name: str = Form(...),
        identity_number: str = Form(...),
        phone: str = Form(...),
        address: str = Form(...),
        photo: UploadFile = File(...),
    ):
        try:
            submission_uuid = str(uuid.UUID(submission_id))
        except ValueError as exc:
            raise HTTPException(422, "submission_id 无效") from exc
        venue_digest = keyed_digest(app_config.PUBLIC_TOKEN_HMAC_KEY, "venue-token", venue_token)
        venue = await request.app.state.repo.get_venue_by_token(venue_digest)
        if not venue:
            raise HTTPException(404, "场所不存在或二维码已停用")
        if venue["status"] != "active":
            raise HTTPException(410, "二维码已更换，请联系工作人员获取新场所码")
        venue_id = int(venue["local_venue_id"])
        name_value = name.strip()
        identity = identity_number.strip().upper().replace(" ", "")
        phone_value = phone.strip().replace(" ", "").replace("-", "")
        address_value = address.strip()
        if not name_value or len(name_value) > 100 or not address_value or len(address_value) > 500:
            raise HTTPException(422, "姓名或地址格式无效")
        if not (len(identity) == 18 and identity[:17].isdigit() and (identity[-1].isdigit() or identity[-1] == "X")):
            raise HTTPException(422, "公民身份号码格式无效")
        if not (len(phone_value) == 11 and phone_value.isdigit() and phone_value.startswith("1")):
            raise HTTPException(422, "手机号格式无效")
        photo_data = await photo.read(app_config.PHOTO_MAX_BYTES + 1)
        mime, normalized_photo = _normalize_photo(photo.filename or "photo", photo.content_type or "", photo_data, app_config)
        payload = {"name": name_value, "identity_number": identity, "phone": phone_value, "address": address_value}
        fingerprint = request_fingerprint(app_config.REQUEST_FINGERPRINT_KEY, payload, normalized_photo)
        existing = await request.app.state.repo.get_submission(submission_uuid)
        if existing:
            if int(existing["local_venue_id"]) == venue_id and existing["request_fingerprint"] == fingerprint:
                return {"submission_id": submission_uuid, "status": existing["state"]}
            raise HTTPException(409, "submission_id 已被其他内容使用")
        rate_keys = [
            (keyed_digest(app_config.REQUEST_FINGERPRINT_KEY, "rate-global", "all"), 300),
            (keyed_digest(app_config.REQUEST_FINGERPRINT_KEY, "rate-venue", str(venue_id)), 60),
            (keyed_digest(app_config.REQUEST_FINGERPRINT_KEY, "rate-device", device_id), 10),
        ]
        if not await request.app.state.repo.check_rate_limits(rate_keys):
            raise HTTPException(429, "提交过于频繁，请稍后再试")
        form_digest = keyed_digest(app_config.FORM_TOKEN_HMAC_KEY, "form-token", form_token)
        if not await request.app.state.repo.consume_form_token(form_digest, venue_id):
            raise HTTPException(400, "登记页面已过期，请重新扫码")
        encrypted = request.app.state.encryptor.encrypt(canonical_json(payload), normalized_photo)
        object_key = f"{secrets.token_hex(24)}.bin"
        target = (app_config.PHOTO_DIR / object_key).resolve()
        if app_config.PHOTO_DIR.resolve() not in target.parents:
            raise HTTPException(500, "照片存储路径无效")
        temp = target.with_suffix(".tmp")
        try:
            with temp.open("xb") as output:
                output.write(encrypted.encrypted_photo)
                output.flush()
                os.fsync(output.fileno())
            temp.replace(target)
            await request.app.state.repo.create_submission({
                "submission_id": submission_uuid,
                "local_venue_id": venue_id,
                "request_fingerprint": fingerprint,
                "encrypted_payload": encrypted.encrypted_payload,
                "wrapped_data_key": encrypted.wrapped_data_key,
                "key_id": encrypted.key_id,
                "algorithm_version": encrypted.algorithm_version,
                "payload_nonce": encrypted.payload_nonce,
                "ciphertext_sha256": encrypted.ciphertext_sha256,
                "photo_object_key": object_key,
                "photo_nonce": encrypted.photo_nonce,
                "photo_ciphertext_sha256": encrypted.photo_ciphertext_sha256,
                "photo_size": len(encrypted.encrypted_photo),
                "photo_mime_type": mime,
            })
        except Exception:
            temp.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        return {"submission_id": submission_uuid, "status": "queued"}

    @application.put("/api/internal/venues/{local_venue_id}")
    async def put_venue(local_venue_id: int, data: VenueUpdate, request: Request, _: str = Depends(internal_request)):
        operation = f"venue:{local_venue_id}"
        cached = await request.app.state.repo.get_request_result(str(data.request_id), operation)
        if cached is not None:
            return signed_json(request, cached)
        result = await request.app.state.repo.upsert_venue({
            "request_id": str(data.request_id),
            "local_venue_id": local_venue_id,
            "display_name": data.display_name.strip(),
            "status": data.status,
            "token_hmac": keyed_digest(app_config.PUBLIC_TOKEN_HMAC_KEY, "venue-token", data.token),
            "token_version": data.token_version,
            "config_revision": data.config_revision,
        })
        await request.app.state.repo.save_request_result(str(data.request_id), operation, result)
        return signed_json(request, result)

    @application.post("/api/internal/venues/reconcile")
    async def reconcile(data: ReconcileRequest, request: Request, _: str = Depends(internal_request)):
        summaries = [
            {
                **item.model_dump(exclude={"token"}),
                "token_hmac": keyed_digest(app_config.PUBLIC_TOKEN_HMAC_KEY, "venue-token", item.token),
            }
            for item in data.venues
        ]
        return signed_json(request, {"drift": await request.app.state.repo.reconcile_venues(summaries)})

    @application.post("/api/internal/submissions/pull")
    async def pull(data: PullRequest, request: Request, _: str = Depends(internal_request)):
        supported = "rsa-oaep-sha256+aes-256-gcm-v1"
        if supported not in data.supported_encryption_versions:
            raise HTTPException(409, "no_supported_encryption_version")
        lease_id, expires_at, rows = await request.app.state.repo.pull_submissions(data.worker_id, data.limit)
        items = []
        for row in rows:
            submission_id = str(row["submission_id"])
            items.append({
                **{key: row[key] for key in (
                    "submission_id", "local_venue_id", "encrypted_payload", "wrapped_data_key", "key_id",
                    "algorithm_version", "payload_nonce", "ciphertext_sha256", "photo_nonce",
                    "photo_ciphertext_sha256", "photo_size", "photo_mime_type",
                )},
                "received_at": _iso(row.get("received_at")),
                "photo_download_path": f"/api/internal/submissions/{submission_id}/photo/{lease_id}",
            })
        return signed_json(request, {"lease_id": lease_id, "lease_expires_at": _iso(expires_at), "items": items})

    @application.get("/api/internal/submissions/{submission_id}/photo/{lease_id}")
    async def leased_photo(submission_id: str, lease_id: str, request: Request, _: str = Depends(internal_request)):
        row = await request.app.state.repo.get_leased_photo(submission_id, lease_id)
        if not row:
            raise HTTPException(404, "lease_or_photo_not_found")
        path = (app_config.PHOTO_DIR / str(row["photo_object_key"])).resolve()
        if app_config.PHOTO_DIR.resolve() not in path.parents or not path.is_file():
            raise HTTPException(404, "photo_not_found")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Content-SHA256": str(row["photo_ciphertext_sha256"]),
                "Content-Length": str(row["photo_size"]),
            },
        )

    @application.post("/api/internal/submissions/ack")
    async def ack(data: AckRequest, request: Request, _: str = Depends(internal_request)):
        results = [
            {"submission_id": str(item.submission_id), "status": item.status, "reason_code": item.reason_code or ""}
            for item in data.results
        ]
        applied = await request.app.state.repo.acknowledge(str(data.lease_id), results)
        return signed_json(request, {"applied": applied})

    @application.post("/api/internal/submissions/renew-lease")
    async def renew(data: RenewLeaseRequest, request: Request, _: str = Depends(internal_request)):
        try:
            expires_at = await request.app.state.repo.renew_lease(str(data.lease_id), data.worker_id)
        except LookupError as exc:
            raise HTTPException(404, "lease_not_found") from exc
        return signed_json(request, {"lease_id": str(data.lease_id), "lease_expires_at": _iso(expires_at)})

    @application.get("/api/internal/status")
    async def internal_status(request: Request, _: str = Depends(internal_request)):
        payload = await request.app.state.repo.status()
        payload["oldest_pending_at"] = _iso(payload.get("oldest_pending_at"))
        payload.update({"status": "ok", "active_key_id": app_config.ACTIVE_ENCRYPTION_KEY_ID})
        return signed_json(request, payload)

    return application


app = create_app()
