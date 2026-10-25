"""可追踪的外部数据获取后台任务。

任务记录只保存来源、阶段、数量和安全错误摘要，不保存外部行正文。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any, Awaitable, Callable

from database import db_manager

_background_tasks: set[asyncio.Task] = set()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def _safe_json(value: Any) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _safe_error_message(exc: BaseException) -> str:
    """Persist only a generic operator hint; exception text may contain source data."""
    if isinstance(exc, (ValueError, RuntimeError)) and str(exc) in {
        "visit_detail_commit_failed", "visit_rating_commit_failed",
    }:
        return "来源快照未通过完整性门禁，当前本地数据未替换"
    return "外部来源运行失败，请查看错误码后重试"


_REDACTED_RESULT_KEYS = {
    "rows", "payload", "payload_json", "response", "raw_rows", "unresolved",
    "source_rows", "body", "content", "password", "token", "secret",
}


def _safe_result(value: Any, *, depth: int = 0) -> Any:
    """Keep run history to aggregate metadata; never persist source row bodies."""
    if depth > 3:
        return None
    if isinstance(value, dict):
        return {
            str(key): _safe_result(item, depth=depth + 1)
            for key, item in value.items()
            if str(key).lower() not in _REDACTED_RESULT_KEYS
            and not any(marker in str(key).lower() for marker in (
                "identity", "phone", "mobile", "tel", "address", "name",
                "id_card", "credential", "person",
            ))
        }
    if isinstance(value, list):
        return [_safe_result(item, depth=depth + 1) for item in value[:20]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return str(value)[:500] if isinstance(value, str) else value
    return str(value)[:200]


def _kind_variants(kind: str) -> list[str]:
    """Return the canonical kind and every legacy spelling for its lease."""
    from services.external_acquisition_registry import SOURCE_ALIASES

    canonical = SOURCE_ALIASES.get(kind, kind)
    return [canonical, *[alias for alias, target in SOURCE_ALIASES.items() if target == canonical]]


def _has_incompatible_semantics(requested: str, active: str) -> bool:
    """Preview/confirmation jobs must never be reused as auto-commit jobs."""
    preview_kinds = {"visit_source_preview", "visit_source_confirm"}
    return (requested in preview_kinds) != (active in preview_kinds)


def _public(row: tuple | None) -> dict[str, Any] | None:
    if not row:
        return None
    raw_result = row[8]
    if isinstance(raw_result, (bytes, bytearray)):
        raw_result = raw_result.decode("utf-8", errors="replace")
    if isinstance(raw_result, str):
        try:
            raw_result = json.loads(raw_result or "{}")
        except (TypeError, ValueError):
            raw_result = {}
    result = {
        "id": int(row[0]),
        "kind": str(row[1]),
        "status": str(row[2]),
        "phase": str(row[3] or "queued"),
        "current": int(row[4] or 0),
        "total": int(row[5]) if row[5] is not None else None,
        "message": str(row[6] or ""),
        "requested_by": int(row[7]) if row[7] is not None else None,
        "result": _safe_result(raw_result or {}),
        "error_code": str(row[9]) if row[9] else None,
        "error_message": "外部来源运行失败，请查看错误码后重试" if row[10] else None,
        "created_at": _iso(row[11]),
        "started_at": _iso(row[12]),
        "finished_at": _iso(row[13]),
        "updated_at": _iso(row[14]),
        "schedule_id": int(row[15]) if row[15] is not None else None,
        "trigger_source": str(row[16] or "manual"),
        "auto_commit_status": str(row[17]) if row[17] else None,
        "safety_gate_code": str(row[18]) if row[18] else None,
    }
    if result["total"]:
        result["progress"] = min(100, round(result["current"] * 100 / result["total"]))
    else:
        result["progress"] = None
    return result


SELECT = """
SELECT id,kind,status,phase,current_count,total_count,progress_message,
       requested_by,result_json,error_code,error_message,created_at,started_at,
       finished_at,updated_at,schedule_id,trigger_source,auto_commit_status,
       safety_gate_code,payload_json
FROM _external_acquisition_runs
"""


class JobContext:
    def __init__(self, run_id: int):
        self.run_id = run_id

    async def update(self, *, phase: str, current: int | None = None,
                     total: int | None = None, message: str = "") -> None:
        pool = db_manager.get_pool("online_data")
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE _external_acquisition_runs SET phase=%s,current_count=%s,total_count=%s,"
                    "progress_message=%s,status='running' WHERE id=%s",
                    (phase, current, total, message[:500], self.run_id),
                )


async def create_job(kind: str, requested_by: int | None, payload: dict[str, Any],
                     runner: Callable[[JobContext], Awaitable[dict[str, Any]]],
                     *, dedupe_key: str | None = None, schedule_id: int | None = None,
                     trigger_source: str = "manual") -> tuple[dict[str, Any], bool]:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            # The schedule row is the source-wide lease.  Locking it before
            # checking active runs closes the no-row race where two processes
            # both observe an idle source and insert a run concurrently.
            from services.external_acquisition_registry import SOURCE_ALIASES, SOURCE_MAP
            source_key = SOURCE_ALIASES.get(kind, kind)
            if source_key in SOURCE_MAP:
                await cur.execute(
                    "SELECT id FROM _external_acquisition_schedules "
                    "WHERE source_key=%s FOR UPDATE",
                    (source_key,),
                )
            # The dedupe key covers repeated requests for the same operation,
            # but a source-wide lease is also required: a manual run and a
            # scheduled run must not update the same source concurrently.
            await cur.execute(
                "SELECT id,kind FROM _external_acquisition_runs "
                "WHERE kind IN (" + ",".join(["%s"] * len(_kind_variants(kind))) + ") "
                "AND status IN ('queued','running') "
                "ORDER BY id DESC LIMIT 1 FOR UPDATE",
                tuple(_kind_variants(kind)),
            )
            active = await cur.fetchone()
            if active:
                if _has_incompatible_semantics(kind, str(active[1])):
                    await conn.rollback()
                    raise RuntimeError("source_busy")
                await conn.commit()
                existing_run = await get_job(int(active[0]))
                return (existing_run or {}, True)
            if dedupe_key:
                await cur.execute(
                    "SELECT id FROM _external_acquisition_runs WHERE kind IN (" + ",".join(["%s"] * len(_kind_variants(kind))) + ") AND dedupe_key=%s "
                    "AND status IN ('queued','running') ORDER BY id DESC LIMIT 1",
                    (*_kind_variants(kind), dedupe_key),
                )
                existing = await cur.fetchone()
                if existing:
                    run_id = int(existing[0])
                    await conn.commit()
                    return ((await get_job(run_id)) or {}, True)
            await cur.execute(
                "INSERT INTO _external_acquisition_runs "
                "(kind,status,phase,requested_by,payload_json,dedupe_key,schedule_id,trigger_source) "
                "VALUES (%s,'queued','queued',%s,%s,%s,%s,%s)",
                (kind, requested_by, _safe_json(_safe_result(payload)), dedupe_key, schedule_id, trigger_source),
            )
            run_id = int(cur.lastrowid)
            await conn.commit()
    task = asyncio.create_task(_execute(run_id, runner))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return ((await get_job(run_id)) or {}, False)


async def _fetch_row(pool, run_id: int) -> tuple | None:
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"{SELECT} WHERE id=%s", (run_id,))
            return await cur.fetchone()


async def get_job(run_id: int) -> dict[str, Any] | None:
    return _public(await _fetch_row(db_manager.get_pool("online_data"), run_id))


async def get_job_metadata(run_id: int) -> dict[str, Any] | None:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT kind,status,payload_json,schedule_id FROM _external_acquisition_runs WHERE id=%s",
                (run_id,),
            )
            row = await cur.fetchone()
    if not row:
        return None
    payload = row[2]
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="replace")
    try:
        payload = json.loads(payload or "{}") if isinstance(payload, str) else (payload or {})
    except (TypeError, ValueError):
        payload = {}
    return {"kind": str(row[0]), "status": str(row[1]), "payload": payload, "schedule_id": int(row[3]) if row[3] is not None else None}


async def _sync_schedule_terminal(run_id: int, status: str, error_code: str | None = None) -> None:
    """Publish a terminal run state to its schedule without holding the run connection."""
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT schedule_id FROM _external_acquisition_runs WHERE id=%s",
                (run_id,),
            )
            row = await cur.fetchone()
    if row and row[0] is not None:
        from services.external_acquisition_registry import mark_schedule_result

        await mark_schedule_result(int(row[0]), status, error_code)


async def latest_job(kind: str) -> dict[str, Any] | None:
    from services.external_acquisition_registry import SOURCE_ALIASES
    canonical = SOURCE_ALIASES.get(kind, kind)
    kinds = _kind_variants(kind)
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(kinds))
            await cur.execute(f"{SELECT} WHERE kind IN ({placeholders}) ORDER BY id DESC LIMIT 1", kinds)
            return _public(await cur.fetchone())


async def _execute(run_id: int, runner: Callable[[JobContext], Awaitable[dict[str, Any]]]) -> None:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _external_acquisition_runs SET status='running',phase='starting',started_at=UTC_TIMESTAMP() WHERE id=%s",
                (run_id,),
            )
    try:
        result = await runner(JobContext(run_id))
    except asyncio.CancelledError:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE _external_acquisition_runs SET status='interrupted',phase='interrupted',"
                    "error_code='cancelled',error_message='服务关闭或任务被取消',finished_at=UTC_TIMESTAMP() WHERE id=%s",
                    (run_id,),
                )
        await _sync_schedule_terminal(run_id, "interrupted", "cancelled")
        raise
    except Exception as exc:  # noqa: BLE001 - safe public summary
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE _external_acquisition_runs SET status='failed',phase='finished',"
                    "error_code=%s,error_message=%s,auto_commit_status='skipped',"
                    "safety_gate_code=%s,finished_at=UTC_TIMESTAMP() WHERE id=%s",
                    (type(exc).__name__[:60], _safe_error_message(exc), "runner_exception", run_id),
                )
        await _sync_schedule_terminal(run_id, "failed", "runner_exception")
        return
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            status = str(result.pop("status", "success"))
            gate_code = result.pop("safety_gate_code", None)
            auto_commit_status = result.pop("auto_commit_status", None)
            await cur.execute(
                "UPDATE _external_acquisition_runs SET status=%s,phase='finished',result_json=%s,"
                "progress_message=%s,auto_commit_status=%s,safety_gate_code=%s,finished_at=UTC_TIMESTAMP() WHERE id=%s",
                (status, _safe_json(_safe_result(result)), str(result.get("message", "已完成"))[:500],
                 auto_commit_status, gate_code, run_id),
            )
    await _sync_schedule_terminal(run_id, status, gate_code)


async def recover_interrupted_jobs() -> int:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT schedule_id FROM _external_acquisition_runs "
                "WHERE status IN ('queued','running') AND schedule_id IS NOT NULL"
            )
            schedule_ids = [int(row[0]) for row in await cur.fetchall()]
            await cur.execute(
                "UPDATE _external_acquisition_runs SET status='interrupted',phase='interrupted',"
                "error_code='service_restarted',error_message='服务重启，任务未继续执行',finished_at=UTC_TIMESTAMP() "
                "WHERE status IN ('queued','running')"
            )
            run_count = int(cur.rowcount or 0)
            if schedule_ids:
                placeholders = ",".join(["%s"] * len(schedule_ids))
                await cur.execute(
                    f"UPDATE _external_acquisition_schedules SET last_status='interrupted',"
                    f"last_error_code='service_restarted' WHERE id IN ({placeholders})",
                    tuple(schedule_ids),
                )
            await conn.commit()
            return run_count


async def stop_external_acquisition_tasks() -> None:
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
