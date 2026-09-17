"""Controlled Dev-only scale acceptance for the metadata dual track.

The runner creates deterministic fictional metadata events in the isolated
delivery ledger, waits for both projections to converge, and writes only a
redacted report.  It has no business database or external service access.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ALLOWED_SCALES = frozenset({1002, 10_000, 100_000})
RUN_RE = re.compile(r"^dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SOURCE_BASE = 8_000_000_000_000_000_000
FIXTURE_NAMESPACE = uuid.UUID("59ac3f38-ad09-4f3b-b55d-2d6f6a8bd7d2")
SAFE_FIELDS = (
    "revision", "event_count", "changed_field_count", "created_count",
    "saved_count", "claimed_count", "assigned_count", "reviewed_count",
    "archived_count", "deleted_count",
)
# Cover the complete relay-to-projection convergence path.  Without a
# sufficient window, an in-flight but healthy high-volume run is recorded as
# a false consistency failure while its queue is still draining.
TIMEOUTS = {1002: 300, 10_000: 3600, 100_000: 21_600}
SAFE_FAILURE_STAGES = frozenset({"configuration", "database_connect", "enqueue", "compare", "evidence", "runtime"})


class ScaleAcceptanceFailure(Exception):
    def __init__(self, error: BaseException, stage: str):
        self.safe_detail = safe_failure_detail(error, stage)
        super().__init__(self.safe_detail)


def safe_failure_detail(error: BaseException, stage: str) -> str:
    if stage not in SAFE_FAILURE_STAGES:
        stage = "runtime"
    error_type = type(error).__name__
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type):
        error_type = "RuntimeError"
    return f"acceptance_failure_type={error_type} acceptance_failure_stage={stage}"


def validate_scale(value: Any) -> int:
    if type(value) is not int or value not in ALLOWED_SCALES:
        raise ValueError("fixed Dev acceptance scale required")
    return value


def fixture(run_id: str, index: int) -> dict[str, Any]:
    """Return one stable, metadata-only event from the reserved Dev range."""
    if not RUN_RE.fullmatch(run_id or ""):
        raise ValueError("Dev run identity required")
    if type(index) is not int or index < 0 or index >= max(ALLOWED_SCALES):
        raise ValueError("fixture index outside controlled range")
    source_id = SOURCE_BASE + index
    return {
        "schema_version": 1,
        "event_id": str(uuid.uuid5(FIXTURE_NAMESPACE, f"{run_id}/event/{index}")),
        "event_type": "task.saved",
        "task_id": f"t_fullchain:{source_id}",
        "source_id": source_id,
        "revision": index + 1,
        "operation_id": str(uuid.uuid5(FIXTURE_NAMESPACE, f"{run_id}/operation/{index}")),
        "changed_fields": ["task_state"],
        "timestamp": "2026-09-16T00:00:00Z",
        "environment": "development",
        "run_id": run_id,
    }


async def _pool(config: Mapping[str, str]):
    import aiomysql

    pool = await aiomysql.create_pool(
        host=config["MYSQL_HOST"], port=3306, user=config["MYSQL_USER"],
        password=config["MYSQL_PASSWORD"], db=config["MYSQL_DATABASE"],
        minsize=1, maxsize=2, connect_timeout=5, autocommit=False,
        charset="utf8mb4", init_command="SET time_zone='+00:00'",
    )
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT environment,run_id,database_name FROM _pipeline_identity WHERE id=1"
                )
                row = await cur.fetchone()
            await conn.rollback()
        if row != ("development", config["DEV_RUN_ID"], "Dev_EventPipeline"):
            raise ValueError("Dev acceptance database identity mismatch")
        return pool
    except BaseException:
        pool.close()
        await pool.wait_closed()
        raise


async def enqueue(pool, run_id: str, scale: int) -> int:
    """Idempotently enqueue the cumulative controlled fixture set."""
    from .services.kafka_delivery_store import enqueue_delivery

    scale = validate_scale(scale)
    inserted = 0
    for start in range(0, scale, 250):
        async with pool.acquire() as conn:
            try:
                await conn.begin()
                async with conn.cursor() as cur:
                    for index in range(start, min(start + 250, scale)):
                        await enqueue_delivery(cur, fixture(run_id, index), run_id=run_id)
                        inserted += 1
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
    return inserted


async def _scalar(cur, sql: str, params: tuple[Any, ...]) -> int:
    await cur.execute(sql, params)
    row = await cur.fetchone()
    return int(row[0])


def acceptance_outcome(
    *, scale: int, counts: tuple[int, ...], projection_mismatches: int,
    revision_mismatches: int,
) -> dict[str, Any]:
    """Separate an unfinished pipeline from a converged data difference."""
    complete = all(value == scale for value in counts)
    difference_count = projection_mismatches + revision_mismatches if complete else 0
    return {
        "complete": complete,
        "convergence_pending_count": max(0, scale - counts[0]),
        "unattributed_difference_count": difference_count,
        "passed": complete and difference_count == 0,
    }


async def snapshot(pool, run_id: str, scale: int) -> dict[str, Any]:
    """Read aggregate identities without exposing synthetic task identifiers."""
    columns = " AND ".join(f"p.{name}<=>f.{name}" for name in SAFE_FIELDS)
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status,COUNT(*) FROM _kafka_event_delivery WHERE run_id=%s GROUP BY status",
                (run_id,),
            )
            delivery = {str(status): int(count) for status, count in await cur.fetchall()}
            python_events = await _scalar(
                cur, "SELECT COUNT(*) FROM dev_task_metadata_python_events WHERE run_id=%s", (run_id,)
            )
            python_rows = await _scalar(
                cur, "SELECT COUNT(*) FROM dev_task_metadata_python WHERE run_id=%s", (run_id,)
            )
            flink_rows = await _scalar(
                cur, "SELECT COUNT(*) FROM dev_task_metadata WHERE run_id=%s", (run_id,)
            )
            flink_events = await _scalar(
                cur, "SELECT COALESCE(SUM(event_count),0) FROM dev_task_metadata WHERE run_id=%s", (run_id,)
            )
            revision_rows = await _scalar(
                cur, "SELECT COUNT(*) FROM dev_task_revisions WHERE run_id=%s", (run_id,)
            )
            await cur.execute(
                "SELECT COUNT(*) FROM dev_task_metadata_python p LEFT JOIN dev_task_metadata f "
                "ON f.run_id=p.run_id AND f.task_id=p.task_id AND f.source_id=p.source_id "
                f"WHERE p.run_id=%s AND (f.task_id IS NULL OR NOT ({columns}))",
                (run_id,),
            )
            projection_mismatches = int((await cur.fetchone())[0])
            projection_mismatches += await _scalar(
                cur,
                "SELECT COUNT(*) FROM dev_task_metadata f LEFT JOIN dev_task_metadata_python p "
                "ON p.run_id=f.run_id AND p.task_id=f.task_id AND p.source_id=f.source_id "
                "WHERE f.run_id=%s AND p.task_id IS NULL",
                (run_id,),
            )
            revision_mismatches = await _scalar(
                cur,
                "SELECT COUNT(*) FROM dev_task_revisions r LEFT JOIN dev_task_metadata f "
                "ON f.run_id=r.run_id AND f.task_id=r.task_id AND f.source_id=r.source_id "
                "WHERE r.run_id=%s AND (f.task_id IS NULL OR NOT (r.revision<=>f.revision))",
                (run_id,),
            )
        await conn.rollback()
    published = delivery.get("published", 0)
    delivery_total = sum(delivery.values())
    counts = (published, delivery_total, python_events, python_rows, flink_rows,
              flink_events, revision_rows)
    outcome = acceptance_outcome(
        scale=scale, counts=counts,
        projection_mismatches=projection_mismatches,
        revision_mismatches=revision_mismatches,
    )
    return {
        "environment": "development", "run_id": run_id, "scale": scale,
        "delivery_published": published, "delivery_total": delivery_total,
        "python_projection_rows": python_rows, "flink_projection_rows": flink_rows,
        "flink_revision_rows": revision_rows,
        "unique_events_python": python_events, "unique_events_flink": flink_events,
        "projection_mismatch_count": projection_mismatches,
        "revision_mismatch_count": revision_mismatches,
        **outcome,
    }


def _write_report(run_id: str, scale: int, report: Mapping[str, Any]) -> None:
    root = Path(os.environ.get(
        "DUAL_TRACK_EVIDENCE_DIR", "/var/lib/binhu-dev-event-pipeline/evidence"
    )) / run_id
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = root / f"scale-{scale}-{stamp}.json"
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)
    status = {
        "environment": "development", "run_id": run_id,
        "evidence_id": f"scale-{scale}",
        "status": "running" if report.get("passed") else "paused",
        "updated_at": report["generated_at"],
        "unattributed_difference_count": report["unattributed_difference_count"],
    }
    status_path = root / "status.json"
    status_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    status_path.chmod(0o600)


async def run(scale: int) -> dict[str, Any]:
    stage = "configuration"
    pool = None
    try:
        from .runtime import configuration

        scale = validate_scale(scale)
        config = configuration()
        if config["APP_ENVIRONMENT"] != "development":
            raise ValueError("Dev acceptance identity required")
        stage = "database_connect"
        pool = await _pool(config)
        stage = "enqueue"
        await enqueue(pool, config["DEV_RUN_ID"], scale)
        deadline = asyncio.get_running_loop().time() + TIMEOUTS[scale]
        while True:
            stage = "compare"
            report = await snapshot(pool, config["DEV_RUN_ID"], scale)
            if report["passed"] or asyncio.get_running_loop().time() >= deadline:
                report["generated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                stage = "evidence"
                _write_report(config["DEV_RUN_ID"], scale, report)
                return report
            await asyncio.sleep(2)
    except ScaleAcceptanceFailure:
        raise
    except Exception as error:
        raise ScaleAcceptanceFailure(error, stage) from None
    finally:
        if pool is not None:
            pool.close()
            await pool.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", type=int, choices=sorted(ALLOWED_SCALES), required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args.scale))
        print(json.dumps(report, sort_keys=True))
        if not report["passed"]:
            raise SystemExit(2)
    except Exception as error:
        detail = getattr(error, "safe_detail", safe_failure_detail(error, "runtime"))
        raise SystemExit(detail) from None


if __name__ == "__main__":
    main()
