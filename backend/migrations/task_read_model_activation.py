"""Verify an isolated task read-model generation before enabling Backend reads."""
from __future__ import annotations

import argparse
import asyncio
import json

import aiomysql
import redis.asyncio as redis

from config import settings
from services.parsers import PARSER_REGISTRY
from services.task_read_model import index_key, task_read_model_keys
from services.task_view_state import (
    TASK_VIEW_STATE_DOMAINS,
    task_view_state_index_keys,
    task_view_state_keys,
    read_task_view_states_for_tasks,
)


SOURCE_TYPES = ("local_table", "local_dispatch", "one_time_continuation_import")
BATCH_SIZE = 500


def _identity() -> tuple[str, str]:
    environment = settings.APP_ENVIRONMENT
    run_id = settings.TASK_READ_MODEL_RUN_ID.strip()
    if environment not in {"development", "staging"}:
        raise ValueError("isolated environment required")
    task_read_model_keys(environment, run_id, "t_fullchain:1")
    if len(settings.TASK_EVENT_ENCRYPTION_KEY.encode("utf-8")) < 32:
        raise ValueError("task read-model encryption identity required")
    return environment, run_id


def _index_membership_mismatch_count(expected: dict[str, set[str]], actual: dict[str, set[str]]) -> int:
    """Count missing, extra, and stray index memberships across a generation."""
    return sum(
        1 for key in set(expected) | set(actual)
        if expected.get(key, set()) != actual.get(key, set())
    )


async def _check_generation() -> tuple[dict, redis.Redis, str]:
    environment, run_id = _identity()
    mysql = await aiomysql.connect(
        host=settings.MYSQL_HOST, port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER, password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_ONLINE_DATA_DB, connect_timeout=5,
        autocommit=True, charset="utf8mb4",
    )
    client = redis.from_url(
        settings.REDIS_URL, decode_responses=True,
        socket_connect_timeout=3, socket_timeout=8,
    )
    try:
        await client.ping()
        _task_key, _indexes_key, prefix = task_read_model_keys(
            environment, run_id, "t_fullchain:1",
        )
        summaries = []
        mismatches = 0
        expected_group_indexes: dict[str, set[str]] = {}
        expected_task_indexes: dict[str, set[str]] = {}
        state_index_mismatches = 0
        canonical_tasks: dict[str, tuple[str, int]] = {}
        for parser_type, parser in PARSER_REGISTRY.items():
            if parser_type == "default":
                continue
            async with mysql.cursor() as cur:
                await cur.execute(
                    "SELECT id,physical_row,revision,row_key FROM _online_source_rows "
                    "WHERE parser_type=%s AND archived_at IS NULL "
                    "AND source_kind IN (%s,%s,%s) ORDER BY id",
                    (parser_type, *SOURCE_TYPES),
                )
                sources = await cur.fetchall()
            member_key = index_key(
                environment, run_id, "task_type", parser.table_name,
                secret=settings.TASK_EVENT_ENCRYPTION_KEY,
            )
            indexed_members = {
                str(item) for item in await client.zrange(member_key, 0, -1)
            }
            indexed = len(indexed_members)
            expected_members: set[str] = set()
            parser_mismatches = 0
            parser_state_index_mismatches = 0
            canonical_sources: dict[str, tuple[int, int, int]] = {}
            for source_id, physical_row, revision, row_key in sources:
                canonical_sources.setdefault(
                    str(row_key), (int(source_id), int(physical_row), int(revision)),
                )
            for offset in range(0, len(sources), BATCH_SIZE):
                batch = sources[offset:offset + BATCH_SIZE]
                pipe = client.pipeline(transaction=False)
                identities = []
                for source_id, physical_row, revision, row_key in batch:
                    task_id = f"{parser.table_name}:{int(physical_row)}"
                    expected_members.add(task_id)
                    task_key, _indexes_key, _prefix = task_read_model_keys(
                        environment, run_id, task_id,
                    )
                    identities.append((int(source_id), int(revision), task_id, str(row_key)))
                    pipe.hgetall(task_key)
                stored_rows = await pipe.execute()
                view_tasks = []
                for expected, stored in zip(identities, stored_rows):
                    source_id, revision, task_id, row_key = expected
                    if (not stored or stored.get("environment") != environment
                            or stored.get("run_id") != run_id
                            or int(stored.get("source_id", 0) or 0) != source_id
                            or int(stored.get("revision", 0) or 0) != revision
                            or stored.get("tombstone") != "0"):
                        parser_mismatches += 1
                        continue
                    task_revision = int(stored.get("revision", 0))
                    view_tasks.append({"task_id": task_id, "revision": task_revision})
                    if canonical_sources.get(row_key, (None,))[0] == source_id:
                        group_token = str(stored.get("row_group_token") or "")
                        if not group_token:
                            parser_state_index_mismatches += 1
                        else:
                            canonical_tasks[task_id] = (group_token, task_revision)
                try:
                    states_by_task = await read_task_view_states_for_tasks(
                        client, environment=environment, run_id=run_id,
                        tasks=view_tasks,
                    )
                    for task in view_tasks:
                        task_id = task["task_id"]
                        for domain in TASK_VIEW_STATE_DOMAINS:
                            state_update = states_by_task[task_id][domain]
                            keys = task_view_state_keys(environment, run_id, task_id, domain)
                            expected_keys = set(task_view_state_index_keys(
                                environment=environment, run_id=run_id,
                                domain=domain, state=state_update["state"],
                                removed=state_update["removed"],
                            ))
                            for index_key in expected_keys:
                                expected_task_indexes.setdefault(index_key, set()).add(task_id)
                            actual_keys = set(await client.smembers(keys[1]))
                            if actual_keys != expected_keys:
                                parser_state_index_mismatches += 1
                            for index_key in expected_keys:
                                if not await client.sismember(index_key, task_id):
                                    parser_state_index_mismatches += 1
                            canonical = canonical_tasks.get(task_id)
                            if canonical:
                                group_token, canonical_revision = canonical
                                group_index_keys = {
                                    index_key.replace(":stateidx:v1:", ":gidx-state:v1:")
                                    for index_key in expected_keys
                                } if canonical_revision == task["revision"] else set()
                                tracked_key = (
                                    f"{prefix}:group:{group_token}:state-indexes:{domain}"
                                )
                                if set(await client.smembers(tracked_key)) != group_index_keys:
                                    parser_state_index_mismatches += 1
                                for group_index in group_index_keys:
                                    if await client.zscore(group_index, group_token) is None:
                                        parser_state_index_mismatches += 1
                                    expected_group_indexes.setdefault(group_index, set()).add(group_token)
                except Exception:
                    parser_state_index_mismatches += max(1, len(view_tasks))
            parser_mismatches += parser_state_index_mismatches
            state_index_mismatches += parser_state_index_mismatches
            membership_matches = indexed_members == expected_members
            mismatches += parser_mismatches + int(not membership_matches)
            summaries.append({
                "parser_type": parser_type,
                "mysql_active_sources": len(sources),
                "redis_indexed_tasks": indexed,
                "revision_or_identity_mismatches": parser_mismatches,
                "state_index_mismatches": parser_state_index_mismatches,
                "membership_matches": membership_matches,
            })
        actual_task_indexes: dict[str, set[str]] = {}
        async for key in client.scan_iter(match=f"{prefix}:stateidx:v1:*", count=BATCH_SIZE):
            normalized_key = key.decode() if isinstance(key, bytes) else str(key)
            actual_task_indexes[normalized_key] = {
                item.decode() if isinstance(item, bytes) else str(item)
                for item in await client.smembers(normalized_key)
            }
        task_membership_mismatches = _index_membership_mismatch_count(
            expected_task_indexes, actual_task_indexes,
        )
        state_index_mismatches += task_membership_mismatches
        mismatches += task_membership_mismatches
        async for key in client.scan_iter(match=f"{prefix}:gidx-state:v1:*", count=BATCH_SIZE):
            normalized_key = key.decode() if isinstance(key, bytes) else str(key)
            indexed_groups = {
                item.decode() if isinstance(item, bytes) else str(item)
                for item in await client.zrange(normalized_key, 0, -1)
            }
            if indexed_groups != expected_group_indexes.get(normalized_key, set()):
                state_index_mismatches += 1
                mismatches += 1
        result = {
            "environment": environment,
            "run_id": run_id,
            "parsers": summaries,
            "mismatch_count": mismatches,
            "state_index_mismatch_count": state_index_mismatches,
            "ready": mismatches == 0,
        }
        return result, client, prefix
    except BaseException:
        await client.aclose()
        raise
    finally:
        mysql.close()


async def run(command: str) -> dict:
    result, client, prefix = await _check_generation()
    try:
        state_key = f"{prefix}:state"
        if command == "verify" and result["ready"]:
            await client.set(f"{prefix}:state-index-version", "v1")
            await client.set(state_key, f"ready:{result['run_id']}")
            result["readiness_marked"] = True
        else:
            if command == "verify":
                await client.delete(f"{prefix}:state-index-version")
                await client.delete(state_key)
            result["readiness_marked"] = False
        return result
    finally:
        await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("measure", "verify"))
    args = parser.parse_args()
    result = asyncio.run(run(args.command))
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    if args.command == "verify" and not result["ready"]:
        raise SystemExit("task_read_model_generation_incomplete")


if __name__ == "__main__":
    main()
