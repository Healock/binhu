"""按当前账号的数据范围裁剪业务响应。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from services.permissions import (
    ONLINE_SUMMARY_VIEW,
    permitted_communities,
    permitted_community,
)
from database import db_manager


def community_scope(
    user: dict[str, Any],
    permission: str = ONLINE_SUMMARY_VIEW,
) -> str | None:
    """None 表示全所；空字符串表示尚未分配社区、不能看业务数据。"""
    return permitted_community(user, permission)


def community_scopes(
    user: dict[str, Any],
    permission: str = ONLINE_SUMMARY_VIEW,
) -> list[str] | None:
    """None 表示全所；空列表表示没有社区业务范围。"""
    return permitted_communities(user, permission)


def filter_report_payload(
    payload: dict,
    user: dict,
    allowed_communities: list[str] | None = None,
    allowed_inspectors: list[str] | None = None,
) -> dict:
    """裁剪人员、社区和扁平汇总表；总计由展示层重新计算。"""
    scopes = community_scopes(user, ONLINE_SUMMARY_VIEW)
    if not payload.get("exists"):
        return payload

    explicit_scope = allowed_communities is not None
    if scopes is None and not explicit_scope and allowed_inspectors is None:
        return payload

    result = deepcopy(payload)
    selected = allowed_communities if explicit_scope else scopes
    accepted = set(selected or [])
    community_filter_active = explicit_scope or scopes is not None
    if community_filter_active:
        for section_name in ("inspector", "community"):
            section = result.get(section_name)
            if not isinstance(section, dict):
                continue
            section["data"] = [
                row
                for row in section.get("data") or []
                if str(row.get("社区") or "").strip() in accepted
            ] if selected else []
            section.pop("summary", None)

    if allowed_inspectors is not None:
        accepted_inspectors = {
            str(name).strip().casefold()
            for name in allowed_inspectors
            if str(name).strip()
        }
        inspector = result.get("inspector")
        if isinstance(inspector, dict):
            inspector["data"] = [
                row
                for row in inspector.get("data") or []
                if str(row.get("姓名") or "").strip().casefold()
                in accepted_inspectors
            ]
        community = result.get("community")
        if isinstance(community, dict):
            # 社区聚合无法从已聚合行安全反推个人值；个人职责模式不返回整社区行。
            community["data"] = []
            community.pop("summary", None)
        if isinstance(inspector, dict):
            inspector.pop("summary", None)
        if isinstance(result.get("data"), list):
            result["data"] = []

    if community_filter_active and isinstance(result.get("data"), list):
        result["data"] = [
            row
            for row in result["data"]
            if str(row.get("社区") or "").strip() in accepted
        ] if selected else []
        result.pop("summary", None)

    if (community_filter_active and not selected) or allowed_inspectors == []:
        result["scope_message"] = "当前账号尚未分配社区部门，暂无业务数据"
    return result


async def community_names_for_scope(conn, scope: str) -> list[str]:
    """返回正式社区名及其别名，供原始数据查询先归一再过滤。"""
    if not scope:
        return []
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT c.name
            FROM _communities AS c
            WHERE c.name=%s
            UNION
            SELECT a.alias
            FROM _community_aliases AS a
            JOIN _communities AS c ON c.id=a.community_id
            WHERE c.name=%s
            """,
            (scope, scope),
        )
        names = [str(row[0]).strip() for row in await cur.fetchall()]
    return names or [scope]


async def community_names_for_scopes(conn, scopes: list[str]) -> list[str]:
    names: list[str] = []
    for scope in scopes:
        for name in await community_names_for_scope(conn, scope):
            if name not in names:
                names.append(name)
    return names


async def allowed_community_names(
    user: dict,
    permission: str = ONLINE_SUMMARY_VIEW,
) -> list[str] | None:
    scopes = community_scopes(user, permission)
    if scopes is None:
        return None
    if not scopes:
        return []
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        return await community_names_for_scopes(conn, scopes)
    finally:
        pool.release(conn)
