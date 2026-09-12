"""Private per-environment account gateway.

This router is intentionally separate from the production operations API.  It is
only useful in a staging/development process and requires a deployment-provided
shared token; it never exposes password hashes or existing passwords.
"""
from __future__ import annotations

import secrets
import json
from datetime import datetime

import bcrypt
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from config import settings
from database import db_manager
from services.environment_identity import username_allowed_in_environment
from services.session_management import invalidate_all_sessions

router = APIRouter(prefix="/api/internal/environment-accounts", tags=["内部环境账号网关"])


class ResetRequest(BaseModel):
    username: str = Field(min_length=3, max_length=100)


def _require_gateway_token(token: str) -> None:
    raw = settings.ENVIRONMENT_ACCOUNT_GATEWAY_TOKENS.strip()
    try:
        values = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        values = {}
    expected = values.get(settings.APP_ENVIRONMENT) if isinstance(values, dict) else None
    if settings.APP_ENVIRONMENT not in {"development", "staging"} or not isinstance(expected, str) or not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=404, detail="not found")


def _temporary_password(environment: str) -> str:
    prefix = "Dev" if environment == "development" else "Stg"
    return f"{prefix}-{secrets.token_urlsafe(18)}!"


@router.get("", dependencies=[])
async def list_environment_accounts(
    x_environment_account_token: str = Header(default=""),
):
    _require_gateway_token(x_environment_account_token)
    suffix = "@dev" if settings.APP_ENVIRONMENT == "development" else "@staging"
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT username, display_name, password_is_temporary, "
                "last_login_at, last_password_reset_at "
                "FROM _users WHERE username LIKE %s ORDER BY username",
                (f"%{suffix}",),
            )
            rows = await cur.fetchall()
    finally:
        pool.release(conn)
    return {
        "environment": settings.APP_ENVIRONMENT,
        "accounts": [
            {
                "username": str(row[0]),
                "display_name": str(row[1] or ""),
                "status": "active",
                "password_is_temporary": bool(row[2]),
                "last_login_at": row[3].isoformat() + "Z" if row[3] else None,
                "last_reset_at": row[4].isoformat() + "Z" if row[4] else None,
            }
            for row in rows
        ],
    }


@router.post("/reset")
async def reset_environment_account(
    payload: ResetRequest,
    x_environment_account_token: str = Header(default=""),
):
    _require_gateway_token(x_environment_account_token)
    username = payload.username.strip().lower()
    if not username_allowed_in_environment(username, settings.APP_ENVIRONMENT):
        raise HTTPException(status_code=400, detail={"code": "environment_account_mismatch", "message": "账号不属于当前环境"})
    temporary_password = _temporary_password(settings.APP_ENVIRONMENT)
    password_hash = bcrypt.hashpw(temporary_password.encode(), bcrypt.gensalt()).decode()
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM _users WHERE username=%s FOR UPDATE", (username,))
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail={"code": "account_not_found", "message": "环境账号不存在"})
            await cur.execute(
                "UPDATE _users SET password_hash=%s, password_is_temporary=1, "
                "last_password_reset_at=UTC_TIMESTAMP() WHERE id=%s",
                (password_hash, row[0]),
            )
            await invalidate_all_sessions(cur, int(row[0]))
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        pool.release(conn)
    # The plaintext is returned exactly once to the authenticated production
    # operator and is never written to logs or audit records.
    return {
        "environment": settings.APP_ENVIRONMENT,
        "username": username,
        "temporary_password": temporary_password,
        "password_is_temporary": True,
        "issued_at": datetime.utcnow().isoformat() + "Z",
    }
