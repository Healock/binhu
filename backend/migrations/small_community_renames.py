"""归并已确认的小区改名关系并保留历史名称。

生产环境固定按 ``measure -> migrate --apply -> verify`` 执行。写入前必须
备份 OnlineData 和 RegistryData。本工具在同一个 MySQL 事务内更新两个库，
保留人工确认状态、确认人和确认时间；历史人工反馈事件保持不可变。

本工具只完成小区身份归并。执行后还必须依次运行房屋匹配和在线任务匹配的
``measure -> migrate --apply -> verify``，使所有非人工确认的候选使用 rule-v3
重新计算。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
from typing import Any

import aiomysql

from config import settings
from migrations.domain_migration import quote_identifier
from services.address_matching import MATCHER_VERSION
from services.police_dispatch import normalize_lookup
from services.small_community_renames import (
    SmallCommunityRenamePlan,
    build_rename_plans,
    rewrite_candidate_payload,
)


def _schema(name: str) -> str:
    return quote_identifier(name)


def _table(schema: str, name: str) -> str:
    return f"{_schema(schema)}.{quote_identifier(name)}"


async def open_connection():
    return await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_ONLINE_DATA_DB,
        autocommit=False,
        charset="utf8mb4",
    )


async def close_connection(conn) -> None:
    conn.close()
    wait_closed = getattr(conn, "wait_closed", None)
    if wait_closed:
        result = wait_closed()
        if inspect.isawaitable(result):
            await result


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, type(fallback)) else fallback
        except (TypeError, ValueError, json.JSONDecodeError):
            return fallback
    return fallback


async def _load_entries(cur, *, lock: bool = False) -> list[dict[str, Any]]:
    suffix = " FOR UPDATE" if lock else ""
    registry = _table(settings.MYSQL_REGISTRY_DB, "_police_address_entries")
    platform_communities = _table(settings.MYSQL_PLATFORM_DB, "_communities")
    await cur.execute(
        "SELECT entry.id,entry.name,entry.detail_address,entry.community_id,"
        "community.name,entry.aliases_json,entry.enabled "
        f"FROM {registry} AS entry "
        f"LEFT JOIN {platform_communities} AS community ON community.id=entry.community_id "
        f"ORDER BY entry.id{suffix}"
    )
    return [
        {
            "id": int(row[0]),
            "name": str(row[1] or ""),
            "detail_address": str(row[2] or ""),
            "community_id": int(row[3]) if row[3] is not None else None,
            "community_name": str(row[4] or ""),
            "aliases": _json(row[5], []),
            "enabled": bool(row[6]),
        }
        for row in await cur.fetchall()
    ]


async def _count(cur, sql: str, params: tuple[Any, ...]) -> int:
    await cur.execute(sql, params)
    row = await cur.fetchone()
    return int((row or [0])[0] or 0)


async def _reference_counts(cur, plan: SmallCommunityRenamePlan) -> dict[str, int]:
    source_ids = tuple(int(row["id"]) for row in plan.sources)
    if not source_ids:
        return {
            "online_suggested": 0,
            "online_confirmed": 0,
            "online_projection": 0,
            "feedback_memory": 0,
            "registry_links": 0,
            "registry_confirmed": 0,
        }
    placeholders = ",".join(["%s"] * len(source_ids))
    registry = _schema(settings.MYSQL_REGISTRY_DB)
    return {
        "online_suggested": await _count(
            cur,
            f"SELECT COUNT(*) FROM _online_task_address_matches WHERE suggested_entry_id IN ({placeholders})",
            source_ids,
        ),
        "online_confirmed": await _count(
            cur,
            f"SELECT COUNT(*) FROM _online_task_address_matches WHERE confirmed_entry_id IN ({placeholders})",
            source_ids,
        ),
        "online_projection": await _count(
            cur,
            f"SELECT COUNT(*) FROM _online_source_projection WHERE small_community_id IN ({placeholders})",
            source_ids,
        ),
        "feedback_memory": await _count(
            cur,
            f"SELECT COUNT(*) FROM _online_address_match_feedback WHERE confirmed_entry_id IN ({placeholders})",
            source_ids,
        ),
        "registry_links": await _count(
            cur,
            f"SELECT COUNT(*) FROM {registry}.registry_property_small_community_links "
            f"WHERE small_community_id IN ({placeholders})",
            source_ids,
        ),
        "registry_confirmed": await _count(
            cur,
            f"SELECT COUNT(*) FROM {registry}.registry_property_small_community_links "
            f"WHERE small_community_id IN ({placeholders}) AND match_status='confirmed'",
            source_ids,
        ),
    }


def _plan_summary(plan: SmallCommunityRenamePlan) -> dict[str, Any]:
    return {
        "rule": plan.rule.key,
        "canonical_name": plan.rule.canonical_name,
        "target_id": int(plan.target["id"]),
        "source_ids": [int(row["id"]) for row in plan.sources],
        "community_id": plan.target.get("community_id"),
        "target_enabled": bool(plan.target.get("enabled")),
        "source_enabled": sum(1 for row in plan.sources if row.get("enabled")),
        "alias_count": len(plan.aliases),
        "inherits_address": bool(
            not str(plan.target.get("detail_address") or "").strip()
            and plan.detail_address
        ),
    }


async def inspect_state(cur, command: str, *, lock: bool = False) -> dict[str, Any]:
    entries = await _load_entries(cur, lock=lock)
    plans, issues = build_rename_plans(entries)
    summaries = []
    for plan in plans:
        summary = _plan_summary(plan)
        summary["references"] = await _reference_counts(cur, plan)
        target_aliases = {
            normalize_lookup(value) for value in (plan.target.get("aliases") or [])
            if normalize_lookup(value)
        }
        summary["canonical_name_applied"] = (
            normalize_lookup(plan.target.get("name"))
            == normalize_lookup(plan.rule.canonical_name)
        )
        summary["aliases_applied"] = all(
            normalize_lookup(value) in target_aliases for value in plan.aliases
        )
        summary["sources_disabled"] = all(not row.get("enabled") for row in plan.sources)
        summary["source_reference_count"] = sum(summary["references"].values())
        summaries.append(summary)
    return {
        "command": command,
        "matcher_version": MATCHER_VERSION,
        "rules": summaries,
        "issues": issues,
        "consistent": not issues and all(
            item["canonical_name_applied"]
            and item["aliases_applied"]
            and item["sources_disabled"]
            and item["source_reference_count"] == 0
            for item in summaries
        ),
    }


async def _rewrite_json_rows(
    cur,
    plan: SmallCommunityRenamePlan,
    *,
    table: str,
    key_columns: tuple[str, ...],
    json_column: str,
    where_columns: tuple[str, ...],
    fallback: Any,
) -> int:
    affected_ids = tuple({int(plan.target["id"]), *(int(row["id"]) for row in plan.sources)})
    placeholders = ",".join(["%s"] * len(affected_ids))
    selected = ",".join((*key_columns, json_column))
    where_clause = " OR ".join(
        f"{column} IN ({placeholders})" for column in where_columns
    )
    await cur.execute(
        f"SELECT {selected} FROM {table} WHERE {where_clause}",
        affected_ids * len(where_columns),
    )
    updates = []
    for row in await cur.fetchall():
        raw_payload = _json(row[-1], fallback)
        rewritten = rewrite_candidate_payload(
            raw_payload,
            affected_ids=affected_ids,
            target_id=int(plan.target["id"]),
            canonical_name=plan.rule.canonical_name,
            community_id=int(plan.target["community_id"]),
            community_name=str(plan.target.get("community_name") or ""),
        )
        if rewritten != raw_payload:
            updates.append((json.dumps(rewritten, ensure_ascii=False), *row[:-1]))
    if updates:
        predicate = " AND ".join(f"{column}=%s" for column in key_columns)
        await cur.executemany(
            f"UPDATE {table} SET {json_column}=%s WHERE {predicate}",
            updates,
        )
    return len(updates)


async def _apply_plan(cur, plan: SmallCommunityRenamePlan) -> dict[str, Any]:
    target_id = int(plan.target["id"])
    community_id = int(plan.target["community_id"])
    community_name = str(plan.target.get("community_name") or "")
    source_ids = tuple(int(row["id"]) for row in plan.sources)
    affected_ids = tuple({target_id, *source_ids})
    address_table = _table(settings.MYSQL_REGISTRY_DB, "_police_address_entries")
    target_params = (
        plan.rule.canonical_name,
        normalize_lookup(plan.rule.canonical_name),
        plan.detail_address,
        json.dumps(list(plan.aliases), ensure_ascii=False),
        target_id,
    )
    await cur.execute(
        f"UPDATE {address_table} SET name=%s,normalized_name=%s,"
        "detail_address=%s,aliases_json=%s,enabled=1 WHERE id=%s",
        target_params,
    )

    if source_ids:
        placeholders = ",".join(["%s"] * len(source_ids))
        await cur.execute(
            f"UPDATE {address_table} SET enabled=0 WHERE id IN ({placeholders})",
            source_ids,
        )
        await cur.execute(
            f"UPDATE _online_task_address_matches SET "
            f"suggested_entry_id=IF(suggested_entry_id IN ({placeholders}),%s,suggested_entry_id),"
            f"confirmed_entry_id=IF(confirmed_entry_id IN ({placeholders}),%s,confirmed_entry_id) "
            f"WHERE suggested_entry_id IN ({placeholders}) OR confirmed_entry_id IN ({placeholders})",
            (*source_ids, target_id, *source_ids, target_id, *source_ids, *source_ids),
        )
        await cur.execute(
            f"UPDATE _online_address_match_feedback SET confirmed_entry_id=%s,"
            f"matcher_version=%s WHERE confirmed_entry_id IN ({placeholders})",
            (target_id, MATCHER_VERSION, *source_ids),
        )

    placeholders = ",".join(["%s"] * len(affected_ids))
    await cur.execute(
        f"UPDATE _online_source_projection SET small_community_id=%s,"
        f"small_community_name=%s WHERE small_community_id IN ({placeholders})",
        (target_id, plan.rule.canonical_name, *affected_ids),
    )
    registry = _schema(settings.MYSQL_REGISTRY_DB)
    await cur.execute(
        f"UPDATE {registry}.registry_property_small_community_links SET "
        f"small_community_id=%s,small_community_name=%s,community_id=%s,"
        f"community_name_snapshot=%s WHERE small_community_id IN ({placeholders})",
        (target_id, plan.rule.canonical_name, community_id, community_name, *affected_ids),
    )

    online_json_updates = await _rewrite_json_rows(
        cur,
        plan,
        table="_online_task_address_matches",
        key_columns=("parser_type", "row_key"),
        json_column="candidates_json",
        where_columns=("suggested_entry_id", "confirmed_entry_id"),
        fallback=[],
    )
    projection_json_updates = await _rewrite_json_rows(
        cur,
        plan,
        table="_online_source_projection",
        key_columns=("parser_type", "row_key"),
        json_column="address_match_candidates",
        where_columns=("small_community_id",),
        fallback=[],
    )
    registry_json_updates = await _rewrite_json_rows(
        cur,
        plan,
        table=f"{registry}.registry_property_small_community_links",
        key_columns=("property_id",),
        json_column="match_evidence",
        where_columns=("small_community_id",),
        fallback={},
    )
    return {
        **_plan_summary(plan),
        "rewritten_candidate_payloads": (
            online_json_updates + projection_json_updates + registry_json_updates
        ),
    }


async def measure(command: str = "measure") -> dict[str, Any]:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            result = await inspect_state(cur, command)
        await conn.rollback()
        return result
    finally:
        await close_connection(conn)


async def migrate() -> dict[str, Any]:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            before = await inspect_state(cur, "before", lock=True)
            if before["issues"]:
                raise RuntimeError("小区改名关系存在歧义，已停止写入")
            entries = await _load_entries(cur, lock=False)
            plans, issues = build_rename_plans(entries)
            if issues:
                raise RuntimeError("小区改名关系发生变化，已停止写入")
            applied = [await _apply_plan(cur, plan) for plan in plans]
            after = await inspect_state(cur, "after")
        await conn.commit()
        return {"command": "migrate", "before": before, "applied": applied, "after": after}
    except Exception:
        await conn.rollback()
        raise
    finally:
        await close_connection(conn)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure", help="只读统计已确认的小区改名关系和旧引用")
    migrate_parser = sub.add_parser("migrate", help="预览或执行小区身份归并")
    migrate_parser.add_argument("--apply", action="store_true", help="明确授权跨 OnlineData/RegistryData 写入")
    sub.add_parser("verify", help="只读核验旧引用已清零且别名完整")
    return parser


async def main_async(args: argparse.Namespace) -> None:
    if args.command == "measure":
        result = await measure()
    elif args.command == "migrate" and args.apply:
        result = await migrate()
    elif args.command == "migrate":
        result = await measure("migrate-preview")
    else:
        result = await measure("verify")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
