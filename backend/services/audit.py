"""Super-administrator audit events without request bodies or secrets."""

from typing import Any

from database import db_manager
from services.ops_redaction import sanitized_json


async def record_admin_audit(
    user: dict | None,
    action: str,
    *,
    target_type: str = "",
    target_name: str = "",
    result: str = "success",
    detail: Any = None,
    ip_address: str = "",
    user_agent: str = "",
) -> int:
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO _admin_audit_log (
                    user_id, username, action, target_type, target_name,
                    result, detail_json, ip_address, user_agent, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP())
                """,
                (
                    user.get("id") if user else None,
                    (user or {}).get("username", "")[:50],
                    action[:80],
                    target_type[:50],
                    target_name[:200],
                    result[:20],
                    sanitized_json(detail) if detail is not None else None,
                    ip_address[:45],
                    user_agent[:300],
                ),
            )
            return int(cur.lastrowid)
    finally:
        pool.release(conn)


def request_audit_fields(request) -> dict[str, str]:
    forwarded = request.headers.get("x-forwarded-for", "")
    ip_address = forwarded.split(",", 1)[0].strip()
    if not ip_address and request.client:
        ip_address = request.client.host
    return {
        "ip_address": ip_address,
        "user_agent": request.headers.get("user-agent", ""),
    }
