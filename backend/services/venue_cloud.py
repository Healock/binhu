from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiomysql

from config import settings
from database import db_manager
from services.qmf_config import decrypt_secret, encrypt_secret
from services.registry_security import hmac_digest, normalize_identity, normalize_phone
from services.venue_cloud_client import VenueCloudClient, VenueCloudClientError
from services.venue_cloud_security import VenueCloudSecurityError, decrypt_submission


SUPPORTED_ENCRYPTION_VERSION = "rsa-oaep-sha256+aes-256-gcm-v1"
SAFE_REJECTION_CODES = {"venue_inactive", "payload_invalid", "photo_invalid", "key_unknown", "ciphertext_invalid"}
_runtime_status: dict[str, Any] = {
    "last_success_at": None,
    "last_error_code": None,
    "last_pull_count": 0,
    "last_reconcile_at": None,
    "cloud_pending_count": None,
    "cloud_oldest_pending_at": None,
    "cloud_uncertain_count": None,
    "cloud_active_key_id": None,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def cloud_enabled() -> bool:
    return bool(settings.VENUE_CLOUD_SYNC_ENABLED or settings.VENUE_CLOUD_PULL_ENABLED)


async def enqueue_venue_cloud_outbox(cur, venue_id: int, config_revision: int, action: str) -> str | None:
    if not settings.VENUE_CLOUD_SYNC_ENABLED:
        return None
    request_id = str(uuid.uuid4())
    await cur.execute(
        "INSERT INTO _venue_cloud_outbox (venue_id,config_revision,action,request_id,status) "
        "VALUES (%s,%s,%s,%s,'pending')",
        (venue_id, config_revision, action, request_id),
    )
    return request_id


async def _claim_outbox_rows(limit: int = 20) -> list[dict[str, Any]]:
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        try:
            await conn.begin()
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "UPDATE _venue_cloud_outbox SET status='pending' "
                    "WHERE status='sending' AND updated_at<DATE_SUB(UTC_TIMESTAMP(),INTERVAL 10 MINUTE)"
                )
                await cur.execute(
                    "SELECT outbox.id,outbox.venue_id,outbox.config_revision,outbox.action,outbox.request_id,"
                    "outbox.attempt_count,"
                    "venue.name,venue.status,venue.encrypted_token,venue.token_version,"
                    "venue.pending_encrypted_token,venue.pending_token_version "
                    "FROM _venue_cloud_outbox outbox JOIN _venue_codes venue ON venue.id=outbox.venue_id "
                    "WHERE outbox.status IN ('pending','error') AND outbox.next_attempt_at<=UTC_TIMESTAMP() "
                    "ORDER BY CASE WHEN outbox.action IN ('delete','disable') THEN 0 ELSE 1 END,outbox.id "
                    "LIMIT %s FOR UPDATE SKIP LOCKED",
                    (limit,),
                )
                rows = list(await cur.fetchall())
                if rows:
                    ids = [int(row["id"]) for row in rows]
                    placeholders = ",".join(["%s"] * len(ids))
                    await cur.execute(
                        f"UPDATE _venue_cloud_outbox SET status='sending',attempt_count=attempt_count+1 WHERE id IN ({placeholders})",
                        tuple(ids),
                    )
            await conn.commit()
            return rows
        except Exception:
            await conn.rollback()
            raise


async def _finish_outbox(row: dict[str, Any], result: dict[str, Any]) -> None:
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        try:
            await conn.begin()
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE _venue_cloud_outbox SET status='sent',last_error_code=NULL WHERE id=%s AND status='sending'",
                    (row["id"],),
                )
                if row["action"] == "rotate":
                    await cur.execute(
                        "UPDATE _venue_codes SET token_hmac=pending_token_hmac,encrypted_token=pending_encrypted_token,"
                        "token_version=pending_token_version,pending_token_hmac=NULL,pending_encrypted_token=NULL,"
                        "pending_token_version=NULL,cloud_sync_status='confirmed',cloud_synced_revision=%s,"
                        "cloud_synced_at=UTC_TIMESTAMP(),cloud_sync_error_code=NULL "
                        "WHERE id=%s AND config_revision=%s",
                        (result["config_revision"], row["venue_id"], row["config_revision"]),
                    )
                else:
                    await cur.execute(
                        "UPDATE _venue_codes SET cloud_sync_status='confirmed',cloud_synced_revision=%s,"
                        "cloud_synced_at=UTC_TIMESTAMP(),cloud_sync_error_code=NULL "
                        "WHERE id=%s AND config_revision=%s",
                        (result["config_revision"], row["venue_id"], row["config_revision"]),
                    )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise


async def _fail_outbox(row_id: int, reason_code: str, attempt_count: int) -> None:
    retry_seconds = min(300, 2 ** min(max(1, attempt_count), 8))
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _venue_cloud_outbox SET status='error',last_error_code=%s,"
                "next_attempt_at=DATE_ADD(UTC_TIMESTAMP(),INTERVAL %s SECOND) "
                "WHERE id=%s",
                (reason_code[:100], retry_seconds, row_id),
            )
            await cur.execute(
                "UPDATE _venue_codes venue JOIN _venue_cloud_outbox outbox ON outbox.venue_id=venue.id "
                "SET venue.cloud_sync_status='error',venue.cloud_sync_error_code=%s WHERE outbox.id=%s",
                (reason_code[:100], row_id),
            )
        await conn.commit()


async def process_outbox_once(client: VenueCloudClient) -> int:
    if not settings.VENUE_CLOUD_SYNC_ENABLED:
        return 0
    rows = await _claim_outbox_rows()
    completed = 0
    for row in rows:
        encrypted_token = row["pending_encrypted_token"] if row["action"] == "rotate" else row["encrypted_token"]
        token_version = row["pending_token_version"] if row["action"] == "rotate" else row["token_version"]
        try:
            token = decrypt_secret(encrypted_token)
            payload = {
                "request_id": str(row["request_id"]),
                "display_name": str(row["name"]),
                "status": str(row["status"]),
                "token": token,
                "token_version": int(token_version),
                "config_revision": int(row["config_revision"]),
            }
            result = await client.request_json("PUT", f"/api/internal/venues/{int(row['venue_id'])}", payload)
            if int(result.get("config_revision", 0)) < int(row["config_revision"]):
                raise VenueCloudClientError("cloud_revision_not_applied")
            await _finish_outbox(row, result)
            completed += 1
        except (VenueCloudClientError, VenueCloudSecurityError) as exc:
            await _fail_outbox(
                int(row["id"]),
                getattr(exc, "reason_code", "security_error"),
                int(row.get("attempt_count") or 1),
            )
        except Exception:
            await _fail_outbox(
                int(row["id"]),
                "unexpected_sync_error",
                int(row.get("attempt_count") or 1),
            )
    return completed


def _validate_decrypted_payload(payload: dict[str, Any], photo: bytes, mime: str) -> tuple[str, str, str, str, str]:
    try:
        name = str(payload["name"]).strip()
        identity = normalize_identity(str(payload["identity_number"]))
        phone = normalize_phone(str(payload["phone"]))
        address = str(payload["address"]).strip()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("payload_invalid") from exc
    if not name or len(name) > 100 or not address or len(address) > 500:
        raise ValueError("payload_invalid")
    if not (len(identity) == 18 and identity[:17].isdigit() and (identity[-1].isdigit() or identity[-1] == "X")):
        raise ValueError("payload_invalid")
    if not (len(phone) == 11 and phone.isdigit() and phone.startswith("1")):
        raise ValueError("payload_invalid")
    magic = {
        "image/jpeg": b"\xff\xd8\xff",
        "image/png": b"\x89PNG\r\n\x1a\n",
        "image/webp": b"RIFF",
    }
    if not photo or len(photo) > settings.VENUE_PHOTO_MAX_BYTES or mime not in magic or not photo.startswith(magic[mime]):
        raise ValueError("photo_invalid")
    if mime == "image/webp" and photo[8:12] != b"WEBP":
        raise ValueError("photo_invalid")
    return name, identity, phone, address, mime


async def _existing_cloud_visit(submission_id: str) -> bool:
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM _venue_visits WHERE cloud_submission_id=%s", (submission_id,))
            return bool(await cur.fetchone())


async def _record_ingest_event(submission_id: str, venue_id: int | None, status: str, reason: str | None, key_id: str | None) -> None:
    try:
        pool = db_manager.get_pool("registry")
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT IGNORE INTO _venue_cloud_ingest_events "
                    "(cloud_submission_id,venue_id,result_status,safe_reason_code,key_id) VALUES (%s,%s,%s,%s,%s)",
                    (submission_id, venue_id, status, reason, key_id),
                )
            await conn.commit()
    except Exception:
        return


async def _ingest_item(client: VenueCloudClient, lease_id: str, item: dict[str, Any]) -> dict[str, str]:
    submission_id = str(item.get("submission_id") or "")
    venue_id = int(item.get("local_venue_id") or 0)
    key_id = str(item.get("key_id") or "")
    if await _existing_cloud_visit(submission_id):
        return {"submission_id": submission_id, "status": "accepted", "reason_code": ""}
    try:
        encrypted_payload = base64.urlsafe_b64decode(str(item["encrypted_payload"]) + "=" * (-len(str(item["encrypted_payload"])) % 4))
        if hashlib.sha256(encrypted_payload).hexdigest() != str(item["ciphertext_sha256"]):
            raise ValueError("ciphertext_invalid")
        request_id = str(uuid.uuid4())
        encrypted_photo = await client.download_photo(
            str(item["photo_download_path"]),
            request_id,
            int(item["photo_size"]),
            str(item["photo_ciphertext_sha256"]),
        )
        payload, photo = decrypt_submission(item, encrypted_photo, settings.VENUE_CLOUD_DECRYPTION_KEY_DIR)
        name, identity, phone, address, mime = _validate_decrypted_payload(payload, photo, str(item["photo_mime_type"]))
    except VenueCloudClientError:
        return {"submission_id": submission_id, "status": "retry_later", "reason_code": "photo_download_failed"}
    except VenueCloudSecurityError as exc:
        reason = "key_unknown" if "私钥不存在" in str(exc) else "ciphertext_invalid"
        await _record_ingest_event(submission_id, venue_id, "rejected", reason, key_id)
        return {"submission_id": submission_id, "status": "rejected", "reason_code": reason}
    except (ValueError, KeyError, TypeError) as exc:
        reason = str(exc) if str(exc) in SAFE_REJECTION_CODES else "payload_invalid"
        await _record_ingest_event(submission_id, venue_id, "rejected", reason, key_id)
        return {"submission_id": submission_id, "status": "rejected", "reason_code": reason}

    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[mime]
    storage_root = Path(settings.VENUE_PHOTO_DIR).resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    storage_key = f"{secrets.token_hex(16)}{suffix}"
    final_path = (storage_root / storage_key).resolve()
    temp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    if storage_root not in final_path.parents:
        return {"submission_id": submission_id, "status": "retry_later", "reason_code": "storage_unavailable"}
    pool = db_manager.get_pool("registry")
    conn = await pool.acquire()
    try:
        with temp_path.open("xb") as output:
            output.write(photo)
            output.flush()
            __import__("os").fsync(output.fileno())
        temp_path.replace(final_path)
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM _venue_codes WHERE id=%s AND status='active' FOR UPDATE", (venue_id,))
            if not await cur.fetchone():
                await conn.rollback()
                final_path.unlink(missing_ok=True)
                await _record_ingest_event(submission_id, venue_id, "rejected", "venue_inactive", key_id)
                return {"submission_id": submission_id, "status": "rejected", "reason_code": "venue_inactive"}
            identity_digest, _ = hmac_digest(identity, kind="identity")
            phone_digest, _ = hmac_digest(phone, kind="phone")
            retention = _utcnow() + timedelta(days=90)
            await cur.execute(
                "INSERT INTO _venue_visits (venue_id,encrypted_name,encrypted_identity,identity_hmac,encrypted_phone,"
                "phone_hmac,encrypted_address,source,cloud_submission_id,cloud_received_at,cloud_key_id,source_ip_hash,retention_until) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'cloud_public_qr',%s,%s,%s,'',%s)",
                (
                    venue_id, encrypt_secret(name), encrypt_secret(identity), identity_digest,
                    encrypt_secret(phone), phone_digest, encrypt_secret(address), submission_id,
                    item.get("received_at"), key_id, retention,
                ),
            )
            visit_id = int(cur.lastrowid)
            await cur.execute(
                "INSERT INTO _venue_visit_photos (visit_id,storage_key,mime_type,size_bytes,sha256,retention_until) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (visit_id, storage_key, mime, len(photo), hashlib.sha256(photo).hexdigest(), retention),
            )
        try:
            await conn.commit()
        except Exception:
            try:
                if await _existing_cloud_visit(submission_id):
                    await _record_ingest_event(submission_id, venue_id, "accepted", None, key_id)
                    return {"submission_id": submission_id, "status": "accepted", "reason_code": ""}
            except Exception:
                pass
            await _record_ingest_event(submission_id, venue_id, "uncertain", "commit_unknown", key_id)
            return {"submission_id": submission_id, "status": "uncertain", "reason_code": "commit_unknown"}
        await _record_ingest_event(submission_id, venue_id, "accepted", None, key_id)
        return {"submission_id": submission_id, "status": "accepted", "reason_code": ""}
    except aiomysql.IntegrityError:
        await conn.rollback()
        final_path.unlink(missing_ok=True)
        if await _existing_cloud_visit(submission_id):
            return {"submission_id": submission_id, "status": "accepted", "reason_code": ""}
        return {"submission_id": submission_id, "status": "retry_later", "reason_code": "database_conflict"}
    except Exception:
        await conn.rollback()
        final_path.unlink(missing_ok=True)
        return {"submission_id": submission_id, "status": "retry_later", "reason_code": "local_storage_error"}
    finally:
        temp_path.unlink(missing_ok=True)
        pool.release(conn)


async def pull_submissions_once(client: VenueCloudClient) -> int:
    if not settings.VENUE_CLOUD_PULL_ENABLED:
        return 0
    request_id = str(uuid.uuid4())
    response = await client.request_json(
        "POST",
        "/api/internal/submissions/pull",
        {
            "request_id": request_id,
            "worker_id": settings.VENUE_CLOUD_WORKER_ID,
            "limit": min(50, max(1, settings.VENUE_CLOUD_PULL_BATCH_SIZE)),
            "supported_encryption_versions": [SUPPORTED_ENCRYPTION_VERSION],
        },
    )
    lease_id = str(response.get("lease_id") or "")
    items = response.get("items") or []
    if not lease_id or not isinstance(items, list):
        raise VenueCloudClientError("invalid_cloud_response")
    if not items:
        return 0
    lease_expires_text = str(response.get("lease_expires_at") or "")
    try:
        lease_expires_at = datetime.fromisoformat(lease_expires_text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise VenueCloudClientError("invalid_cloud_response")
    results = []
    for item in items:
        if lease_expires_at - _utcnow() < timedelta(seconds=60):
            renewed = await client.request_json(
                "POST",
                "/api/internal/submissions/renew-lease",
                {
                    "request_id": str(uuid.uuid4()),
                    "lease_id": lease_id,
                    "worker_id": settings.VENUE_CLOUD_WORKER_ID,
                },
            )
            try:
                lease_expires_at = datetime.fromisoformat(
                    str(renewed["lease_expires_at"]).replace("Z", "+00:00")
                ).replace(tzinfo=None)
            except (KeyError, ValueError):
                raise VenueCloudClientError("invalid_cloud_response")
        results.append(await _ingest_item(client, lease_id, item))
    await client.request_json(
        "POST",
        "/api/internal/submissions/ack",
        {"request_id": str(uuid.uuid4()), "lease_id": lease_id, "results": results},
    )
    return len(results)


async def reconcile_venues_once(client: VenueCloudClient) -> int:
    if not settings.VENUE_CLOUD_SYNC_ENABLED:
        return 0
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id,status,encrypted_token,token_version,config_revision FROM _venue_codes"
            )
            rows = list(await cur.fetchall())
    venues = [
        {
            "local_venue_id": int(row["id"]),
            "status": str(row["status"]),
            "token": decrypt_secret(row["encrypted_token"]),
            "token_version": int(row["token_version"]),
            "config_revision": int(row["config_revision"]),
        }
        for row in rows
    ]
    result = await client.request_json(
        "POST",
        "/api/internal/venues/reconcile",
        {"request_id": str(uuid.uuid4()), "venues": venues},
    )
    drift = result.get("drift") or []
    if drift:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                for item in drift:
                    await cur.execute(
                        "UPDATE _venue_cloud_outbox SET status='pending',next_attempt_at=UTC_TIMESTAMP() "
                        "WHERE venue_id=%s AND config_revision=(SELECT config_revision FROM _venue_codes WHERE id=%s)",
                        (int(item["local_venue_id"]), int(item["local_venue_id"])),
                    )
            await conn.commit()
    return len(drift)


async def get_venue_cloud_status() -> dict[str, Any]:
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT SUM(status IN ('pending','sending','error')) AS outbox_pending,"
                "SUM(status='error') AS outbox_failed FROM _venue_cloud_outbox"
            )
            outbox = await cur.fetchone() or {}
            await cur.execute(
                "SELECT SUM(result_status='uncertain') AS uncertain_count FROM _venue_cloud_ingest_events"
            )
            ingest = await cur.fetchone() or {}
    return {
        "enabled": cloud_enabled(),
        "sync_enabled": settings.VENUE_CLOUD_SYNC_ENABLED,
        "pull_enabled": settings.VENUE_CLOUD_PULL_ENABLED,
        "local_public_entry_enabled": settings.VENUE_LOCAL_PUBLIC_ENTRY_ENABLED,
        "outbox_pending": int(outbox.get("outbox_pending") or 0),
        "outbox_failed": int(outbox.get("outbox_failed") or 0),
        "uncertain_count": int(ingest.get("uncertain_count") or 0),
        **_runtime_status,
    }


async def run_venue_cloud_scheduler() -> None:
    if not cloud_enabled():
        while True:
            await asyncio.sleep(3600)
    delay = max(5, settings.VENUE_CLOUD_PULL_INTERVAL_SECONDS)
    client: VenueCloudClient | None = None
    next_reconcile = 0.0
    try:
        while True:
            try:
                if client is None:
                    client = VenueCloudClient()
                await process_outbox_once(client)
                pulled = await pull_submissions_once(client)
                cloud_status = await client.request_json("GET", "/api/internal/status")
                _runtime_status.update(
                    cloud_pending_count=int(cloud_status.get("pending_count") or 0),
                    cloud_oldest_pending_at=cloud_status.get("oldest_pending_at"),
                    cloud_uncertain_count=int(cloud_status.get("uncertain_count") or 0),
                    cloud_active_key_id=cloud_status.get("active_key_id"),
                )
                if asyncio.get_running_loop().time() >= next_reconcile:
                    await reconcile_venues_once(client)
                    _runtime_status["last_reconcile_at"] = _utcnow().isoformat()
                    next_reconcile = asyncio.get_running_loop().time() + 86400
                _runtime_status.update(last_success_at=_utcnow().isoformat(), last_error_code=None, last_pull_count=pulled)
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except VenueCloudClientError as exc:
                _runtime_status["last_error_code"] = exc.reason_code
                await asyncio.sleep(min(300, delay * 2 + random.randint(0, 5)))
            except Exception:
                _runtime_status["last_error_code"] = "unexpected_worker_error"
                await asyncio.sleep(min(300, delay * 2 + random.randint(0, 5)))
    finally:
        if client is not None:
            await client.close()
