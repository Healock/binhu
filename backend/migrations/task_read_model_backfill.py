"""Emit current isolated local tasks into the v3 Outbox for Kafka/Flink replay.

This command never writes Redis. The existing relay, Kafka topic and Flink
consumer remain the only path that materializes the Backend read model.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid

import aiomysql

from config import settings
from services.domain_events import enqueue_event, stable_task_id, task_changed_field_categories
from services.parsers import PARSER_REGISTRY
from services.task_commands import append_task_command


SOURCE_TYPES = ("local_table", "local_dispatch", "one_time_continuation_import")
NAMESPACE = uuid.UUID("f1bc9131-5a69-4fb6-9bc4-822a37ad793f")
BATCH_SIZE = 250


def identity() -> tuple[str, str]:
    environment = settings.APP_ENVIRONMENT
    run_id = settings.TASK_READ_MODEL_RUN_ID.strip()
    if environment not in {"development", "staging"}:
        raise ValueError("isolated environment required")
    from services.task_read_model import task_read_model_keys
    task_read_model_keys(environment, run_id, "t_fullchain:1")
    if len(settings.TASK_EVENT_ENCRYPTION_KEY.encode("utf-8")) < 32:
        raise ValueError("task event encryption identity required")
    return environment, run_id


def event_ids(environment: str, run_id: str, parser: str, source_id: int, revision: int) -> tuple[str, str, str]:
    key = f"{environment}:{run_id}:{parser}:{source_id}:{revision}"
    return tuple(str(uuid.uuid5(NAMESPACE, f"{kind}:{key}")) for kind in ("command", "operation", "event"))


async def _connect(*, autocommit: bool):
    return await aiomysql.connect(
        host=settings.MYSQL_HOST, port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER, password=settings.MYSQL_PASSWORD,
        db=settings.MYSQL_ONLINE_DATA_DB, connect_timeout=5,
        autocommit=autocommit, charset="utf8mb4",
    )


async def measure() -> dict:
    environment, run_id = identity()
    conn = await _connect(autocommit=True)
    counts = {}
    try:
        async with conn.cursor() as cur:
            for parser_type in PARSER_REGISTRY:
                if parser_type == "default":
                    continue
                await cur.execute(
                    "SELECT COUNT(*) FROM _online_source_rows WHERE parser_type=%s "
                    "AND archived_at IS NULL AND source_kind IN (%s,%s,%s)",
                    (parser_type, *SOURCE_TYPES),
                )
                row = await cur.fetchone()
                counts[parser_type] = int(row[0])
    finally:
        conn.close()
    return {"environment": environment, "run_id": run_id, "active_sources": counts,
            "total": sum(counts.values()), "writes": 0}


async def apply() -> dict:
    baseline = await measure()
    environment, run_id = identity()
    conn = await _connect(autocommit=False)
    emitted = skipped = 0
    try:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            for parser_type, parser in PARSER_REGISTRY.items():
                if parser_type == "default":
                    continue
                await cur.execute(
                    "SELECT id,physical_row,revision,values_json FROM _online_source_rows "
                    "WHERE parser_type=%s AND archived_at IS NULL "
                    "AND source_kind IN (%s,%s,%s) ORDER BY id",
                    (parser_type, *SOURCE_TYPES),
                )
                while True:
                    rows = await cur.fetchmany(BATCH_SIZE)
                    if not rows:
                        break
                    for row in rows:
                        source_id = int(row["id"])
                        physical_row = int(row["physical_row"])
                        revision = int(row["revision"])
                        task_id = stable_task_id(parser.table_name, physical_row)
                        command_id, operation_id, event_id = event_ids(
                            environment, run_id, parser_type, source_id, revision,
                        )
                        await cur.execute(
                            "SELECT 1 FROM _task_command_records WHERE command_id=%s OR event_id=%s LIMIT 1",
                            (command_id, event_id),
                        )
                        if await cur.fetchone():
                            skipped += 1
                            from services.task_view_state import enqueue_current_task_view_state_snapshot

                            await enqueue_current_task_view_state_snapshot(
                                cur, source_id=source_id, snapshot={},
                            )
                            await conn.commit()
                            continue
                        snapshot = row["values_json"]
                        if isinstance(snapshot, str):
                            snapshot = json.loads(snapshot)
                        if not isinstance(snapshot, dict) or not snapshot:
                            raise ValueError("active task snapshot unavailable")
                        changed = task_changed_field_categories(parser_type, parser.COLUMNS)
                        envelope = await append_task_command(
                            cur, command_id=command_id, operation_id=operation_id,
                            actor_id=0, environment=environment, parser_type=parser_type,
                            task_id=task_id, source_id=source_id, action="create",
                            expected_revision=None, revision=revision,
                            changed_fields=changed, snapshot=snapshot, event_id=event_id,
                            authorization_policy="migration.task_read_model_backfill",
                            authorization_scope_sha256=(
                                "0" * 64
                            ),
                            authorization_decision="allow",
                        )
                        await enqueue_event(
                            cur, domain="online", event_type="online.task.created",
                            aggregate_type="online_task", aggregate_id=task_id,
                            aggregate_revision=revision, audiences=["authenticated"],
                            command_id=command_id,
                            task_id=task_id, source_id=source_id, operation_id=operation_id,
                            changed_fields=changed, encrypted_payload=envelope,
                            event_id=event_id,
                        )
                        from services.task_view_state import enqueue_current_task_view_state_snapshot

                        await enqueue_current_task_view_state_snapshot(
                            cur, source_id=source_id, snapshot=snapshot,
                        )
                        emitted += 1
                    await conn.commit()
    except BaseException:
        await conn.rollback()
        raise
    finally:
        conn.close()
    return {**baseline, "emitted": emitted, "already_emitted": skipped,
            "writes": emitted, "idempotent": True}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("measure", "apply"))
    args = parser.parse_args()
    result = asyncio.run(measure() if args.action == "measure" else apply())
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
    main()
