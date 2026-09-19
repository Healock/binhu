"""Dev-only resident monitor for the Python/Flink metadata projections.

The monitor reads only the isolated derived database.  It writes redacted,
append-only evidence and stops in a durable ``paused`` state on a mismatch.
It deliberately has no Kafka producer, business database, or control-plane
dependency.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .runtime import configuration, ensure_database_identity

SAFE_FIELDS = (
    "revision", "event_count", "changed_field_count", "created_count",
    "saved_count", "claimed_count", "assigned_count", "reviewed_count",
    "archived_count", "deleted_count",
)
MISMATCH_DETAIL_LIMIT = 100
RUN_RE = re.compile(r"^dev-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
EVIDENCE_RE = re.compile(r"^dual-track-[0-9A-Za-z][A-Za-z0-9_-]{3,63}$")
_SAFE_FAILURE_STAGES = frozenset({
    "identity", "identity_environment", "identity_run_id", "identity_options",
    "evidence_directory", "evidence_read", "database_connect",
    "database_identity", "projection_query", "evidence_write", "monitor_loop",
})


class MonitorRuntimeFailure(Exception):
    """An exception carrying only a safe Dev diagnostic summary."""

    def __init__(self, error: BaseException, stage: str):
        self.safe_detail = runtime_failure_detail(error, stage)
        super().__init__(self.safe_detail)


def runtime_failure_detail(error: BaseException, stage: str) -> str:
    """Return a non-sensitive type/stage diagnostic for Dev runtime logs."""
    if stage not in _SAFE_FAILURE_STAGES:
        stage = "monitor_loop"
    error_type = type(error).__name__
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type):
        error_type = "RuntimeError"
    return f"runtime_failure_type={error_type} runtime_failure_stage={stage}"


def _row_key(row: Mapping[str, Any]) -> tuple[str, int]:
    task_id = row.get("task_id")
    source_id = row.get("source_id")
    if not isinstance(task_id, str) or not task_id or type(source_id) is not int or source_id <= 0:
        raise ValueError("invalid projection row identity")
    return task_id, source_id


def _safe_row(row: Mapping[str, Any]) -> dict[str, Any]:
    task_id, source_id = _row_key(row)
    values: dict[str, Any] = {}
    for name in SAFE_FIELDS:
        value = row.get(name)
        if type(value) is not int or value < 0:
            raise ValueError("invalid projection metric")
        values[name] = value
    return {"task_id_sha256": hashlib.sha256(task_id.encode()).hexdigest(),
            "source_id": source_id, **values}


def build_report(
    python_rows: Sequence[Mapping[str, Any]],
    flink_rows: Sequence[Mapping[str, Any]],
    python_events: int,
    flink_events: int,
    run_id: str,
    evidence_id: str,
) -> dict[str, Any]:
    """Compare database snapshots and return a redacted report."""
    if not RUN_RE.fullmatch(run_id) or not EVIDENCE_RE.fullmatch(evidence_id):
        raise ValueError("Dev monitor identity required")
    if type(python_events) is not int or type(flink_events) is not int or python_events < 0 or flink_events < 0:
        raise ValueError("invalid event counts")
    py = {_row_key(row): row for row in python_rows}
    fl = {_row_key(row): row for row in flink_rows}
    differences: list[dict[str, Any]] = []
    for key in sorted(set(py) | set(fl)):
        expected = _safe_row(py[key]) if key in py else None
        actual = _safe_row(fl[key]) if key in fl else None
        if expected != actual:
            differences.append({
                "task_id_sha256": (expected or actual)["task_id_sha256"],
                "revision": (actual or expected)["revision"],
                "fields": sorted(set((expected or {}) | (actual or {})) - {"task_id_sha256", "source_id"}),
                "expected": expected,
                "actual": actual,
                "attribution": "missing_in_track" if expected is None or actual is None else "projection_value_mismatch",
            })
    if python_events != flink_events:
        differences.append({
            "task_id_sha256": None, "revision": None, "fields": ["unique_events"],
            "expected": {"unique_events": python_events},
            "actual": {"unique_events": flink_events},
            "attribution": "event_count_mismatch",
        })
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "environment": "development", "run_id": run_id, "evidence_id": evidence_id,
        "generated_at": now, "python_tasks": len(py), "flink_tasks": len(fl),
        "unique_events_python": python_events, "unique_events_flink": flink_events,
        "differences": differences, "unattributed_difference_count": len(differences),
        "passed": not differences,
    }


def _prefixed_safe_row(row: Mapping[str, Any], prefix: str) -> dict[str, Any] | None:
    """Build one redacted comparison row from a bounded SQL mismatch result."""
    if row.get(f"{prefix}_revision") is None:
        return None
    task_id = row.get("task_id")
    source_id = row.get("source_id")
    if not isinstance(task_id, str) or not task_id or type(source_id) is not int or source_id <= 0:
        raise ValueError("invalid projection row identity")
    values: dict[str, Any] = {}
    for name in SAFE_FIELDS:
        value = row.get(f"{prefix}_{name}")
        if type(value) is not int or value < 0:
            raise ValueError("invalid projection metric")
        values[name] = value
    return {
        "task_id_sha256": hashlib.sha256(task_id.encode()).hexdigest(),
        "source_id": source_id,
        **values,
    }


async def _count(cur: Any, statement: str, params: tuple[Any, ...]) -> int:
    await cur.execute(statement, params)
    row = await cur.fetchone()
    value = row.get("count") if isinstance(row, Mapping) else row[0]
    if isinstance(value, bool):
        raise ValueError("invalid comparison count")
    try:
        count = int(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("invalid comparison count") from None
    if count < 0 or count != value:
        raise ValueError("invalid comparison count")
    return count


async def query_report(
    pool: Any,
    run_id: str,
    evidence_id: str,
    dict_cursor: Any,
) -> dict[str, Any]:
    """Compare both projections with bounded application memory.

    Counts and equality checks stay in MySQL.  Only a fixed number of redacted
    mismatch rows cross into Python, so a 100,000-row healthy run does not
    materialize two full snapshots and two dictionaries in the monitor.
    """
    if not RUN_RE.fullmatch(run_id) or not EVIDENCE_RE.fullmatch(evidence_id):
        raise ValueError("Dev monitor identity required")
    join = (
        "f.run_id=p.run_id AND f.task_id=p.task_id AND f.source_id=p.source_id"
    )
    equal = " AND ".join(f"p.{name}<=>f.{name}" for name in SAFE_FIELDS)
    columns = ",".join(
        ["p.task_id AS task_id", "p.source_id AS source_id"]
        + [f"p.{name} AS p_{name}" for name in SAFE_FIELDS]
        + [f"f.{name} AS f_{name}" for name in SAFE_FIELDS]
    )
    flink_only_columns = ",".join(
        ["f.task_id AS task_id", "f.source_id AS source_id"]
        + [f"NULL AS p_{name}" for name in SAFE_FIELDS]
        + [f"f.{name} AS f_{name}" for name in SAFE_FIELDS]
    )
    async with pool.acquire() as conn:
        async with conn.cursor(dict_cursor) as cur:
            python_tasks = await _count(
                cur, "SELECT COUNT(*) AS count FROM dev_task_metadata_python WHERE run_id=%s", (run_id,)
            )
            flink_tasks = await _count(
                cur, "SELECT COUNT(*) AS count FROM dev_task_metadata WHERE run_id=%s", (run_id,)
            )
            python_events = await _count(
                cur,
                "SELECT COUNT(*) AS count FROM dev_task_metadata_python_events WHERE run_id=%s",
                (run_id,),
            )
            flink_events = await _count(
                cur,
                "SELECT COALESCE(SUM(event_count),0) AS count FROM dev_task_metadata WHERE run_id=%s",
                (run_id,),
            )
            python_side_mismatches = await _count(
                cur,
                "SELECT COUNT(*) AS count FROM dev_task_metadata_python p "
                f"LEFT JOIN dev_task_metadata f ON {join} "
                f"WHERE p.run_id=%s AND (f.task_id IS NULL OR NOT ({equal}))",
                (run_id,),
            )
            flink_only_mismatches = await _count(
                cur,
                "SELECT COUNT(*) AS count FROM dev_task_metadata f "
                "LEFT JOIN dev_task_metadata_python p ON "
                "p.run_id=f.run_id AND p.task_id=f.task_id AND p.source_id=f.source_id "
                "WHERE f.run_id=%s AND p.task_id IS NULL",
                (run_id,),
            )
            await cur.execute(
                f"SELECT {columns} FROM dev_task_metadata_python p "
                f"LEFT JOIN dev_task_metadata f ON {join} "
                f"WHERE p.run_id=%s AND (f.task_id IS NULL OR NOT ({equal})) "
                "ORDER BY p.task_id,p.source_id LIMIT %s",
                (run_id, MISMATCH_DETAIL_LIMIT),
            )
            detail_rows = list(await cur.fetchall())
            remaining = MISMATCH_DETAIL_LIMIT - len(detail_rows)
            if remaining:
                await cur.execute(
                    f"SELECT {flink_only_columns} FROM dev_task_metadata f "
                    "LEFT JOIN dev_task_metadata_python p ON "
                    "p.run_id=f.run_id AND p.task_id=f.task_id AND p.source_id=f.source_id "
                    "WHERE f.run_id=%s AND p.task_id IS NULL "
                    "ORDER BY f.task_id,f.source_id LIMIT %s",
                    (run_id, remaining),
                )
                detail_rows.extend(await cur.fetchall())
    differences: list[dict[str, Any]] = []
    for row in detail_rows:
        expected = _prefixed_safe_row(row, "p")
        actual = _prefixed_safe_row(row, "f")
        changed = list(SAFE_FIELDS) if expected is None or actual is None else [
            name for name in SAFE_FIELDS if expected[name] != actual[name]
        ]
        differences.append({
            "task_id_sha256": (expected or actual)["task_id_sha256"],
            "revision": (actual or expected)["revision"],
            "fields": changed,
            "expected": expected,
            "actual": actual,
            "attribution": (
                "missing_in_track" if expected is None or actual is None else "projection_value_mismatch"
            ),
        })
    if python_events != flink_events:
        differences.append({
            "task_id_sha256": None,
            "revision": None,
            "fields": ["unique_events"],
            "expected": {"unique_events": python_events},
            "actual": {"unique_events": flink_events},
            "attribution": "event_count_mismatch",
        })
    projection_mismatch_count = python_side_mismatches + flink_only_mismatches
    unattributed = projection_mismatch_count + int(python_events != flink_events)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "environment": "development",
        "run_id": run_id,
        "evidence_id": evidence_id,
        "generated_at": now,
        "python_tasks": python_tasks,
        "flink_tasks": flink_tasks,
        "unique_events_python": python_events,
        "unique_events_flink": flink_events,
        "projection_mismatch_count": projection_mismatch_count,
        "differences": differences,
        "difference_details_truncated": projection_mismatch_count > len(detail_rows),
        "unattributed_difference_count": unattributed,
        "passed": unattributed == 0,
    }


def _write_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    path.chmod(0o600)


def write_cycle(evidence_dir: Path, report: Mapping[str, Any], sequence: int) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    _write_exclusive(evidence_dir / f"comparison-{stamp}-{sequence:08d}.json", report)
    status = {"environment": "development", "run_id": report["run_id"],
              "evidence_id": report["evidence_id"],
              "status": "paused" if not report["passed"] else "running",
              "updated_at": report["generated_at"],
              "unattributed_difference_count": report["unattributed_difference_count"]}
    # status is an operational pointer; each mismatch itself is immutable.
    (evidence_dir / "monitor-status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (evidence_dir / "monitor-status.json").chmod(0o600)
    if not report["passed"]:
        _write_exclusive(evidence_dir / f"alert-{stamp}-{sequence:08d}.json", {
            "type": "dual_track_mismatch", "status": "paused",
            "detected_at": report["generated_at"], "run_id": report["run_id"],
            "evidence_id": report["evidence_id"], "differences": report["differences"],
        })


async def run(config: Mapping[str, str], evidence_dir: Path, evidence_id: str,
              interval_seconds: float = 15.0, cycles: int | None = None) -> dict[str, Any]:
    """Run the resident monitor until a mismatch or an optional test limit."""
    stage = "identity"
    try:
        if config.get("APP_ENVIRONMENT") != "development":
            raise MonitorRuntimeFailure(ValueError("environment identity"), "identity_environment")
        if not RUN_RE.fullmatch(config.get("DEV_RUN_ID", "")):
            raise MonitorRuntimeFailure(ValueError("run identity"), "identity_run_id")
        if interval_seconds <= 0 or (cycles is not None and cycles <= 0):
            raise MonitorRuntimeFailure(ValueError("monitor options"), "identity_options")
        stage = "evidence_directory"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        status_path = evidence_dir / "monitor-status.json"
        if status_path.is_symlink():
            raise ValueError("monitor status must not be a symlink")
        if status_path.is_file():
            stage = "evidence_read"
            try:
                previous = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ValueError("monitor status is unreadable") from None
            if previous.get("status") == "paused":
                return {"passed": False, "paused": True,
                        "unattributed_difference_count": previous.get("unattributed_difference_count", 1),
                        "run_id": config["DEV_RUN_ID"], "evidence_id": evidence_id}
        import aiomysql
        stage = "database_connect"
        pool = await aiomysql.create_pool(host=config["MYSQL_HOST"], port=3306,
            user=config["MYSQL_USER"], password=config["MYSQL_PASSWORD"], db=config["MYSQL_DATABASE"],
            minsize=1, maxsize=2, connect_timeout=5, autocommit=True, charset="utf8mb4",
            init_command="SET time_zone='+00:00'")
        stage = "database_identity"
        await ensure_database_identity(pool, config)
    except MonitorRuntimeFailure:
        raise
    except Exception as error:
        raise MonitorRuntimeFailure(error, stage) from None
    try:
        sequence = 0
        while True:
            stage = "projection_query"
            report = await query_report(
                pool, config["DEV_RUN_ID"], evidence_id, aiomysql.DictCursor
            )
            stage = "evidence_write"
            write_cycle(evidence_dir, report, sequence)
            sequence += 1
            if not report["passed"] or (cycles is not None and sequence >= cycles):
                report["paused"] = not report["passed"]
                return report
            await asyncio.sleep(interval_seconds)
    except MonitorRuntimeFailure:
        raise
    except Exception as error:
        raise MonitorRuntimeFailure(error, stage) from None
    finally:
        pool.close()
        await pool.wait_closed()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--evidence-id", required=True)
    parser.add_argument("--interval-seconds", type=float, default=15.0)
    parser.add_argument("--cycles", type=int)
    args = parser.parse_args()
    try:
        config = configuration()
        report = asyncio.run(run(config, args.evidence_dir, args.evidence_id,
                                 args.interval_seconds, args.cycles))
        print(json.dumps({"passed": report["passed"], "paused": report.get("paused", False),
                          "unattributed_difference_count": report["unattributed_difference_count"]}))
        # A mismatch is a deliberate terminal state.  Exit successfully so
        # the Compose restart policy cannot erase the pause by hot-looping.
    except MonitorRuntimeFailure as error:
        raise SystemExit(error.safe_detail) from None
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(runtime_failure_detail(error, "monitor_loop")) from None


if __name__ == "__main__":
    main()
