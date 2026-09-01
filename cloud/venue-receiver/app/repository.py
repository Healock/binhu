from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiomysql


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MySQLRepository:
    def __init__(self, pool: aiomysql.Pool, *, queued_retention_hours: int, lease_seconds: int):
        self.pool = pool
        self.queued_retention_hours = queued_retention_hours
        self.lease_seconds = lease_seconds

    @classmethod
    async def connect(cls, settings) -> "MySQLRepository":
        pool = await aiomysql.create_pool(
            host=settings.MYSQL_HOST,
            port=settings.MYSQL_PORT,
            user=settings.MYSQL_USER,
            password=settings.MYSQL_PASSWORD,
            db=settings.MYSQL_DATABASE,
            minsize=1,
            maxsize=10,
            autocommit=False,
            charset="utf8mb4",
        )
        repo = cls(pool, queued_retention_hours=settings.QUEUED_RETENTION_HOURS, lease_seconds=settings.LEASE_SECONDS)
        await repo.initialize(settings.SCHEMA_PATH)
        return repo

    async def close(self) -> None:
        self.pool.close()
        await self.pool.wait_closed()

    async def ping(self) -> None:
        """Execute a lightweight query used by the readiness probe."""
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()

    async def initialize(self, schema_path: Path) -> None:
        statements = [part.strip() for part in schema_path.read_text(encoding="utf-8").split(";") if part.strip()]
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                for statement in statements:
                    await cur.execute(statement)
            await conn.commit()

    async def get_venue_by_token(self, token_hmac: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT local_venue_id,display_name,status,token_version,config_revision "
                    "FROM venues WHERE token_hmac=%s",
                    (token_hmac,),
                )
                current = await cur.fetchone()
                if current:
                    return current
                await cur.execute(
                    "SELECT retired.local_venue_id,venue.display_name,'retired' AS status,"
                    "retired.token_version,venue.config_revision FROM retired_venue_tokens retired "
                    "JOIN venues venue ON venue.local_venue_id=retired.local_venue_id "
                    "WHERE retired.token_hmac=%s",
                    (token_hmac,),
                )
                return await cur.fetchone()

    async def check_rate_limits(self, limits: list[tuple[str, int]]) -> bool:
        exceeded = False
        async with self.pool.acquire() as conn:
            try:
                await conn.begin()
                async with conn.cursor() as cur:
                    for bucket_key, limit in limits:
                        await cur.execute(
                            "INSERT INTO rate_limit_buckets "
                            "(bucket_key,window_started_at,request_count,expires_at) "
                            "VALUES (%s,UTC_TIMESTAMP(),1,DATE_ADD(UTC_TIMESTAMP(),INTERVAL 10 MINUTE)) "
                            "ON DUPLICATE KEY UPDATE "
                            "request_count=IF(window_started_at<DATE_SUB(UTC_TIMESTAMP(),INTERVAL 1 MINUTE),1,request_count+1),"
                            "window_started_at=IF(window_started_at<DATE_SUB(UTC_TIMESTAMP(),INTERVAL 1 MINUTE),UTC_TIMESTAMP(),window_started_at),"
                            "expires_at=DATE_ADD(UTC_TIMESTAMP(),INTERVAL 10 MINUTE)",
                            (bucket_key,),
                        )
                        await cur.execute(
                            "SELECT request_count FROM rate_limit_buckets WHERE bucket_key=%s",
                            (bucket_key,),
                        )
                        row = await cur.fetchone()
                        exceeded = exceeded or int(row[0]) > limit
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise
        return not exceeded

    async def issue_form_token(self, token_hmac: str, venue_id: int, expires_at: datetime) -> None:
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO form_tokens (token_hmac,local_venue_id,expires_at) VALUES (%s,%s,%s)",
                    (token_hmac, venue_id, expires_at),
                )
            await conn.commit()

    async def consume_form_token(self, token_hmac: str, venue_id: int) -> bool:
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE form_tokens SET consumed_at=UTC_TIMESTAMP() "
                    "WHERE token_hmac=%s AND local_venue_id=%s AND consumed_at IS NULL AND expires_at>=UTC_TIMESTAMP()",
                    (token_hmac, venue_id),
                )
                consumed = cur.rowcount == 1
            await conn.commit()
            return consumed

    async def get_submission(self, submission_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT submission_id,local_venue_id,request_fingerprint,state FROM submissions WHERE submission_id=%s",
                    (submission_id,),
                )
                return await cur.fetchone()

    async def create_submission(self, item: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO submissions (submission_id,local_venue_id,request_fingerprint,state,"
                        "encrypted_payload,wrapped_data_key,key_id,algorithm_version,payload_nonce,ciphertext_sha256,"
                        "photo_object_key,photo_nonce,photo_ciphertext_sha256,photo_size,photo_mime_type,expires_at) "
                        "VALUES (%s,%s,%s,'queued',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (
                            item["submission_id"], item["local_venue_id"], item["request_fingerprint"],
                            item["encrypted_payload"], item["wrapped_data_key"], item["key_id"],
                            item["algorithm_version"], item["payload_nonce"], item["ciphertext_sha256"],
                            item["photo_object_key"], item["photo_nonce"], item["photo_ciphertext_sha256"],
                            item["photo_size"], item["photo_mime_type"],
                            utcnow() + timedelta(hours=self.queued_retention_hours),
                        ),
                    )
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def claim_nonce(self, nonce: str, request_id: str) -> bool:
        digest = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        async with self.pool.acquire() as conn:
            try:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO internal_request_nonces (nonce_hash,request_id,expires_at) "
                        "VALUES (%s,%s,DATE_ADD(UTC_TIMESTAMP(),INTERVAL 10 MINUTE))",
                        (digest, request_id),
                    )
                await conn.commit()
                return True
            except aiomysql.IntegrityError:
                await conn.rollback()
                return False

    async def get_request_result(self, request_id: str, operation: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT response_json FROM internal_request_results "
                    "WHERE request_id=%s AND operation=%s AND expires_at>=UTC_TIMESTAMP()",
                    (request_id, operation),
                )
                row = await cur.fetchone()
                return json.loads(row[0]) if row else None

    async def save_request_result(self, request_id: str, operation: str, result: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO internal_request_results (request_id,operation,response_json,expires_at) "
                    "VALUES (%s,%s,%s,DATE_ADD(UTC_TIMESTAMP(),INTERVAL 7 DAY)) "
                    "ON DUPLICATE KEY UPDATE response_json=VALUES(response_json)",
                    (request_id, operation, json.dumps(result, ensure_ascii=False, separators=(",", ":"))),
                )
            await conn.commit()

    async def upsert_venue(self, item: dict[str, Any]) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            try:
                await conn.begin()
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT config_revision,status,token_hmac,token_version FROM venues WHERE local_venue_id=%s FOR UPDATE",
                        (item["local_venue_id"],),
                    )
                    current = await cur.fetchone()
                    if current and int(current["config_revision"]) >= int(item["config_revision"]):
                        await conn.commit()
                        return {
                            "applied": False,
                            "config_revision": int(current["config_revision"]),
                            "status": current["status"],
                            "token_version": int(current["token_version"]),
                        }
                    if current and str(current["token_hmac"]) != str(item["token_hmac"]):
                        await cur.execute(
                            "INSERT IGNORE INTO retired_venue_tokens (token_hmac,local_venue_id,token_version) "
                            "VALUES (%s,%s,%s)",
                            (current["token_hmac"], item["local_venue_id"], current["token_version"]),
                        )
                    await cur.execute(
                        "INSERT INTO venues (local_venue_id,display_name,status,token_hmac,token_version,config_revision,last_request_id) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                        "display_name=VALUES(display_name),status=VALUES(status),token_hmac=VALUES(token_hmac),"
                        "token_version=VALUES(token_version),config_revision=VALUES(config_revision),last_request_id=VALUES(last_request_id)",
                        (
                            item["local_venue_id"], item["display_name"], item["status"], item["token_hmac"],
                            item["token_version"], item["config_revision"], item["request_id"],
                        ),
                    )
                await conn.commit()
                return {
                    "applied": True,
                    "config_revision": int(item["config_revision"]),
                    "status": item["status"],
                    "token_version": int(item["token_version"]),
                }
            except Exception:
                await conn.rollback()
                raise

    async def reconcile_venues(self, summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ids = [int(item["local_venue_id"]) for item in summaries]
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                if ids:
                    placeholders = ",".join(["%s"] * len(ids))
                    await cur.execute(
                        f"SELECT local_venue_id,status,token_hmac,token_version,config_revision FROM venues WHERE local_venue_id IN ({placeholders})",
                        tuple(ids),
                    )
                else:
                    await cur.execute(
                        "SELECT local_venue_id,status,token_hmac,token_version,config_revision FROM venues WHERE 1=0"
                    )
                current = {int(row["local_venue_id"]): row for row in await cur.fetchall()}
                await cur.execute("SELECT local_venue_id FROM venues WHERE status='active'")
                active_cloud_ids = {int(row["local_venue_id"]) for row in await cur.fetchall()}
        drift: list[dict[str, Any]] = []
        for expected in summaries:
            venue_id = int(expected["local_venue_id"])
            actual = current.get(venue_id)
            if not actual or any(str(actual[key]) != str(expected[key]) for key in ("status", "token_hmac", "token_version", "config_revision")):
                drift.append({"local_venue_id": venue_id, "reason_code": "missing" if not actual else "revision_or_state_mismatch"})
        for venue_id in sorted(active_cloud_ids - set(ids)):
            drift.append({"local_venue_id": venue_id, "reason_code": "unexpected_active_cloud_venue"})
        return drift

    async def pull_submissions(self, worker_id: str, limit: int) -> tuple[str, datetime, list[dict[str, Any]]]:
        lease_id = str(uuid.uuid4())
        lease_expires_at = utcnow() + timedelta(seconds=self.lease_seconds)
        async with self.pool.acquire() as conn:
            try:
                await conn.begin()
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT submission_id FROM submissions WHERE "
                        "(state='queued' OR (state='leased' AND lease_expires_at<UTC_TIMESTAMP())) "
                        "AND expires_at>=UTC_TIMESTAMP() ORDER BY received_at LIMIT %s FOR UPDATE SKIP LOCKED",
                        (limit,),
                    )
                    ids = [row["submission_id"] for row in await cur.fetchall()]
                    if ids:
                        placeholders = ",".join(["%s"] * len(ids))
                        await cur.execute(
                            f"UPDATE submissions SET state='leased',lease_id=%s,lease_owner=%s,lease_expires_at=%s,attempt_count=attempt_count+1 "
                            f"WHERE submission_id IN ({placeholders})",
                            tuple([lease_id, worker_id, lease_expires_at, *ids]),
                        )
                        await cur.execute(
                            f"SELECT submission_id,local_venue_id,encrypted_payload,wrapped_data_key,key_id,algorithm_version,"
                            f"payload_nonce,ciphertext_sha256,photo_nonce,photo_ciphertext_sha256,photo_size,photo_mime_type,received_at "
                            f"FROM submissions WHERE submission_id IN ({placeholders}) ORDER BY received_at",
                            tuple(ids),
                        )
                        rows = list(await cur.fetchall())
                    else:
                        rows = []
                await conn.commit()
                return lease_id, lease_expires_at, rows
            except Exception:
                await conn.rollback()
                raise

    async def get_leased_photo(self, submission_id: str, lease_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT photo_object_key,photo_ciphertext_sha256,photo_size FROM submissions "
                    "WHERE submission_id=%s AND lease_id=%s AND state='leased' AND lease_expires_at>=UTC_TIMESTAMP()",
                    (submission_id, lease_id),
                )
                return await cur.fetchone()

    async def acknowledge(self, lease_id: str, results: list[dict[str, str]]) -> list[dict[str, str]]:
        applied: list[dict[str, str]] = []
        terminal = {"accepted", "rejected", "uncertain"}
        async with self.pool.acquire() as conn:
            try:
                await conn.begin()
                async with conn.cursor() as cur:
                    for result in results:
                        state = result["status"]
                        reason = result.get("reason_code") or None
                        if state == "retry_later":
                            await cur.execute(
                                "UPDATE submissions SET state='queued',lease_id=NULL,lease_owner=NULL,lease_expires_at=NULL,safe_reason_code=%s "
                                "WHERE submission_id=%s AND lease_id=%s AND state='leased'",
                                (reason, result["submission_id"], lease_id),
                            )
                        elif state in terminal:
                            await cur.execute(
                                "UPDATE submissions SET state=%s,acknowledged_at=UTC_TIMESTAMP(),safe_reason_code=%s "
                                "WHERE submission_id=%s AND lease_id=%s AND state='leased'",
                                (state, reason, result["submission_id"], lease_id),
                            )
                        else:
                            continue
                        if cur.rowcount == 1:
                            applied.append({"submission_id": result["submission_id"], "status": state})
                await conn.commit()
                return applied
            except Exception:
                await conn.rollback()
                raise

    async def renew_lease(self, lease_id: str, worker_id: str) -> datetime:
        expires_at = utcnow() + timedelta(seconds=self.lease_seconds)
        async with self.pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE submissions SET lease_expires_at=%s WHERE lease_id=%s AND lease_owner=%s AND state='leased'",
                    (expires_at, lease_id, worker_id),
                )
                if cur.rowcount == 0:
                    raise LookupError("lease_not_found")
            await conn.commit()
        return expires_at

    async def status(self) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            async with conn.cursor(aiomysql.DictCursor) as cur:
                await cur.execute(
                    "SELECT SUM(state IN ('queued','leased')) AS pending_count,"
                    "SUM(state='uncertain') AS uncertain_count,MIN(CASE WHEN state IN ('queued','leased') THEN received_at END) AS oldest_pending_at "
                    "FROM submissions"
                )
                row = await cur.fetchone() or {}
                await cur.execute("SELECT COUNT(*) AS venue_count FROM venues WHERE status='active'")
                venue = await cur.fetchone() or {}
        return {
            "pending_count": int(row.get("pending_count") or 0),
            "uncertain_count": int(row.get("uncertain_count") or 0),
            "oldest_pending_at": row.get("oldest_pending_at"),
            "active_venue_count": int(venue.get("venue_count") or 0),
        }

    async def expire_records(self, accepted_retention_hours: int, audit_retention_days: int) -> list[str]:
        async with self.pool.acquire() as conn:
            try:
                await conn.begin()
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT photo_object_key FROM submissions WHERE "
                        "(state='accepted' AND acknowledged_at<DATE_SUB(UTC_TIMESTAMP(),INTERVAL %s HOUR)) OR expires_at<UTC_TIMESTAMP()",
                        (accepted_retention_hours,),
                    )
                    keys = [str(row[0]) for row in await cur.fetchall()]
                    await cur.execute(
                        "DELETE FROM submissions WHERE (state='accepted' AND acknowledged_at<DATE_SUB(UTC_TIMESTAMP(),INTERVAL %s HOUR)) "
                        "OR expires_at<UTC_TIMESTAMP()",
                        (accepted_retention_hours,),
                    )
                    await cur.execute("DELETE FROM form_tokens WHERE expires_at<UTC_TIMESTAMP()")
                    await cur.execute("DELETE FROM internal_request_nonces WHERE expires_at<UTC_TIMESTAMP()")
                    await cur.execute("DELETE FROM internal_request_results WHERE expires_at<UTC_TIMESTAMP()")
                    await cur.execute("DELETE FROM rate_limit_buckets WHERE expires_at<UTC_TIMESTAMP()")
                    await cur.execute(
                        "DELETE FROM delivery_events WHERE created_at<DATE_SUB(UTC_TIMESTAMP(),INTERVAL %s DAY)",
                        (audit_retention_days,),
                    )
                await conn.commit()
                return keys
            except Exception:
                await conn.rollback()
                raise
