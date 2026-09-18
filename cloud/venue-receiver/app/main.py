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
import unicodedata
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
from .validation import (
    ValidationError,
    validate_drinking_report_fields,
    validate_public_submission_fields,
    validate_signature_strokes,
)
from .drinking_page import render_drinking_report_page


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


class PublicFormUpdate(BaseModel):
    request_id: uuid.UUID
    form_key: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=200)
    status: Literal["active", "inactive"]
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


class WaitRequest(BaseModel):
    request_id: uuid.UUID
    worker_id: str = Field(min_length=8, max_length=100)
    timeout_seconds: int = Field(default=20, ge=1, le=20)


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


class SubmissionNotifier:
    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._generation = 0

    @property
    def generation(self) -> int:
        return self._generation

    async def notify(self) -> None:
        async with self._condition:
            self._generation += 1
            self._condition.notify_all()

    async def wait_for_change(self, generation: int, timeout_seconds: int) -> bool:
        async with self._condition:
            if self._generation != generation:
                return True
            try:
                await asyncio.wait_for(
                    self._condition.wait_for(lambda: self._generation != generation),
                    timeout=timeout_seconds,
                )
            except TimeoutError:
                return False
            return True


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
    return r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="referrer" content="no-referrer"><title>场所登记</title><style>
:root{font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;color:#17212b;background:#f5f7fb;color-scheme:light}
*{box-sizing:border-box}body{margin:0;min-height:100vh;padding:max(20px,env(safe-area-inset-top)) 16px max(24px,env(safe-area-inset-bottom));background:linear-gradient(180deg,#edf4ff 0,#f5f7fb 220px)}
main{width:min(100%,560px);margin:clamp(12px,6vh,64px) auto 0;background:#fff;border:1px solid #e2e8f0;border-radius:16px;box-shadow:0 12px 32px #1d4ed80f;padding:clamp(22px,5vw,36px)}
.brand{display:inline-flex;align-items:center;gap:8px;color:#2563eb;font-weight:700;font-size:13px;letter-spacing:.04em}.brand i{display:block;width:9px;height:9px;border-radius:50%;background:#2563eb}
h1{font-size:clamp(24px,5vw,30px);line-height:1.25;margin:14px 0 8px;color:#0f172a}p{color:#64748b;line-height:1.6;margin:0 0 26px}
form{display:grid;gap:18px}label{display:grid;gap:7px;color:#334155;font-size:14px;font-weight:650}.label-text{display:flex;align-items:baseline;gap:4px;line-height:1.4}.required{display:inline-block;color:#dc2626;font-weight:750;line-height:1;margin:0}input{width:100%;min-height:46px;border:1px solid #cbd5e1;border-radius:10px;padding:11px 13px;background:#fff;color:#0f172a;font:inherit;transition:border-color .15s,box-shadow .15s}input:focus{outline:0;border-color:#2563eb;box-shadow:0 0 0 3px #2563eb1f}input[aria-invalid=true]{border-color:#dc2626;box-shadow:0 0 0 3px #dc26261a}
input[type=file]{padding:9px;background:#f8fafc}.hint{font-size:12px;font-weight:400;color:#64748b}.field-error{min-height:16px;color:#dc2626;font-size:12px;font-weight:500}.actions{display:grid;gap:10px;margin-top:4px}button{min-height:48px;border:0;border-radius:10px;background:#2563eb;color:#fff;font:inherit;font-weight:700;cursor:pointer;box-shadow:0 5px 12px #2563eb2b}button:hover{background:#1d4ed8}button:disabled{opacity:.6;cursor:wait}.message{min-height:22px;color:#b42318;font-size:14px}.message.success{color:#15803d}@media(max-width:520px){body{padding-left:12px;padding-right:12px}main{margin-top:12px;padding:22px 18px;border-radius:14px}}
</style></head><body><main><div class="brand"><i></i>滨湖新城派出所</div><h1 id="venue">场所登记</h1>
<p>请填写真实、完整的信息。姓名、身份证号码、手机号、地址和照片均为必填项。</p><form id="form" novalidate>
<label><span class="label-text">姓名<span class="required" aria-hidden="true">*</span></span><input name="name" maxlength="100" required autocomplete="name"><span class="field-error" data-error-for="name"></span></label>
<label><span class="label-text">公民身份号码<span class="required" aria-hidden="true">*</span></span><input name="identity_number" maxlength="18" required inputmode="text" autocomplete="off"><span class="hint">请输入18位居民身份证号码</span><span class="field-error" data-error-for="identity_number"></span></label>
<label><span class="label-text">手机号<span class="required" aria-hidden="true">*</span></span><input name="phone" maxlength="11" required inputmode="tel" autocomplete="tel"><span class="field-error" data-error-for="phone"></span></label>
<label><span class="label-text">地址<span class="required" aria-hidden="true">*</span></span><input name="address" maxlength="500" required autocomplete="street-address"><span class="field-error" data-error-for="address"></span></label>
<label><span class="label-text">照片<span class="required" aria-hidden="true">*</span></span><input name="photo" type="file" accept="image/jpeg,image/png,image/webp" required><span class="hint">支持 JPG、PNG 或 WebP，单张不超过 5 MB</span><span class="field-error" data-error-for="photo"></span></label>
<div class="actions"><button type="submit">提交登记</button><div id="message" class="message" role="status" aria-live="polite"></div></div></form></main><script>
const token=decodeURIComponent(location.pathname.split('/').pop()||''),form=document.querySelector('#form'),message=document.querySelector('#message');let formToken='';
const readResponse=async r=>{const contentType=(r.headers.get('content-type')||'').toLowerCase(),text=await r.text();let data=null;if(contentType.includes('application/json')||contentType.includes('+json')){try{const parsed=JSON.parse(text);if(parsed&&typeof parsed==='object')data=parsed}catch(_){}}return {response:r,data}};
const responseMessage=({response,data},fallback)=>{const detail=data&&(typeof data.detail==='string'?data.detail:data.message);if(typeof detail==='string'&&detail.trim())return detail;if(response.status===413)return '照片或上传请求超过网关限制，请压缩照片后重试';if(response.status===404)return '二维码入口失效或接口版本不一致，请重新扫码或联系管理员';if([502,503,504].includes(response.status))return '云端登记服务暂时不可用，请稍后重试';return fallback};
const makeUuid=()=>{if(globalThis.crypto&&typeof globalThis.crypto.randomUUID==='function')return globalThis.crypto.randomUUID();const bytes=new Uint8Array(16);if(globalThis.crypto&&typeof globalThis.crypto.getRandomValues==='function')globalThis.crypto.getRandomValues(bytes);else for(let i=0;i<bytes.length;i++)bytes[i]=Math.floor(Math.random()*256);bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128;const hex=[...bytes].map(value=>value.toString(16).padStart(2,'0')).join('');return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`};
const submissionId=makeUuid();const identityWeights=[7,9,10,5,8,4,2,1,6,3,7,9,10,5,8,4,2],identityChecks='10X98765432';
const normalizeText=value=>value.normalize('NFKC').trim().split(/\s+/u).join(' ');
const identityValid=value=>{const v=normalizeText(value).replaceAll(' ','').toUpperCase();if(!/^[0-9]{17}[0-9X]$/.test(v))return false;const y=Number(v.slice(6,10)),m=Number(v.slice(10,12)),d=Number(v.slice(12,14)),dt=new Date(Date.UTC(y,m-1,d));if(dt.getUTCFullYear()!==y||dt.getUTCMonth()!==m-1||dt.getUTCDate()!==d)return false;return identityChecks[[...v.slice(0,17)].reduce((sum,digit,i)=>sum+Number(digit)*identityWeights[i],0)%11]===v[17]};
const setError=(field,text)=>{const input=form.elements[field],target=document.querySelector(`[data-error-for="${field}"]`);if(target)target.textContent=text||'';if(input)input.setAttribute('aria-invalid',text?'true':'false')};
const validate=()=>{const values=Object.fromEntries(new FormData(form).entries()),errors={};const name=normalizeText(String(values.name||'')),address=normalizeText(String(values.address||'')),phone=String(values.phone||'').trim().replace(/[ -]/g,'');if(!name)errors.name='请填写姓名';else if(/[\u0000-\u001f\u007f-\u009f]/u.test(name))errors.name='姓名不能包含控制字符';if(!identityValid(String(values.identity_number||'')))errors.identity_number='请输入有效的18位身份证号码';if(!/^1[3-9]\d{9}$/.test(phone))errors.phone='请输入有效的11位手机号';if(!address)errors.address='请填写地址';else if(/[\u0000-\u001f\u007f-\u009f]/u.test(address))errors.address='地址不能包含控制字符';if(!(values.photo instanceof File)||!values.photo.size)errors.photo='请选择照片';for(const field of ['name','identity_number','phone','address','photo'])setError(field,errors[field]);return {values:{...values,name,phone,address},errors};};
fetch(`/api/public/venues/${encodeURIComponent(token)}`,{credentials:'omit'}).then(async r=>{const parsed=await readResponse(r);if(!parsed.response.ok)throw new Error(responseMessage(parsed,'二维码不可用'));const d=parsed.data;if(!d||typeof d.name!=='string'||typeof d.form_token!=='string')throw new Error('二维码返回内容无效，请重新扫码或联系管理员');document.querySelector('#venue').textContent=d.name;formToken=d.form_token}).catch(e=>{message.textContent=e.message;form.hidden=true});
let deviceId=localStorage.getItem('binhuVenueDeviceId');if(!deviceId){deviceId=makeUuid();localStorage.setItem('binhuVenueDeviceId',deviceId)}
form.addEventListener('submit',async e=>{e.preventDefault();message.textContent='';message.classList.remove('success');const checked=validate();if(Object.keys(checked.errors).length){message.textContent='请先修正标红字段';return}const button=form.querySelector('button');button.disabled=true;message.textContent='正在提交…';const body=new FormData(form);body.set('name',checked.values.name);body.set('phone',checked.values.phone);body.set('address',checked.values.address);body.set('submission_id',submissionId);body.set('form_token',formToken);body.set('venue_token',token);body.set('device_id',deviceId);try{const parsed=await fetch('/api/public/submissions',{method:'POST',body,credentials:'omit'}).then(readResponse);if(!parsed.response.ok)throw new Error(responseMessage(parsed,'服务器返回了无法识别的错误页面，请稍后重试'));message.textContent='提交成功，工作人员将在平台内核验登记信息';message.classList.add('success');form.querySelectorAll('input,button').forEach(x=>x.disabled=true)}catch(err){message.textContent=err.message||'提交失败，请稍后重试';button.disabled=false}});
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
        application.state.submission_notifier = SubmissionNotifier()
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

    @application.get("/drinking-report/{token}", response_class=HTMLResponse)
    async def drinking_report_page(token: str, request: Request):
        if not 32 <= len(token) <= 200:
            raise HTTPException(404, "二维码不可用")
        form = await request.app.state.repo.get_public_form_by_token(
            keyed_digest(app_config.PUBLIC_TOKEN_HMAC_KEY, "public-form-token", token)
        )
        if not form:
            raise HTTPException(404, "二维码不可用")
        if form["status"] != "active":
            return HTMLResponse(_retired_registration_page(), status_code=410, headers={"Cache-Control": "no-store"})
        return HTMLResponse(render_drinking_report_page(), headers={"Cache-Control": "no-store", "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; form-action 'self'"})

    @application.get("/api/public/forms/{token}")
    async def public_form_info(token: str, request: Request):
        form = await request.app.state.repo.get_public_form_by_token(
            keyed_digest(app_config.PUBLIC_TOKEN_HMAC_KEY, "public-form-token", token)
        )
        if not form or form["status"] != "active":
            raise HTTPException(404, "二维码不存在或已停用")
        form_token = secrets.token_urlsafe(32)
        await request.app.state.repo.issue_public_form_token(
            keyed_digest(app_config.FORM_TOKEN_HMAC_KEY, "public-form-token", form_token),
            str(form["form_key"]),
            _utcnow() + timedelta(seconds=3),
            _utcnow() + timedelta(seconds=app_config.FORM_TOKEN_TTL_SECONDS),
        )
        return JSONResponse({"form_key": str(form["form_key"]), "display_name": str(form["display_name"]), "form_token": form_token}, headers={"Cache-Control": "no-store"})

    @application.post("/api/public/drinking-reports", status_code=202)
    async def public_drinking_report(payload: dict[str, Any], request: Request):
        required = ("submission_id", "form_token", "form_token_value", "device_id", "name", "unit_position", "drinking_at", "drinking_place", "reason", "inviter", "travel_method", "responsible_leader_name", "reporter_signature", "leader_signature")
        if any(not str(payload.get(k) or "").strip() for k in required):
            raise HTTPException(422, "请完整填写必填字段并完成双方签名")
        try:
            submission_id = str(uuid.UUID(str(payload["submission_id"])))
        except ValueError as exc:
            raise HTTPException(422, "submission_id 无效") from exc
        form = await request.app.state.repo.get_public_form_by_token(
            keyed_digest(app_config.PUBLIC_TOKEN_HMAC_KEY, "public-form-token", str(payload["form_token_value"]))
        )
        if not form or form["status"] != "active":
            raise HTTPException(404, "二维码不存在或已停用")
        if str(form["form_key"]) != "drinking_report":
            raise HTTPException(422, "二维码类型无效")
        device_id = str(payload["device_id"])
        if not 16 <= len(device_id) <= 200:
            raise HTTPException(422, "device_id 无效")
        try:
            normalized = validate_drinking_report_fields(payload, timezone_name=app_config.DRINKING_REPORT_TIMEZONE)
            signatures = {
                "reporter": validate_signature_strokes(payload["reporter_signature"], field="reporter_signature", label="报备人签名"),
                "leader": validate_signature_strokes(payload["leader_signature"], field="leader_signature", label="责任领导签名"),
            }
        except ValidationError as exc:
            raise HTTPException(422, exc.message, headers={"X-Binhu-Validation-Field": exc.field}) from exc
        body = {**normalized, "rules_version": "2026-09-16", "rules_acknowledged_at": _iso(_utcnow()), "reporter_signature": signatures["reporter"], "leader_signature": signatures["leader"]}
        fingerprint_fields = {k: str(v) for k, v in normalized.items()}
        fingerprint_fields["reporter_signature"] = json.dumps(signatures["reporter"], sort_keys=True, separators=(",", ":"))
        fingerprint_fields["leader_signature"] = json.dumps(signatures["leader"], sort_keys=True, separators=(",", ":"))
        fingerprint = request_fingerprint(app_config.REQUEST_FINGERPRINT_KEY, fingerprint_fields, b"")
        existing = await request.app.state.repo.get_submission(submission_id)
        if existing:
            if existing.get("public_form_key") == form["form_key"] and existing.get("request_fingerprint") == fingerprint:
                return {"submission_id": submission_id, "status": existing["state"]}
            raise HTTPException(409, "submission_id 已被其他内容使用")
        client_host = request.client.host if request.client else "unknown"
        rate_keys = [
            (keyed_digest(app_config.REQUEST_FINGERPRINT_KEY, "rate-global", "all"), 300),
            (keyed_digest(app_config.REQUEST_FINGERPRINT_KEY, "rate-public-form", str(form["form_key"])), 60),
            (keyed_digest(app_config.REQUEST_FINGERPRINT_KEY, "rate-device", device_id), 10),
            (keyed_digest(app_config.REQUEST_FINGERPRINT_KEY, "rate-client", client_host), 30),
        ]
        if not await request.app.state.repo.check_rate_limits(rate_keys):
            raise HTTPException(429, "提交过于频繁，请稍后再试")
        if not await request.app.state.repo.consume_public_form_token(
            keyed_digest(app_config.FORM_TOKEN_HMAC_KEY, "public-form-token", str(payload["form_token"])), str(form["form_key"])
        ):
            raise HTTPException(400, "报备页面已过期，请重新扫码")
        encrypted = request.app.state.encryptor.encrypt_payload(canonical_json(body))
        await request.app.state.repo.create_submission({"submission_id": submission_id, "submission_kind": "drinking_report", "public_form_key": str(form["form_key"]), "request_fingerprint": fingerprint, "encrypted_payload": encrypted.encrypted_payload, "wrapped_data_key": encrypted.wrapped_data_key, "key_id": encrypted.key_id, "algorithm_version": encrypted.algorithm_version, "payload_nonce": encrypted.payload_nonce, "ciphertext_sha256": encrypted.ciphertext_sha256})
        with suppress(Exception):
            await request.app.state.submission_notifier.notify()
        return {"submission_id": submission_id, "status": "queued"}

    @application.put("/api/internal/public-forms/{form_key}")
    async def put_public_form(form_key: str, data: PublicFormUpdate, request: Request, _: str = Depends(internal_request)):
        if form_key != data.form_key:
            raise HTTPException(422, "form_key 不一致")
        result = await request.app.state.repo.upsert_public_form({**data.model_dump(), "request_id": str(data.request_id), "token_hmac": keyed_digest(app_config.PUBLIC_TOKEN_HMAC_KEY, "public-form-token", data.token)})
        return signed_json(request, result)

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
        try:
            normalized_fields = validate_public_submission_fields(
                name=name,
                identity_number=identity_number,
                phone=phone,
                address=address,
            )
        except ValidationError as exc:
            raise HTTPException(422, exc.message, headers={"X-Binhu-Validation-Field": exc.field}) from exc
        name_value = normalized_fields["name"]
        identity = normalized_fields["identity_number"]
        phone_value = normalized_fields["phone"]
        address_value = normalized_fields["address"]
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
        with suppress(Exception):
            await request.app.state.submission_notifier.notify()
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
        supported = {"rsa-oaep-sha256+aes-256-gcm-v1", "rsa-oaep-sha256+aes-256-gcm-payload-v1"}
        if not supported.intersection(data.supported_encryption_versions):
            raise HTTPException(409, "no_supported_encryption_version")
        lease_id, expires_at, rows = await request.app.state.repo.pull_submissions(data.worker_id, data.limit)
        items = []
        for row in rows:
            submission_id = str(row["submission_id"])
            items.append({
                **{key: row[key] for key in (
                    "submission_id", "submission_kind", "local_venue_id", "public_form_key", "encrypted_payload", "wrapped_data_key", "key_id",
                    "algorithm_version", "payload_nonce", "ciphertext_sha256", "photo_object_key", "photo_nonce",
                    "photo_ciphertext_sha256", "photo_size", "photo_mime_type",
                )},
                "received_at": _iso(row.get("received_at")),
                "photo_download_path": f"/api/internal/submissions/{submission_id}/photo/{lease_id}" if row.get("photo_object_key") else None,
            })
        return signed_json(request, {"lease_id": lease_id, "lease_expires_at": _iso(expires_at), "items": items})

    @application.post("/api/internal/submissions/wait")
    async def wait_for_submissions(data: WaitRequest, request: Request, _: str = Depends(internal_request)):
        notifier = request.app.state.submission_notifier
        generation = notifier.generation
        pending_count = await request.app.state.repo.available_submission_count()
        if pending_count > 0:
            return signed_json(
                request,
                {"available": True, "pending_count": pending_count, "wake_reason": "available"},
            )
        await notifier.wait_for_change(generation, data.timeout_seconds)
        pending_count = await request.app.state.repo.available_submission_count()
        return signed_json(
            request,
            {
                "available": pending_count > 0,
                "pending_count": pending_count,
                "wake_reason": "available" if pending_count > 0 else "timeout",
            },
        )

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
