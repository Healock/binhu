"""Reconcile registry import issues after community aliases change."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from database import db_manager
from services.registry_import import (
    ISSUE_HOUSEHOLD_COMMUNITY_UNRESOLVED,
    normalize_community,
)

logger = logging.getLogger(__name__)


def community_labels(formal_name: str, aliases: Iterable[str]) -> set[str]:
    """Return exact current labels that belong to one formal community."""
    return {
        normalize_community(value)
        for value in (formal_name, *aliases)
        if normalize_community(value)
    }


def issue_matches_community(payload_json: Any, labels: set[str]) -> bool:
    """Check an unresolved issue using only its stored source community."""
    if not labels:
        return False
    if isinstance(payload_json, str):
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError):
            return False
    else:
        payload = payload_json
    if not isinstance(payload, dict):
        return False
    source_community = normalize_community(
        payload.get("community")
        or payload.get("community_name")
        or payload.get("社区名称")
        or payload.get("sssq")
    )
    return bool(source_community and source_community in labels)


async def reconcile_household_community_issues(
    *,
    formal_name: str,
    aliases: Iterable[str],
    reviewed_by: int | None,
) -> int:
    """Resolve pending community issues that now match one current label set.

    The registry database is separate from the platform database where aliases
    are edited, so this runs after the platform transaction commits. It is
    idempotent and never changes unresolved, duplicate, or certificate issues.
    """
    labels = community_labels(formal_name, aliases)
    if not labels:
        return 0
    pool = db_manager.get_pool("registry")
    conn = await pool.acquire()
    resolved = 0
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, payload_json FROM registry_import_issues "
                "WHERE status='pending' AND source_type='household' "
                "AND issue_type=%s",
                (ISSUE_HOUSEHOLD_COMMUNITY_UNRESOLVED,),
            )
            candidates = await cur.fetchall()
            for issue_id, payload_json in candidates:
                if not issue_matches_community(payload_json, labels):
                    continue
                await cur.execute(
                    "UPDATE registry_import_issues SET status='resolved', "
                    "review_note=%s, reviewed_by=%s, reviewed_at=UTC_TIMESTAMP() "
                    "WHERE id=%s AND status='pending'",
                    (
                        f"社区别名重算后自动归入：{formal_name}",
                        reviewed_by,
                        issue_id,
                    ),
                )
                resolved += cur.rowcount
        await conn.commit()
        return resolved
    except Exception:
        await conn.rollback()
        raise
    finally:
        pool.release(conn)


async def reconcile_household_community_issues_safely(**kwargs) -> int:
    """Best-effort wrapper used after a committed community update."""
    try:
        return await reconcile_household_community_issues(**kwargs)
    except Exception:
        logger.exception("registry community issue reconciliation failed")
        return -1
