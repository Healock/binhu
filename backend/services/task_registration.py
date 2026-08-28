"""Task registration/property association without persisting residence addresses.

The Tencent column remains the human-readable projection.  This module stores
only the stable property reference and verification metadata needed to decide
whether a newly selected ``待登记`` task may become ``已登记``.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Any

from config import settings
from services.registry_import import normalize_address
from services.task_workflow import TASK_WORKFLOWS


REGISTRATION_TASK_TYPES = (
    "全链条",
    "出租房屋核查",
    "寄递业",
    "疑似返苏",
    "苏州涉警",
    "交通涉警",
)

REGISTRATION_STATES = {
    "awaiting_match",
    "matched_once",
    "review_required",
    "confirmation_pending",
    "confirmed",
    "cancelled",
    "legacy_completed",
}

REVIEW_REASON_LABELS = {
    "missing_property": "缺少拟登记住址",
    "address_mismatch": "居住证登记地址与拟登记住址不一致",
    "address_ambiguous": "同一社区存在多套相同地址房屋",
    "property_inactive": "关联房屋已停用",
    "property_changed": "关联房屋档案已更新，请重新确认",
    "community_conflict": "房屋与任务所属社区不一致",
    "registration_missing": "居住证平台未查询到有效登记",
    "registration_cancelled": "居住证登记已注销",
    "source_changed": "任务来源已变化",
    "source_missing": "任务来源已删除",
    "source_ambiguous": "任务来源不唯一",
    "lookup_failed": "居住证平台查询失败",
    "writeback_pending": "已确认，等待同步腾讯表格",
    "confirmation_enqueue_failed": "自动确认未能进入写回队列，请重新复核",
}


def is_registration_task(parser_type: str) -> bool:
    return parser_type in REGISTRATION_TASK_TYPES


def is_pending_registration(parser_type: str, values: dict[str, Any]) -> bool:
    if not is_registration_task(parser_type):
        return False
    result_field = "核查反馈" if parser_type == "疑似返苏" else "核查结果"
    return str(values.get(result_field) or "").strip() == "待登记"


def address_hmac(value: str) -> str:
    normalized = normalize_address(value)
    if not normalized:
        return ""
    return hmac.new(
        settings.registry_hmac_key.encode("utf-8"),
        f"registration-address:{normalized}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


async def ensure_task_registration_schema(cur) -> None:
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _task_registration_links (
            parser_type VARCHAR(50) NOT NULL,
            row_key CHAR(32) NOT NULL,
            source_id BIGINT DEFAULT NULL,
            source_revision BIGINT UNSIGNED DEFAULT NULL,
            source_row_hash CHAR(64) NOT NULL DEFAULT '',
            identity_hmac CHAR(64) NOT NULL DEFAULT '',
            task_community VARCHAR(200) NOT NULL DEFAULT '',
            property_id BIGINT DEFAULT NULL,
            property_version INT UNSIGNED DEFAULT NULL,
            status VARCHAR(32) NOT NULL DEFAULT 'awaiting_match',
            reason_code VARCHAR(64) NOT NULL DEFAULT '',
            match_count TINYINT UNSIGNED NOT NULL DEFAULT 0,
            last_address_hmac CHAR(64) NOT NULL DEFAULT '',
            last_scan_token CHAR(36) NOT NULL DEFAULT '',
            selected_by BIGINT DEFAULT NULL,
            selected_at DATETIME DEFAULT NULL,
            confirmed_by BIGINT DEFAULT NULL,
            manual_confirmed_at DATETIME DEFAULT NULL,
            confirmed_at DATETIME DEFAULT NULL,
            manual_reason VARCHAR(64) NOT NULL DEFAULT '',
            manual_note VARCHAR(500) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (parser_type,row_key),
            INDEX idx_task_registration_source (source_id),
            INDEX idx_task_registration_property (property_id,status),
            INDEX idx_task_registration_review (status,reason_code)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    )
    for column, definition in (
        ("source_revision", "BIGINT UNSIGNED DEFAULT NULL AFTER source_id"),
        ("source_row_hash", "CHAR(64) NOT NULL DEFAULT '' AFTER source_revision"),
        ("identity_hmac", "CHAR(64) NOT NULL DEFAULT '' AFTER source_row_hash"),
        ("task_community", "VARCHAR(200) NOT NULL DEFAULT '' AFTER identity_hmac"),
        ("manual_confirmed_at", "DATETIME DEFAULT NULL AFTER confirmed_by"),
    ):
        await cur.execute(
            "SHOW COLUMNS FROM _task_registration_links LIKE %s",
            (column,),
        )
        if not await cur.fetchone():
            await cur.execute(
                f"ALTER TABLE _task_registration_links ADD COLUMN {column} {definition}"
            )
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _task_registration_events (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            parser_type VARCHAR(50) NOT NULL,
            row_key CHAR(32) NOT NULL,
            source_id BIGINT DEFAULT NULL,
            property_id BIGINT DEFAULT NULL,
            event_type VARCHAR(50) NOT NULL,
            reason_code VARCHAR(64) NOT NULL DEFAULT '',
            actor_user_id BIGINT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_task_registration_event_task (parser_type,row_key,created_at),
            INDEX idx_task_registration_event_property (property_id,created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    )
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS _task_registration_migrations (
            migration_key VARCHAR(100) PRIMARY KEY,
            completed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
        """
    )
    migration_key = "legacy-fullchain-pending-registration-v1"
    await cur.execute(
        "SELECT migration_key FROM _task_registration_migrations WHERE migration_key=%s",
        (migration_key,),
    )
    if not await cur.fetchone():
        await cur.execute(
            """
            INSERT IGNORE INTO _task_registration_links
                (parser_type,row_key,status,reason_code,selected_at)
            SELECT '全链条',projection.row_key,'legacy_completed','',UTC_TIMESTAMP()
            FROM _online_source_projection AS projection
            WHERE projection.parser_type='全链条'
              AND TRIM(COALESCE(JSON_UNQUOTE(
                    JSON_EXTRACT(projection.values_json,'$."核查结果"')
                  ),''))='待登记'
            """
        )
        await cur.execute(
            "INSERT INTO _task_registration_migrations (migration_key) VALUES (%s)",
            (migration_key,),
        )


async def record_registration_event(
    cur,
    *,
    parser_type: str,
    row_key: str,
    event_type: str,
    source_id: int | None = None,
    property_id: int | None = None,
    reason_code: str = "",
    actor_user_id: int | None = None,
) -> None:
    await cur.execute(
        """
        INSERT INTO _task_registration_events
            (parser_type,row_key,source_id,property_id,event_type,reason_code,actor_user_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            parser_type,
            row_key,
            source_id,
            property_id,
            event_type[:50],
            reason_code[:64],
            actor_user_id,
        ),
    )


async def ensure_missing_registration_review(
    cur,
    *,
    parser_type: str,
    row_key: str,
    values: dict[str, Any],
    source_contexts: list[dict[str, Any]],
    identity_hmac: str,
    task_community: str,
) -> None:
    """Persist a safe review marker for a Tencent-side pending result.

    A row may be changed to ``待登记`` outside the platform.  It must never be
    treated as a historical completed row or silently wait without a property
    link.  Existing active/legacy links are preserved; a previously cancelled
    link is reopened without reusing its old property selection.
    """
    if not is_pending_registration(parser_type, values):
        return
    unique_source = source_contexts[0] if len(source_contexts) == 1 else {}
    reason_code = "missing_property" if len(source_contexts) == 1 else "source_ambiguous"
    await cur.execute(
        """
        INSERT INTO _task_registration_links
            (parser_type,row_key,source_id,source_revision,source_row_hash,
             identity_hmac,task_community,status,reason_code)
        VALUES (%s,%s,%s,%s,%s,%s,%s,'review_required',%s)
        ON DUPLICATE KEY UPDATE
            source_id=IF(status='cancelled',VALUES(source_id),source_id),
            source_revision=IF(status='cancelled',VALUES(source_revision),source_revision),
            source_row_hash=IF(status='cancelled',VALUES(source_row_hash),source_row_hash),
            identity_hmac=IF(status='cancelled',VALUES(identity_hmac),identity_hmac),
            task_community=IF(status='cancelled',VALUES(task_community),task_community),
            property_id=IF(status='cancelled',NULL,property_id),
            property_version=IF(status='cancelled',NULL,property_version),
            status=IF(status='cancelled','review_required',status),
            reason_code=IF(status='cancelled',VALUES(reason_code),reason_code),
            match_count=IF(status='cancelled',0,match_count),
            last_address_hmac=IF(status='cancelled','',last_address_hmac),
            last_scan_token=IF(status='cancelled','',last_scan_token)
        """,
        (
            parser_type,
            row_key,
            unique_source.get("id"),
            unique_source.get("revision"),
            str(unique_source.get("row_hash") or "")[:64],
            identity_hmac[:64],
            task_community[:200],
            reason_code,
        ),
    )


async def registration_links_by_rows(
    cur, parser_type: str, row_keys: list[str]
) -> dict[str, dict[str, Any]]:
    if not is_registration_task(parser_type) or not row_keys:
        return {}
    placeholders = ",".join(["%s"] * len(row_keys))
    registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
    await cur.execute(
        f"""
        SELECT link.row_key,link.source_id,link.property_id,link.property_version,
               link.status,link.reason_code,link.match_count,link.selected_at,
               link.confirmed_at,link.manual_reason,link.last_scan_token,
               link.manual_confirmed_at,
               property.natural_address,property.building,property.room,
               property.normalized_address,property.status,property.current_version,
               property.community_id,property.community_name_snapshot
        FROM _task_registration_links AS link
        LEFT JOIN `{registry}`.registry_properties AS property
          ON property.id=link.property_id
        WHERE link.parser_type=%s AND link.row_key IN ({placeholders})
        """,
        (parser_type, *row_keys),
    )
    result: dict[str, dict[str, Any]] = {}
    for row in await cur.fetchall():
        status = str(row[4] or "awaiting_match")
        reason = str(row[5] or "")
        confirmed_at = row[8] if isinstance(row[8], datetime) else None
        archive_available_at = (
            confirmed_at + timedelta(hours=24)
            if parser_type == "全链条" and status == "confirmed" and confirmed_at
            else None
        )
        result[str(row[0])] = {
            "source_id": int(row[1]) if row[1] is not None else None,
            "property_id": int(row[2]) if row[2] is not None else None,
            "property_version": int(row[3]) if row[3] is not None else None,
            "status": status,
            "reason_code": reason,
            "reason": REVIEW_REASON_LABELS.get(reason, ""),
            "match_count": int(row[6] or 0),
            "selected_at": row[7].isoformat() + "Z" if isinstance(row[7], datetime) else None,
            "confirmed_at": confirmed_at.isoformat() + "Z" if confirmed_at else None,
            "archive_available_at": (
                archive_available_at.isoformat() + "Z" if archive_available_at else None
            ),
            "archive_ready": bool(
                archive_available_at and datetime.utcnow() >= archive_available_at
            ),
            "manual_reason": str(row[9] or ""),
            "last_scan_token": str(row[10] or ""),
            "manual_confirmed_at": row[11].isoformat() + "Z" if isinstance(row[11], datetime) else None,
            "property": None if row[2] is None else {
                "id": int(row[2]),
                "natural_address": str(row[12] or ""),
                "building": str(row[13] or ""),
                "room": str(row[14] or ""),
                "normalized_address": str(row[15] or ""),
                "status": str(row[16] or ""),
                "version": int(row[17] or 0),
                "community_id": int(row[18]) if row[18] is not None else None,
                "community_name": str(row[19] or ""),
            },
        }
    return result


async def validate_registration_property(
    cur,
    *,
    property_id: int,
    expected_version: int,
    task_community: str,
) -> dict[str, Any]:
    registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
    await cur.execute(
        f"""
        SELECT id,community_id,community_name_snapshot,natural_address,building,room,
               normalized_address,status,current_version
        FROM `{registry}`.registry_properties WHERE id=%s
        """,
        (property_id,),
    )
    row = await cur.fetchone()
    if not row:
        raise ValueError("所选房屋档案不存在")
    if str(row[7] or "") != "active":
        raise ValueError("所选房屋档案已停用")
    if int(row[8] or 0) != int(expected_version):
        raise ValueError("房屋档案已更新，请重新搜索并确认")
    await cur.execute(
        """
        SELECT community.id,community.name,alias.alias
        FROM _communities AS community
        LEFT JOIN _community_aliases AS alias ON alias.community_id=community.id
        WHERE community.id=%s AND community.is_active=1
        """,
        (row[1],),
    )
    community_labels = {
        str(value or "").strip()
        for item in await cur.fetchall()
        for value in item[1:]
        if str(value or "").strip()
    }
    if str(task_community or "").strip() not in community_labels:
        raise ValueError("只能关联任务所属社区内的有效房屋")
    canonical_address = "".join(
        str(value or "").strip() for value in (row[3], row[4], row[5])
    ) or str(row[6] or "").strip()
    if not normalize_address(canonical_address):
        raise ValueError("所选房屋缺少可用于比对的规范地址")
    return {
        "id": int(row[0]),
        "community_id": int(row[1]) if row[1] is not None else None,
        "community_name": str(row[2] or ""),
        "address": canonical_address,
        "normalized_address": normalize_address(canonical_address),
        "version": int(row[8]),
    }


async def select_registration_property(
    cur,
    *,
    parser_type: str,
    row_key: str,
    source_id: int,
    property_id: int,
    property_version: int,
    source_revision: int,
    source_row_hash: str,
    identity_hmac: str,
    task_community: str,
    user_id: int | None,
) -> None:
    await cur.execute(
        "SELECT source_id,property_id,property_version,status,source_revision,"
        "source_row_hash,identity_hmac,task_community "
        "FROM _task_registration_links WHERE parser_type=%s AND row_key=%s FOR UPDATE",
        (parser_type, row_key),
    )
    current = await cur.fetchone()
    if current and (
        int(current[0] or 0) == int(source_id)
        and int(current[1] or 0) == int(property_id)
        and int(current[2] or 0) == int(property_version)
        and int(current[4] or 0) == int(source_revision)
        and str(current[5] or "") == source_row_hash[:64]
        and str(current[6] or "") == identity_hmac[:64]
        and str(current[7] or "") == task_community[:200]
        and str(current[3] or "") in {
            "awaiting_match", "matched_once",
        }
    ):
        return
    await cur.execute(
        """
        INSERT INTO _task_registration_links
            (parser_type,row_key,source_id,source_revision,source_row_hash,
             identity_hmac,task_community,property_id,property_version,status,
             reason_code,match_count,last_address_hmac,last_scan_token,
             selected_by,selected_at,confirmed_by,manual_confirmed_at,confirmed_at,manual_reason,manual_note)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'awaiting_match','',0,'','',%s,UTC_TIMESTAMP(),NULL,NULL,NULL,'','')
        ON DUPLICATE KEY UPDATE
            source_id=VALUES(source_id),source_revision=VALUES(source_revision),
            source_row_hash=VALUES(source_row_hash),identity_hmac=VALUES(identity_hmac),
            task_community=VALUES(task_community),property_id=VALUES(property_id),
            property_version=VALUES(property_version),status='awaiting_match',
            reason_code='',match_count=0,last_address_hmac='',last_scan_token='',
            selected_by=VALUES(selected_by),selected_at=UTC_TIMESTAMP(),
            confirmed_by=NULL,manual_confirmed_at=NULL,confirmed_at=NULL,
            manual_reason='',manual_note=''
        """,
        (
            parser_type, row_key, source_id, source_revision,
            source_row_hash[:64], identity_hmac[:64], task_community[:200],
            property_id, property_version, user_id,
        ),
    )
    await record_registration_event(
        cur,
        parser_type=parser_type,
        row_key=row_key,
        source_id=source_id,
        property_id=property_id,
        event_type="property_selected",
        actor_user_id=user_id,
    )


async def migrate_registration_link(
    cur,
    *,
    parser_type: str,
    row_key_before: str,
    row_key_after: str,
    source_id: int,
) -> None:
    if row_key_before == row_key_after:
        return
    await cur.execute(
        "SELECT source_id FROM _task_registration_links "
        "WHERE parser_type=%s AND row_key=%s FOR UPDATE",
        (parser_type, row_key_before),
    )
    row = await cur.fetchone()
    if not row or int(row[0] or 0) != int(source_id):
        raise ValueError("任务主键变化，原房屋关联无法安全迁移")
    await cur.execute(
        "SELECT row_key FROM _task_registration_links "
        "WHERE parser_type=%s AND row_key=%s FOR UPDATE",
        (parser_type, row_key_after),
    )
    if await cur.fetchone():
        raise ValueError("任务主键变化后存在另一条房屋关联")
    await cur.execute(
        "UPDATE _task_registration_links SET row_key=%s "
        "WHERE parser_type=%s AND row_key=%s",
        (row_key_after, parser_type, row_key_before),
    )


async def cancel_registration_link(
    cur,
    *,
    parser_type: str,
    row_key: str,
    source_id: int,
    user_id: int | None,
) -> None:
    await cur.execute(
        "SELECT property_id,status FROM _task_registration_links "
        "WHERE parser_type=%s AND row_key=%s FOR UPDATE",
        (parser_type, row_key),
    )
    row = await cur.fetchone()
    if not row:
        return
    await cur.execute(
        """
        UPDATE _task_registration_links
        SET source_id=%s,status='cancelled',reason_code='',match_count=0,
            last_address_hmac='',last_scan_token=''
        WHERE parser_type=%s AND row_key=%s
        """,
        (source_id, parser_type, row_key),
    )
    await record_registration_event(
        cur,
        parser_type=parser_type,
        row_key=row_key,
        source_id=source_id,
        property_id=int(row[0]) if row[0] is not None else None,
        event_type="registration_cancelled",
        actor_user_id=user_id,
    )


def registration_task_state(
    parser_type: str,
    values: dict[str, Any],
    link_status: str = "",
) -> str | None:
    if link_status in {"legacy_completed", "confirmed"}:
        return "completed"
    # The automatic writer temporarily overlays the Tencent result with
    # 已登记 while its writeback is still pending.  Keep that task in the
    # checked queue until the remote write has been verified.
    if link_status in {
        "awaiting_match", "matched_once", "review_required",
        "confirmation_pending",
    }:
        # During automatic writeback the local projection may temporarily
        # contain 已登记.  The link state remains authoritative until the
        # Tencent write is verified, so keep it in the checked queue.
        return "checked"
    if not is_pending_registration(parser_type, values):
        return None
    return "checked"


def registration_context_reason(link: dict[str, Any]) -> str:
    """Return a safe reason when the source/property context changed."""
    if int(link.get("source_count") or 0) != 1:
        return "source_ambiguous"
    if link.get("current_source_id") is None:
        return "source_missing"
    if int(link.get("source_id") or 0) != int(link.get("current_source_id") or 0):
        return "source_changed"
    if (
        link.get("source_revision") is not None
        and int(link.get("source_revision") or 0)
        != int(link.get("current_source_revision") or 0)
    ):
        return "source_changed"
    if (
        str(link.get("source_row_hash") or "")
        and str(link.get("source_row_hash") or "")
        != str(link.get("current_source_row_hash") or "")
    ):
        return "source_changed"
    if (
        str(link.get("identity_hmac") or "")
        != str(link.get("current_identity_hmac") or "")
    ):
        return "source_changed"
    if (
        str(link.get("task_community") or "").strip()
        != str(link.get("current_community") or "").strip()
    ):
        return "community_conflict"
    if str(link.get("property_status") or "") != "active":
        return "property_inactive"
    if int(link.get("property_version") or 0) != int(link.get("current_version") or 0):
        return "property_changed"
    return ""


async def registration_match_context(cur, parser_type: str, row_key: str) -> dict[str, Any] | None:
    if not is_registration_task(parser_type):
        return None
    registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
    await cur.execute(
        f"""
        SELECT link.source_id,link.source_revision,link.source_row_hash,link.identity_hmac,
               link.property_id,link.property_version,link.status,
               property.normalized_address,property.status,property.current_version,
               property.community_id,property.community_name_snapshot,
               link.task_community,source.id,source.revision,source.row_hash,
               projection.identity_hmac,projection.community,projection.source_count
        FROM _task_registration_links AS link
        LEFT JOIN `{registry}`.registry_properties AS property
          ON property.id=link.property_id
        LEFT JOIN _online_source_rows AS source
          ON source.id=link.source_id AND source.parser_type=link.parser_type
         AND source.row_key=link.row_key
        LEFT JOIN _online_source_projection AS projection
          ON projection.parser_type=link.parser_type AND projection.row_key=link.row_key
        WHERE link.parser_type=%s AND link.row_key=%s
        """,
        (parser_type, row_key),
    )
    row = await cur.fetchone()
    if not row or not row[4]:
        return None
    return {
        "source_id": int(row[0]) if row[0] is not None else None,
        "source_revision": int(row[1]) if row[1] is not None else None,
        "source_row_hash": str(row[2] or ""),
        "identity_hmac": str(row[3] or ""),
        "property_id": int(row[4]),
        "property_version": int(row[5] or 0),
        "status": str(row[6] or ""),
        "normalized_address": str(row[7] or ""),
        "property_status": str(row[8] or ""),
        "current_version": int(row[9] or 0),
        "community_id": int(row[10]) if row[10] is not None else None,
        "community_name": str(row[11] or ""),
        "task_community": str(row[12] or ""),
        "current_source_id": int(row[13]) if row[13] is not None else None,
        "current_source_revision": int(row[14]) if row[14] is not None else None,
        "current_source_row_hash": str(row[15] or ""),
        "current_identity_hmac": str(row[16] or ""),
        "current_community": str(row[17] or ""),
        "source_count": int(row[18] or 0),
    }


async def update_registration_match(
    cur,
    *,
    parser_type: str,
    row_key: str,
    link: dict[str, Any],
    scan_token: str,
    matched: bool,
    reason_code: str = "",
    observed_address_hmac: str = "",
) -> bool:
    """Store one independent scan observation; return whether auto-confirmed."""
    same_scan = str(link.get("last_scan_token") or "") == scan_token
    next_count = int(link.get("match_count") or 0)
    previous_address_hmac = str(link.get("last_address_hmac") or "")
    if not same_scan:
        if not matched:
            next_count = 0
        elif previous_address_hmac and observed_address_hmac != previous_address_hmac:
            # A different address in a later scan is not a consecutive match.
            # Start a fresh first-match observation instead of confirming the
            # task from two different residence addresses.
            next_count = 1
        else:
            next_count += 1
    if not matched:
        next_count = 0
    status = (
        "confirmation_pending" if matched and next_count >= 2
        else "matched_once" if matched
        else "review_required"
    )
    await cur.execute(
        """
        UPDATE _task_registration_links
        SET status=%s,reason_code=%s,match_count=%s,last_address_hmac=%s,
            last_scan_token=%s,confirmed_at=IF(%s='confirmed',COALESCE(confirmed_at,UTC_TIMESTAMP()),NULL),
            updated_at=UTC_TIMESTAMP()
        WHERE parser_type=%s AND row_key=%s AND source_id=%s AND property_id=%s
          AND status IN ('awaiting_match','matched_once','review_required')
        """,
        (
            status,
            reason_code[:64],
            next_count,
            observed_address_hmac[:64],
            scan_token,
            status,
            parser_type,
            row_key,
            link.get("source_id"),
            link.get("property_id"),
        ),
    )
    if cur.rowcount != 1:
        return False
    await record_registration_event(
        cur,
        parser_type=parser_type,
        row_key=row_key,
        source_id=link.get("source_id"),
        property_id=link.get("property_id"),
        event_type="residence_match" if matched else "residence_mismatch",
        reason_code=reason_code,
    )
    return status == "confirmation_pending"


async def mark_registration_confirmation_failed(
    cur,
    *,
    parser_type: str,
    row_key: str,
    source_id: int,
    property_id: int,
    reason_code: str = "confirmation_enqueue_failed",
) -> bool:
    """Atomically return a failed confirmation enqueue to manual review."""
    await cur.execute(
        """
        UPDATE _task_registration_links
        SET status='review_required',reason_code=%s,match_count=0,
            last_address_hmac='',last_scan_token='',confirmed_at=NULL,
            updated_at=UTC_TIMESTAMP()
        WHERE parser_type=%s AND row_key=%s AND source_id=%s AND property_id=%s
          AND status IN ('awaiting_match','matched_once','review_required','confirmation_pending')
        """,
        (reason_code[:64], parser_type, row_key, source_id, property_id),
    )
    if cur.rowcount != 1:
        return False
    await record_registration_event(
        cur,
        parser_type=parser_type,
        row_key=row_key,
        source_id=source_id,
        property_id=property_id,
        event_type="registration_confirmation_enqueue_failed",
        reason_code=reason_code,
    )
    return True


async def refresh_registration_source_context_after_writeback(
    cur,
    *,
    parser_type: str,
    source_id: int,
    previous_revision: int,
    previous_row_hash: str,
    current_revision: int,
    current_row_hash: str,
) -> bool:
    """Advance a link only across the exact source writeback it expected."""
    await cur.execute(
        """
        UPDATE _task_registration_links
        SET source_revision=%s,source_row_hash=%s,updated_at=UTC_TIMESTAMP()
        WHERE parser_type=%s AND source_id=%s
          AND source_revision=%s AND source_row_hash=%s
          AND status IN ('awaiting_match','matched_once','review_required','confirmation_pending')
        """,
        (
            current_revision,
            current_row_hash,
            parser_type,
            source_id,
            previous_revision,
            previous_row_hash,
        ),
    )
    return cur.rowcount == 1


async def enqueue_automatic_registration_confirmation(
    conn,
    *,
    parser_type: str,
    row_key: str,
    user_id: int = 0,
) -> int:
    """Queue the final 已登记 writeback without exposing a privileged edit API."""
    from services.online_local_writeback import (
        enqueue_local_changes,
        load_local_changes,
        overlay_local_values,
    )
    from services.online_source import json_value, stable_json

    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT source.id, source.revision, source.values_json, source.row_hash,
                   source.spreadsheet_id, source.sheet_id, source.physical_row,
                   source.row_key, spreadsheet.parser_type, spreadsheet.file_id,
                   spreadsheet.data_sheet_id, spreadsheet.header_row,
                   projection.identity_hmac, projection.community,
                   projection.source_count
            FROM _online_source_rows AS source
            JOIN _config_spreadsheets AS spreadsheet
              ON spreadsheet.id=source.spreadsheet_id
            JOIN _online_source_projection AS projection
              ON projection.parser_type=source.parser_type
             AND projection.row_key=source.row_key
            WHERE source.parser_type=%s AND source.row_key=%s
            FOR UPDATE
            """,
            (parser_type, row_key),
        )
        rows = await cur.fetchall()
        if len(rows) != 1:
            raise ValueError("任务来源不唯一或已不存在")
        row = rows[0]
        source = {
            "id": int(row[0]), "revision": int(row[1]),
            "values": json_value(row[2], {}), "row_hash": str(row[3] or ""),
            "spreadsheet_id": int(row[4]), "sheet_id": str(row[5]),
            "physical_row": int(row[6]), "row_key": str(row[7]),
            "spreadsheet": {
                "parser_type": str(row[8]), "file_id": str(row[9]),
                "data_sheet_id": str(row[10]), "header_row": int(row[11] or 1),
            },
        }
        await cur.execute(
            "SELECT source_id,source_revision,source_row_hash,property_id,property_version,"
            "identity_hmac,task_community,status "
            "FROM _task_registration_links WHERE parser_type=%s AND row_key=%s FOR UPDATE",
            (parser_type, row_key),
        )
        link = await cur.fetchone()
        if not link or str(link[7] or "") != "confirmation_pending":
            raise ValueError("登记确认状态已变化")
        if int(link[0] or 0) != source["id"] or str(link[5] or "") != str(row[12] or ""):
            raise ValueError("任务来源或核查对象已变化")
        if link[1] is not None and int(link[1]) != source["revision"]:
            raise ValueError("任务来源版本已变化")
        if str(link[2] or "") and str(link[2]) != source["row_hash"]:
            raise ValueError("任务来源内容已变化")
        if str(link[6] or "").strip() != str(row[13] or "").strip():
            raise ValueError("任务所属社区已变化")
        registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
        await cur.execute(
            f"SELECT status,current_version FROM `{registry}`.registry_properties WHERE id=%s",
            (int(link[3]),),
        )
        property_row = await cur.fetchone()
        if not property_row or str(property_row[0] or "") != "active" or int(property_row[1] or 0) != int(link[4] or 0):
            raise ValueError("关联房屋已变化")
        grouped = await load_local_changes(cur, [source["id"]])
        values = overlay_local_values(source["values"], grouped.get(source["id"], []))
        workflow = TASK_WORKFLOWS.get(parser_type)
        if not workflow or values.get(workflow.result_field, "").strip() != "待登记":
            raise ValueError("任务结果已变化，不能自动确认")
        await cur.execute(
            """
            INSERT INTO _online_writeback_audit
                (user_id,username,action,parser_type,spreadsheet_id,sheet_id,
                 physical_row,column_name,row_key_before,row_key_after,
                 before_values,after_values,sync_status)
            VALUES (%s,'system','auto_registration',%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending')
            """,
            (user_id, parser_type, source["spreadsheet_id"], source["sheet_id"],
             source["physical_row"], workflow.result_field, source["row_key"],
             source["row_key"], stable_json(values),
             stable_json({**values, workflow.result_field: "已登记"})),
        )
        audit_id = int(cur.lastrowid)
        await enqueue_local_changes(
            conn,
            source=source,
            changes={workflow.result_field: "已登记"},
            user={"id": user_id, "username": "system"},
            audit_id=audit_id,
        )
    return audit_id


async def finalize_registration_writeback(
    cur,
    *,
    parser_type: str,
    row_key: str,
    source_id: int,
    succeeded: bool,
    reason_code: str = "",
) -> None:
    """Move confirmation_pending to the terminal state after Tencent verification."""
    await cur.execute(
        "SELECT link.property_id,link.status,link.source_revision,link.source_row_hash,"
        "source.revision,source.row_hash FROM _task_registration_links link "
        "LEFT JOIN _online_source_rows source ON source.id=link.source_id "
        "AND source.parser_type=link.parser_type AND source.row_key=link.row_key "
        "WHERE link.parser_type=%s AND link.row_key=%s AND link.source_id=%s FOR UPDATE",
        (parser_type, row_key, source_id),
    )
    row = await cur.fetchone()
    if not row or str(row[1] or "") not in {"confirmation_pending", "confirmed"}:
        return
    source_context_matches = bool(
        row[4] is not None
        and int(row[2] or 0) == int(row[4] or 0)
        and str(row[3] or "") == str(row[5] or "")
    )
    status = "confirmed" if succeeded and source_context_matches else "review_required"
    reason = (
        "" if status == "confirmed"
        else "source_changed" if succeeded
        else (reason_code or "writeback_pending")
    )
    await cur.execute(
        "UPDATE _task_registration_links SET status=%s,reason_code=%s,"
        "confirmed_at=IF(%s='confirmed',COALESCE(confirmed_at,UTC_TIMESTAMP()),NULL) "
        "WHERE parser_type=%s AND row_key=%s AND source_id=%s",
        (status, reason[:64], status, parser_type, row_key, source_id),
    )
    await record_registration_event(
        cur,
        parser_type=parser_type,
        row_key=row_key,
        source_id=source_id,
        property_id=int(row[0]) if row[0] is not None else None,
        event_type=(
            "registration_confirmed"
            if status == "confirmed"
            else "registration_writeback_failed"
        ),
        reason_code=reason,
    )
