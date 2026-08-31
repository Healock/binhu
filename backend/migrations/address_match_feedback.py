"""任务小区匹配规则重算与人工反馈记忆核验工具。

生产环境固定按 ``measure -> migrate --apply -> verify`` 执行。写入前备份
OnlineData；工具只重算本地任务投影，不修改任务原始地址、原始社区或人工确认。
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json

import aiomysql

from config import settings
from services.address_match_feedback import ACTIVE, CONFLICT
from services.address_matching import MATCHER_VERSION, candidate_group_key
from services.online_source import rebuild_projection
from services.task_workflow import TASK_WORKFLOWS


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


def _candidate_group_count(raw_candidates) -> int:
    if isinstance(raw_candidates, str):
        try:
            raw_candidates = json.loads(raw_candidates)
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_candidates = []
    if not isinstance(raw_candidates, list):
        return 0
    return len({
        candidate_group_key(candidate)
        for candidate in raw_candidates
        if isinstance(candidate, dict)
    })


async def summarize(cur, command: str) -> dict:
    await cur.execute(
        "SELECT parser_type,match_status,candidates_json,matcher_version "
        "FROM _online_task_address_matches"
    )
    rows = await cur.fetchall()
    ambiguous = [row for row in rows if str(row[1] or "") == "ambiguous"]
    unique_ambiguous = sum(
        1 for row in ambiguous if _candidate_group_count(row[2]) == 1
    )
    true_multi = sum(
        1 for row in ambiguous if _candidate_group_count(row[2]) > 1
    )
    by_parser_type: dict[str, dict[str, int]] = {}
    for parser_type, status, _, _ in rows:
        bucket = by_parser_type.setdefault(str(parser_type), {})
        label = str(status or "unmatched")
        bucket[label] = bucket.get(label, 0) + 1
    await cur.execute(
        "SELECT status,COUNT(*) FROM _online_address_match_feedback GROUP BY status"
    )
    feedback = {str(status): int(count) for status, count in await cur.fetchall()}
    return {
        "command": command,
        "total_matches": len(rows),
        "ambiguous_total": len(ambiguous),
        "ambiguous_single_logical_candidate": unique_ambiguous,
        "ambiguous_true_multi_candidate": true_multi,
        "ambiguous_without_candidate": len(ambiguous) - unique_ambiguous - true_multi,
        "outdated_matcher_rows": sum(
            1
            for _, status, _, version in rows
            if str(status or "") != "confirmed"
            and str(version or "") != MATCHER_VERSION
        ),
        "feedback_active": feedback.get(ACTIVE, 0),
        "feedback_conflict": feedback.get(CONFLICT, 0),
        "by_parser_type": by_parser_type,
    }


async def measure(command: str = "measure") -> dict:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            result = await summarize(cur, command)
        await conn.rollback()
        return result
    finally:
        await close_connection(conn)


async def migrate() -> dict:
    conn = await open_connection()
    try:
        async with conn.cursor() as cur:
            before = await summarize(cur, "before")
            processed: dict[str, str] = {}
            for parser_type in TASK_WORKFLOWS:
                await rebuild_projection(cur, parser_type, reconcile_graph=False)
                processed[parser_type] = "rebuilt"
            after = await summarize(cur, "after")
        await conn.commit()
        return {
            "command": "migrate",
            "processed": processed,
            "before": before,
            "after": after,
        }
    except Exception:
        await conn.rollback()
        raise
    finally:
        await close_connection(conn)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure", help="只读统计现有多候选和可自动匹配数量")
    migrate_parser = sub.add_parser("migrate", help="预览或执行本地匹配重算")
    migrate_parser.add_argument("--apply", action="store_true", help="明确授权写入 OnlineData")
    sub.add_parser("verify", help="只读核验重算和反馈记忆状态")
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
