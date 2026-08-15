"""Persistent background acquisition for landlord responsibility notices."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any

from config import settings
from database import db_manager
from services.registry_certificate_source import (
    CERTIFICATE_PAGE_SIZE,
    iter_certificate_pages,
)
from services.registry_import import classify_certificate_rows
from services.visit_source import VisitSourceError


CERTIFICATE_SOURCE_LOCK = "binhu_registry_certificate_source"
REGISTRY_IMPORT_WRITE_CHUNK = 500
ACTIVE_STATUSES = {"pending", "running"}
_background_tasks: set[asyncio.Task] = set()


def _json(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, type(default)) else default
        except (TypeError, ValueError):
            return default
    return default


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() + "Z" if value else None


def _public_run(row: tuple | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": int(row[0]),
        "status": str(row[1]),
        "phase": str(row[2]),
        "current_page": int(row[3] or 0),
        "fetched_count": int(row[4] or 0),
        "accepted_count": int(row[5] or 0),
        "rejected_count": int(row[6] or 0),
        "batch_id": int(row[7]) if row[7] is not None else None,
        "preview": _json(row[8], {}),
        "error_code": str(row[9]) if row[9] else None,
        "error_message": str(row[10]) if row[10] else None,
        "started_at": _iso(row[11]),
        "finished_at": _iso(row[12]),
        "created_at": _iso(row[13]),
        "updated_at": _iso(row[14]),
    }


RUN_SELECT = """
    SELECT id,status,phase,current_page,fetched_count,accepted_count,
           rejected_count,batch_id,summary_json,error_code,error_message,started_at,
           finished_at,created_at,updated_at
    FROM registry_certificate_source_runs
"""


async def get_certificate_source_run(run_id: int) -> dict[str, Any] | None:
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"{RUN_SELECT} WHERE id=%s", (run_id,))
            return _public_run(await cur.fetchone())


async def get_latest_certificate_source_run() -> dict[str, Any] | None:
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(f"{RUN_SELECT} ORDER BY id DESC LIMIT 1")
            return _public_run(await cur.fetchone())


async def create_certificate_source_run(requested_by: int | None) -> tuple[dict[str, Any], bool]:
    """Create one run, or return the active run when a click is repeated."""
    pool = db_manager.get_pool("registry")
    run_id = 0
    reused = False
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT GET_LOCK(%s, 5)", (CERTIFICATE_SOURCE_LOCK,))
            lock_row = await cur.fetchone()
            if not lock_row or lock_row[0] != 1:
                raise RuntimeError("告知书读取任务正在创建，请稍后重试")
            try:
                await cur.execute(
                    "SELECT id FROM registry_certificate_source_runs "
                    "WHERE status IN ('pending','running') ORDER BY id DESC LIMIT 1"
                )
                existing = await cur.fetchone()
                if existing:
                    run_id = int(existing[0])
                    reused = True
                else:
                    await cur.execute(
                        "INSERT INTO registry_certificate_source_runs "
                        "(status,phase,requested_by) VALUES ('pending','queued',%s)",
                        (requested_by,),
                    )
                    run_id = int(cur.lastrowid)
            finally:
                await cur.execute("SELECT RELEASE_LOCK(%s)", (CERTIFICATE_SOURCE_LOCK,))
                await cur.fetchone()
    if not reused:
        launch_certificate_source_run(run_id)
    run = await get_certificate_source_run(run_id)
    if not run:
        raise RuntimeError("告知书读取任务创建后无法重新定位")
    return run, reused


async def retry_certificate_source_run(run_id: int, *, restart: bool = False) -> dict[str, Any]:
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT GET_LOCK(%s, 5)", (CERTIFICATE_SOURCE_LOCK,))
            lock_row = await cur.fetchone()
            if not lock_row or lock_row[0] != 1:
                raise RuntimeError("告知书读取任务正在更新，请稍后重试")
            try:
                await cur.execute(
                    "SELECT status FROM registry_certificate_source_runs WHERE id=%s FOR UPDATE",
                    (run_id,),
                )
                row = await cur.fetchone()
                if not row:
                    raise LookupError("告知书读取任务不存在")
                status = str(row[0])
                if status not in ACTIVE_STATUSES and status != "completed":
                    if restart:
                        await cur.execute(
                            "DELETE FROM registry_certificate_source_pages WHERE run_id=%s",
                            (run_id,),
                        )
                        counters = "current_page=0,fetched_count=0,accepted_count=0,rejected_count=0,"
                    else:
                        counters = ""
                    await cur.execute(
                        "UPDATE registry_certificate_source_runs SET status='pending',phase='queued',"
                        f"{counters}batch_id=NULL,summary_json=NULL,error_code=NULL,error_message=NULL,"
                        "finished_at=NULL WHERE id=%s",
                        (run_id,),
                    )
            finally:
                await cur.execute("SELECT RELEASE_LOCK(%s)", (CERTIFICATE_SOURCE_LOCK,))
                await cur.fetchone()
    run = await get_certificate_source_run(run_id)
    if not run:
        raise LookupError("告知书读取任务不存在")
    if run["status"] == "pending":
        launch_certificate_source_run(run_id)
    return run


def launch_certificate_source_run(run_id: int) -> None:
    task = asyncio.create_task(run_certificate_source_run(run_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def stop_certificate_source_tasks() -> None:
    tasks = list(_background_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def recover_interrupted_certificate_source_runs() -> int:
    """Keep completed pages and make interrupted runs explicitly resumable."""
    try:
        pool = db_manager.get_pool("registry")
    except ValueError:
        return 0
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE registry_certificate_source_runs "
                "SET status='failed',phase='finished',error_code='service_restarted',"
                "error_message='服务重启，已保留读取进度，可继续读取',finished_at=UTC_TIMESTAMP() "
                "WHERE status IN ('pending','running')"
            )
            return int(cur.rowcount or 0)


async def _create_preview_batch(
    conn,
    rows: list[dict[str, Any]],
    created_by: int | None,
) -> dict[str, Any]:
    classified = classify_certificate_rows(rows)
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    file_hash = hashlib.sha256(canonical).hexdigest()
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id,status FROM registry_source_batches "
                "WHERE source_type='certificate' AND file_sha256=%s",
                (file_hash,),
            )
            existing = await cur.fetchone()
            if existing:
                await conn.rollback()
                return {
                    "batch_id": int(existing[0]),
                    "status": str(existing[1]),
                    "idempotent": True,
                    "total_count": len(rows),
                    "normal_count": classified["normal_count"],
                    "issue_count": classified["issue_count"],
                    "problem_row_count": classified["problem_row_count"],
                    "duplicate_groups": classified["duplicate_groups"],
                    "conflict_groups": classified["conflict_groups"],
                }
            await cur.execute(
                "INSERT INTO registry_source_batches "
                "(source_type,file_name,file_sha256,status,imported_count,candidate_count,conflict_count,created_by) "
                "VALUES ('certificate','房东责任告知书只读接口',%s,'preview',0,%s,%s,%s)",
                (
                    file_hash,
                    classified["normal_count"],
                    classified["problem_row_count"],
                    created_by,
                ),
            )
            batch_id = int(cur.lastrowid)
            source_values = [
                (
                    batch_id,
                    f"房东责任告知书只读接口:{row.get('source_row') or ''}"[:190],
                    "property_certificate",
                    json.dumps(row, ensure_ascii=False, default=str),
                )
                for row in classified["rows"]
            ]
            for offset in range(0, len(source_values), REGISTRY_IMPORT_WRITE_CHUNK):
                await cur.executemany(
                    "INSERT INTO registry_source_records "
                    "(batch_id,source_ref,entity_type,payload_json) VALUES (%s,%s,%s,%s)",
                    source_values[offset:offset + REGISTRY_IMPORT_WRITE_CHUNK],
                )
            issue_values = [
                (
                    batch_id,
                    issue["issue_type"],
                    "certificate",
                    f"房东责任告知书只读接口:{issue['source_ref']}"[:190],
                    issue["entity_key"],
                    json.dumps(issue["payload"], ensure_ascii=False, default=str),
                    issue["reason"],
                )
                for issue in classified["issues"]
            ]
            for offset in range(0, len(issue_values), REGISTRY_IMPORT_WRITE_CHUNK):
                await cur.executemany(
                    "INSERT INTO registry_import_issues "
                    "(batch_id,issue_type,source_type,source_ref,entity_key,payload_json,reason) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    issue_values[offset:offset + REGISTRY_IMPORT_WRITE_CHUNK],
                )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {
        "batch_id": batch_id,
        "status": "preview",
        "idempotent": False,
        "total_count": len(rows),
        "normal_count": classified["normal_count"],
        "issue_count": classified["issue_count"],
        "problem_row_count": classified["problem_row_count"],
        "duplicate_groups": classified["duplicate_groups"],
        "conflict_groups": classified["conflict_groups"],
    }


async def _stored_pages(cur, run_id: int) -> list[tuple]:
    await cur.execute(
        "SELECT page_no,row_count,accepted_count,rejected_count,fingerprint,payload_json "
        "FROM registry_certificate_source_pages WHERE run_id=%s ORDER BY page_no",
        (run_id,),
    )
    return list(await cur.fetchall())


async def _mark_failed(run_id: int, code: str, message: str) -> None:
    pool = db_manager.get_pool("registry")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE registry_certificate_source_runs "
                "SET status='failed',phase='finished',error_code=%s,error_message=%s,"
                "finished_at=UTC_TIMESTAMP() WHERE id=%s",
                (code[:60], message[:500], run_id),
            )


async def run_certificate_source_run(run_id: int) -> None:
    pool = db_manager.get_pool("registry")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE registry_certificate_source_runs "
                "SET status='running',phase='reading',started_at=COALESCE(started_at,UTC_TIMESTAMP()),"
                "finished_at=NULL,error_code=NULL,error_message=NULL "
                "WHERE id=%s AND status='pending'",
                (run_id,),
            )
            if int(cur.rowcount or 0) != 1:
                return
            pages = await _stored_pages(cur, run_id)

        page_map = {int(row[0]): row for row in pages}
        fingerprints = {str(row[4]) for row in pages}
        last_page = max(page_map, default=0)
        start_page = last_page if last_page else 1
        fetched_count = sum(int(row[1] or 0) for row in pages)
        accepted_count = sum(int(row[2] or 0) for row in pages)
        rejected_count = sum(int(row[3] or 0) for row in pages)
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE registry_certificate_source_runs SET current_page=%s,fetched_count=%s,"
                "accepted_count=%s,rejected_count=%s WHERE id=%s",
                (last_page, fetched_count, accepted_count, rejected_count, run_id),
            )

        async for page in iter_certificate_pages(start_page=start_page):
            page_no = int(page["page"])
            fingerprint = str(page["fingerprint"])
            stored = page_map.get(page_no)
            if stored:
                if str(stored[4]) != fingerprint:
                    raise VisitSourceError(
                        "source_changed",
                        "来源数据在断点位置发生变化，请重新读取以避免错位",
                    )
                if page["is_last"]:
                    break
                continue
            if page["raw_count"] == CERTIFICATE_PAGE_SIZE and fingerprint in fingerprints:
                raise VisitSourceError("pagination_repeated", "告知书来源重复返回同一分页，已停止读取")
            next_fetched = fetched_count + int(page["raw_count"])
            next_accepted = accepted_count + len(page["rows"])
            next_rejected = rejected_count + int(page["rejected_count"])
            if next_fetched > settings.VISIT_SOURCE_MAX_RECORDS:
                raise VisitSourceError("too_many_records", "告知书来源记录数超过保护阈值")
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO registry_certificate_source_pages "
                    "(run_id,page_no,row_count,accepted_count,rejected_count,fingerprint,payload_json) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        run_id,
                        page_no,
                        int(page["raw_count"]),
                        len(page["rows"]),
                        int(page["rejected_count"]),
                        fingerprint,
                        json.dumps(page["rows"], ensure_ascii=False, default=str),
                    ),
                )
                await cur.execute(
                    "UPDATE registry_certificate_source_runs "
                    "SET current_page=%s,fetched_count=%s,accepted_count=%s,rejected_count=%s "
                    "WHERE id=%s",
                    (page_no, next_fetched, next_accepted, next_rejected, run_id),
                )
            page_map[page_no] = (
                page_no,
                int(page["raw_count"]),
                len(page["rows"]),
                int(page["rejected_count"]),
                fingerprint,
                page["rows"],
            )
            fingerprints.add(fingerprint)
            fetched_count = next_fetched
            accepted_count = next_accepted
            rejected_count = next_rejected
            if page["is_last"]:
                break

        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE registry_certificate_source_runs SET phase='classifying' WHERE id=%s",
                (run_id,),
            )
            pages = await _stored_pages(cur, run_id)
        rows: list[dict[str, Any]] = []
        for page in pages:
            for row in _json(page[5], []):
                materialized = dict(row)
                materialized["source_row"] = len(rows) + 1
                rows.append(materialized)
        if not rows:
            raise VisitSourceError("scope_or_schema", "没有通过派出所、社区和地址校验的告知书记录")

        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT requested_by FROM registry_certificate_source_runs WHERE id=%s",
                (run_id,),
            )
            requester = await cur.fetchone()
            await cur.execute(
                "UPDATE registry_certificate_source_runs SET phase='writing_preview' WHERE id=%s",
                (run_id,),
            )
        result = await _create_preview_batch(
            conn,
            rows,
            int(requester[0]) if requester and requester[0] is not None else None,
        )
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM registry_certificate_source_pages WHERE run_id=%s",
                (run_id,),
            )
            await cur.execute(
                "UPDATE registry_certificate_source_runs "
                "SET status='completed',phase='finished',batch_id=%s,summary_json=%s,finished_at=UTC_TIMESTAMP(),"
                "error_code=NULL,error_message=NULL WHERE id=%s",
                (result["batch_id"], json.dumps(result, ensure_ascii=False), run_id),
            )
    except asyncio.CancelledError:
        await _mark_failed(
            run_id,
            "service_stopped",
            "服务停止，已保留读取进度，可继续读取",
        )
        raise
    except VisitSourceError as exc:
        await _mark_failed(run_id, exc.code, exc.message)
    except Exception as exc:
        print(f"[REGISTRY_CERTIFICATE] run={run_id} failed: {type(exc).__name__}")
        await _mark_failed(run_id, "internal_error", "告知书后台读取失败，请稍后继续读取")
    finally:
        pool.release(conn)
