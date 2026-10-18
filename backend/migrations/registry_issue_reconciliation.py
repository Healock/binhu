"""Recompute pending household community issues after alias maintenance.

Production order is ``measure -> migrate --apply -> verify``. The migration
only resolves an issue when its stored source community uniquely matches one
currently active formal community or alias; it never deletes issue history.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json

import aiomysql

from config import settings
from services.registry_issue_reconciliation import (
    _reconcile_source_row,
    unique_community_matches,
)
from services.registry_import import ISSUE_HOUSEHOLD_COMMUNITY_UNRESOLVED, normalize_community
from services.registry_property_canonical import load_property_identity_graph


async def close_connection(conn) -> None:
    conn.close()
    wait_closed = getattr(conn, "wait_closed", None)
    if wait_closed is not None:
        result = wait_closed()
        if inspect.isawaitable(result):
            await result


async def open_connection(database: str):
    return await aiomysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        db=database,
        autocommit=False,
        charset="utf8mb4",
    )


async def load_label_map(cur) -> dict[int, tuple[str, set[str]]]:
    await cur.execute("SELECT id, name FROM _communities WHERE is_active=1")
    communities = {
        int(row[0]): (str(row[1] or "").strip(), {normalize_community(row[1])})
        for row in await cur.fetchall()
    }
    await cur.execute("SELECT community_id, alias FROM _community_aliases")
    for community_id, alias in await cur.fetchall():
        item = communities.get(int(community_id))
        normalized = normalize_community(alias)
        if item and normalized:
            item[1].add(normalized)
    return communities


async def run_reconciliation(*, apply: bool) -> dict:
    # Organization tables follow the same domain switch as runtime SQL
    # routing.  During an incomplete platform-domain migration they remain in
    # OnlineData, while split production uses PlatformData.
    platform_database = (
        settings.MYSQL_PLATFORM_DB
        if settings.PLATFORM_DOMAIN_ACTIVE
        else settings.MYSQL_ONLINE_DATA_DB
    )
    platform_conn = await open_connection(platform_database)
    registry_conn = await open_connection(settings.MYSQL_REGISTRY_DB)
    try:
        async with platform_conn.cursor() as platform_cur:
            labels_by_community = await load_label_map(platform_cur)
        await platform_conn.rollback()
        async with registry_conn.cursor() as registry_cur:
            identity_graph = await load_property_identity_graph(registry_cur)
            await registry_cur.execute(
                "SELECT id, batch_id, source_ref, payload_json FROM registry_import_issues "
                "WHERE status='pending' AND source_type='household' AND issue_type=%s",
                (ISSUE_HOUSEHOLD_COMMUNITY_UNRESOLVED,),
            )
            rows = await registry_cur.fetchall()
            matches = []
            linked_by_batch: dict[int, int] = {}
            for issue_id, batch_id, source_ref, payload_json in rows:
                match = unique_community_matches(payload_json, labels_by_community)
                if not match:
                    continue
                await registry_cur.execute(
                    "SELECT id, entity_id, payload_json FROM registry_source_records "
                    "WHERE batch_id=%s AND source_ref=%s AND entity_type='household_property' "
                    "ORDER BY id LIMIT 1 FOR UPDATE",
                    (batch_id, source_ref),
                )
                source = await registry_cur.fetchone()
                if not source:
                    continue
                if source[1] is None:
                    linked = await _reconcile_source_row(
                        registry_cur,
                        batch_id=int(batch_id),
                        source_ref=str(source_ref),
                        payload=json.loads(source[2]) if isinstance(source[2], str) else dict(source[2]),
                        community_id=match[0],
                        community_name=match[1],
                        reviewer_id=None,
                        apply=apply,
                        identity_graph=identity_graph,
                    )
                    if not linked:
                        continue
                    linked_by_batch[int(batch_id)] = linked_by_batch.get(int(batch_id), 0) + 1
                matches.append((int(issue_id), match[1]))
            if apply:
                for issue_id, formal_name in matches:
                    await registry_cur.execute(
                        "UPDATE registry_import_issues SET status='resolved', "
                        "review_note=%s, reviewed_by=NULL, reviewed_at=UTC_TIMESTAMP() "
                        "WHERE id=%s AND status='pending'",
                        (f"社区别名重算后自动归入：{formal_name}", issue_id),
                    )
                for batch_id, linked_count in linked_by_batch.items():
                    await registry_cur.execute(
                        "SELECT COUNT(*) FROM registry_import_issues WHERE batch_id=%s AND status='pending'",
                        (batch_id,),
                    )
                    pending = int((await registry_cur.fetchone())[0])
                    await registry_cur.execute(
                        "UPDATE registry_source_batches SET status=%s, imported_count=imported_count+%s "
                        "WHERE id=%s AND source_type='household'",
                        ("partially_imported" if pending else "imported", linked_count, batch_id),
                    )
                await registry_conn.commit()
            else:
                await registry_conn.rollback()
        return {
            "command": "migrate" if apply else "measure",
            "platform_database": platform_database,
            "registry_database": settings.MYSQL_REGISTRY_DB,
            "pending_before": len(rows),
            "unique_matches": len(matches),
            "unresolved_after": len(rows) - len(matches),
            "applied": len(matches) if apply else 0,
        }
    except Exception:
        await registry_conn.rollback()
        raise
    finally:
        await close_connection(registry_conn)
        await close_connection(platform_conn)


async def run_verify() -> dict:
    result = await run_reconciliation(apply=False)
    return {"command": "verify", **result}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("measure")
    migrate = sub.add_parser("migrate")
    migrate.add_argument("--apply", action="store_true")
    sub.add_parser("verify")
    return parser


async def main_async(args: argparse.Namespace) -> None:
    if args.command == "measure":
        result = await run_reconciliation(apply=False)
    elif args.command == "migrate":
        result = await run_reconciliation(apply=bool(args.apply))
        if not args.apply:
            result["command"] = "migrate-preview"
    else:
        result = await run_verify()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
