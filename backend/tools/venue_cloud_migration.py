"""Audit or prepare replacement venue tokens for the cloud cutover.

The default mode is read-only. Applying requires an explicit backup reference,
an exact expected active-venue count and an exclusive mode-0600 output file.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import aiomysql

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from services.qmf_config import encrypt_secret


def token_digest(token: str) -> str:
    return hmac.new(
        settings.registry_hmac_key.encode(),
        f"venue-token:{token}".encode(),
        hashlib.sha256,
    ).hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="场所码云端切换审计与批量换码")
    parser.add_argument("--apply", action="store_true", help="写入待确认的新令牌和 Outbox")
    parser.add_argument("--backup-reference", default="", help="执行前 RegistryData 备份记录")
    parser.add_argument("--expected-active-count", type=int, default=-1)
    parser.add_argument("--output", type=Path, help="仅 --apply 使用，保存新二维码清单")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> dict:
    if args.apply and (
        not args.backup_reference.strip()
        or args.expected_active_count < 0
        or args.output is None
    ):
        raise SystemExit("--apply requires --backup-reference, --expected-active-count and --output")
    if args.apply and args.output.exists():
        raise SystemExit("output already exists; refusing to overwrite a token manifest")

    temporary_output: Path | None = None
    committed = False
    conn = await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_REGISTRY_DB,
        autocommit=False,
        charset="utf8mb4",
    )
    try:
        await conn.begin()
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(
                "SELECT id,name,token_version,config_revision,pending_token_version "
                "FROM _venue_codes WHERE status='active' ORDER BY id FOR UPDATE"
            )
            rows = list(await cur.fetchall())
            pending = [row for row in rows if row["pending_token_version"] is not None]
            summary = {
                "mode": "apply" if args.apply else "audit",
                "active_count": len(rows),
                "already_pending_count": len(pending),
                "backup_reference": args.backup_reference if args.apply else None,
            }
            if not args.apply:
                await conn.rollback()
                return summary
            if len(rows) != args.expected_active_count:
                raise RuntimeError(
                    f"active venue count changed: expected {args.expected_active_count}, actual {len(rows)}"
                )
            if pending:
                raise RuntimeError("one or more active venues already have a pending token rotation")

            output_rows = []
            base_url = settings.VENUE_PUBLIC_BASE_URL.rstrip("/")
            if base_url != "https://47.100.44.36":
                raise RuntimeError("VENUE_PUBLIC_BASE_URL must be https://47.100.44.36 for cutover")
            for row in rows:
                token = secrets.token_urlsafe(32)
                token_version = int(row["token_version"]) + 1
                revision = int(row["config_revision"]) + 1
                request_id = str(uuid.uuid4())
                await cur.execute(
                    "UPDATE _venue_codes SET pending_token_hmac=%s,pending_encrypted_token=%s,"
                    "pending_token_version=%s,config_revision=%s,cloud_sync_status='pending',"
                    "cloud_sync_error_code=NULL WHERE id=%s",
                    (token_digest(token), encrypt_secret(token), token_version, revision, row["id"]),
                )
                await cur.execute(
                    "INSERT INTO _venue_cloud_outbox "
                    "(venue_id,config_revision,action,request_id,status) VALUES (%s,%s,'rotate',%s,'pending')",
                    (row["id"], revision, request_id),
                )
                output_rows.append({
                    "venue_id": int(row["id"]),
                    "venue_name": str(row["name"]),
                    "token_version": token_version,
                    "url": f"{base_url}/venue/{quote(token, safe='')}",
                })
        temporary_output = args.output.with_name(args.output.name + ".pending")
        if temporary_output.exists():
            raise RuntimeError("pending output already exists; inspect it before retrying")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary_output, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(
                {**summary, "venues": output_rows},
                output,
                ensure_ascii=False,
                indent=2,
            )
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        await conn.commit()
        committed = True
        os.replace(temporary_output, args.output)
        return {**summary, "output": str(args.output), "prepared_count": len(output_rows)}
    except Exception:
        await conn.rollback()
        if temporary_output is not None and not committed:
            temporary_output.unlink(missing_ok=True)
        raise
    finally:
        conn.close()
        await conn.wait_closed()


def main() -> None:
    result = asyncio.run(run(parse_args()))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
