"""Synthetic real-MySQL acceptance checks for online-summary increments.

This runner is intended for the isolated release-shadow Compose project only.
It deliberately reuses ``verify.guard`` before importing or initializing the
application database layer.  All fixtures are synthetic and retained for
diagnosis; this script never performs cleanup or connects to a production
host.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
from datetime import date, timedelta
from pathlib import Path


ARTIFACT = Path("/artifacts/summary-verify.json")
PARSER_TYPE = "疑似返苏"
TIMEZONE = "Asia/Shanghai"


class CheckFailure(RuntimeError):
    """A safe, non-sensitive acceptance failure."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailure(message)


def key_for(token: str, label: str) -> str:
    return hashlib.sha256(f"{token}:{label}".encode("utf-8")).hexdigest()[:32]


def row_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


async def pool_query(pool, sql: str, params=(), *, many: bool = False):
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            if many:
                return await cur.fetchall()
            return await cur.fetchone()
    finally:
        pool.release(conn)


async def pool_execute(pool, sql: str, params=()) -> int:
    """Execute one DML statement without attempting to fetch a result row."""
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            rowcount = int(cur.rowcount or 0)
            await conn.commit()
            return rowcount
    finally:
        pool.release(conn)


async def create_fixture(token: str, *, label: str, state: str, revision: int,
                         business_date: date, inspector: str, community: str,
                         ) -> dict:
    """Create one complete canonical local-source task and its responsibility."""
    from database import db_manager
    from services.local_source import create_local_source_row
    from services.online_summary_updates import enqueue_online_summary_update

    online_pool = db_manager.get_pool("online_data")
    values = {
        "下发日期": business_date.isoformat(),
        "截止日期": business_date.isoformat(),
        "核查人": inspector,
        "社区": community,
        "姓名": f"synthetic-{label}",
        "身份证号码": f"SYNTHETIC-ID-{token}-{label}",
        "联系号码": f"SYNTHETIC-PHONE-{token}-{label}",
        "高频抓拍小区": "",
        "现住址": "",
        "核查反馈": state,
        "研判": "",
        "二次核查结果": "",
    }
    source_ref = f"shadow-summary:{token}:{label}"
    conn = await online_pool.acquire()
    try:
        async with conn.cursor() as cur:
            await conn.begin()
            try:
                source = await create_local_source_row(
                    cur,
                    PARSER_TYPE,
                    values,
                    source_kind="local_table",
                    source_ref=source_ref,
                )
                task_id = int(source["local_task_id"])
                row_key = str(source["row_key"])
                await cur.execute(
                    "INSERT INTO _task_assignment_responsibilities "
                    "(parser_type,row_key,first_community,first_inspector,"
                    "captured_by,capture_source) VALUES (%s,%s,%s,%s,NULL,'shadow_verify') "
                    "ON DUPLICATE KEY UPDATE first_community=first_community",
                    (PARSER_TYPE, row_key, community, inspector),
                )
                await enqueue_online_summary_update(
                    cur,
                    task_id=task_id,
                    parser_type=PARSER_TYPE,
                    row_key=row_key,
                    revision=revision,
                    business_date=business_date,
                    operation_id=f"shadow-summary-{token}-{label}-{revision}",
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
    finally:
        online_pool.release(conn)
    return {
        "task_id": task_id,
        "row_key": row_key,
        "source_id": int(source["id"]),
        "source_ref": source_ref,
        "values": values,
    }


async def mutate_fixture(token: str, fixture: dict, *, state: str,
                         revision: int, business_date: date,
                         inspector: str, community: str) -> None:
    """Change only synthetic local data and enqueue it in one transaction."""
    from database import db_manager
    from services.local_source import local_row_hash, stable_json
    from services.online_summary_updates import enqueue_online_summary_update

    pool = db_manager.get_pool("online_data")
    values = dict(fixture["values"])
    values.update({"社区": community, "核查人": inspector, "核查反馈": state})
    encoded = stable_json(values)
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await conn.begin()
            try:
                await cur.execute(
                    "UPDATE t_suspect_return SET "
                    "下发日期=%s,截止日期=%s,核查人=%s,社区=%s,姓名=%s,"
                    "身份证号码=%s,联系号码=%s,高频抓拍小区=%s,现住址=%s,"
                    "核查反馈=%s,研判=%s,二次核查结果=%s WHERE id=%s",
                    tuple(values[field] for field in (
                        "下发日期", "截止日期", "核查人", "社区", "姓名",
                        "身份证号码", "联系号码", "高频抓拍小区", "现住址",
                        "核查反馈", "研判", "二次核查结果",
                    )) + (fixture["task_id"],),
                )
                await cur.execute(
                    "UPDATE _online_source_rows SET revision=%s,row_hash=%s,"
                    "values_json=%s,physical_row=%s,source_ref=%s,archived_at=NULL,"
                    "refreshed_at=UTC_TIMESTAMP() WHERE id=%s",
                    (
                        revision,
                        local_row_hash(values),
                        encoded,
                        fixture["task_id"],
                        fixture["source_ref"],
                        fixture["source_id"],
                    ),
                )
                await cur.execute(
                    "UPDATE _local_source_records SET values_json=%s,content_hash=%s,"
                    "revision=%s,status='active',archived_at=NULL,"
                    "updated_at=UTC_TIMESTAMP() WHERE source_kind='local_table' "
                    "AND source_ref=%s",
                    (
                        encoded,
                        local_row_hash(values),
                        revision,
                        fixture["source_ref"],
                    ),
                )
                await enqueue_online_summary_update(
                    cur,
                    task_id=fixture["task_id"],
                    parser_type=PARSER_TYPE,
                    row_key=fixture["row_key"],
                    revision=revision,
                    business_date=business_date,
                    operation_id=f"shadow-summary-{token}-{fixture['row_key'][:6]}-{revision}",
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
    finally:
        pool.release(conn)


async def process_once_and_require(expected: int = 1) -> None:
    from services.online_summary_updates import process_online_summary_updates_once

    processed = await process_online_summary_updates_once(limit=50)
    require(processed >= expected, "summary worker did not process expected trigger")


async def seed_organization(token: str) -> tuple[str, str, int]:
    from database import db_manager

    community = f"ShadowCommunity-{token}"
    inspector = f"ShadowInspector-{token}"
    platform_pool = db_manager.get_pool("platform")
    conn = await platform_pool.acquire()
    try:
        async with conn.cursor() as cur:
            await conn.begin()
            try:
                await cur.execute(
                    "INSERT INTO _communities (name,is_active) VALUES (%s,1) "
                    "ON DUPLICATE KEY UPDATE is_active=1",
                    (community,),
                )
                await cur.execute("SELECT id FROM _communities WHERE name=%s", (community,))
                community_id = int((await cur.fetchone())[0])
                await cur.execute(
                    "INSERT INTO _departments (name,department_type,community_id,is_active) "
                    "VALUES (%s,'community',%s,1) ON DUPLICATE KEY UPDATE "
                    "community_id=VALUES(community_id),is_active=1",
                    (community, community_id),
                )
                await cur.execute("SELECT id FROM _departments WHERE community_id=%s", (community_id,))
                department_id = int((await cur.fetchone())[0])
                await cur.execute(
                    "INSERT INTO _grid_members (name,community,department_id,position,status) "
                    "VALUES (%s,%s,%s,'组员','在岗') ON DUPLICATE KEY UPDATE "
                    "community=VALUES(community),department_id=VALUES(department_id),"
                    "position='组员',status='在岗'",
                    (inspector, community, department_id),
                )
                await cur.execute("SELECT id FROM _grid_members WHERE name=%s", (inspector,))
                member_id = int((await cur.fetchone())[0])
                await cur.execute(
                    "INSERT IGNORE INTO _grid_member_department_links "
                    "(member_id,department_id,sort_order) VALUES (%s,%s,0)",
                    (member_id, department_id),
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
    finally:
        platform_pool.release(conn)
    return community, inspector, member_id


async def read_ledger(fixture: dict, report_date: date) -> tuple:
    from database import db_manager

    pool = db_manager.get_pool("daily_report")
    return await pool_query(
        pool,
        "SELECT task_state,effective_workload,source_revision,community,inspector "
        "FROM _daily_task_ledger WHERE report_date=%s AND parser_type=%s AND row_key=%s",
        (report_date, PARSER_TYPE, fixture["row_key"]),
    )


async def main() -> None:
    results = {
        "status": "failed",
        "checks": [],
        "fixtures_retained": True,
    }
    initialized = False
    failure: BaseException | None = None
    error_step = "startup"
    try:
        # This must remain the first environment/database action.
        from verify import guard

        error_step = "guard_before_init"
        run_id = await guard()
        results["run_id"] = run_id
        sys.path.insert(0, "/app")
        from database import close_db, db_manager, init_db
        from services.business_time import current_business_date

        error_step = "init_db"
        await init_db()
        initialized = True
        error_step = "guard_after_init"
        await guard()
        results["checks"].append("shadow_guard_before_and_after_init")

        token = hashlib.sha256(
            f"{run_id}:{time.time_ns()}".encode("utf-8")
        ).hexdigest()[:12]
        today = current_business_date(TIMEZONE)
        yesterday = today - timedelta(days=1)
        error_step = "seed_organization"
        community, first_inspector, member_id = await seed_organization(token)

        error_step = "inspect_materialization"
        first_table_name = f"{today.isoformat()}_daily_suspectReturn_inspector"
        daily_pool = db_manager.get_pool("daily_report")
        table_row = await pool_query(
            daily_pool,
            "SELECT table_name FROM _daily_report_meta WHERE table_name=%s",
            (first_table_name,),
        )
        had_materialized_table = table_row is not None

        error_step = "create_same_day_fixture"
        same_day = await create_fixture(
            token, label="same-day", state="", revision=1,
            business_date=today, inspector=first_inspector, community=community,
        )
        error_step = "process_same_day_initial"
        await process_once_and_require()
        results["checks"].append("first_day_worker_created_or_reused_materialization")

        error_step = "process_same_day_unable"
        await mutate_fixture(
            token, same_day, state="无法核实", revision=2, business_date=today,
            inspector=first_inspector, community=community,
        )
        await process_once_and_require()
        error_step = "process_same_day_completed"
        await mutate_fixture(
            token, same_day, state="无需登记", revision=3, business_date=today,
            inspector=first_inspector, community=community,
        )
        await process_once_and_require()

        error_step = "verify_same_day_ledger"
        current_ledger = await read_ledger(same_day, today)
        require(current_ledger is not None, "same-day ledger row missing")
        require(current_ledger[0] == "completed", "same-day final state mismatch")
        require(int(current_ledger[1]) == 1, "same-day workload exceeded one")
        require(int(current_ledger[2]) == 3, "latest same-day revision missing")

        # Replay an older completed trigger through the real worker.  It must
        # be skipped by the authoritative revision fence without overwriting
        # the ledger row.
        error_step = "replay_stale_revision"
        online_pool = db_manager.get_pool("online_data")
        stale_id = await pool_query(
            online_pool,
            "SELECT id FROM _online_summary_updates WHERE task_id=%s AND revision=2 "
            "AND business_date=%s",
            (same_day["task_id"], today),
        )
        require(stale_id is not None, "stale trigger fixture missing")
        await pool_execute(
            online_pool,
            "UPDATE _online_summary_updates SET status='pending',next_attempt_at=NULL "
            "WHERE id=%s",
            (stale_id[0],),
        )
        await process_once_and_require()
        error_step = "verify_stale_revision_fence"
        after_stale = await read_ledger(same_day, today)
        require(after_stale[2] == 3 and after_stale[1] == 1, "old revision overwrote ledger")
        results["checks"].append("same_day_duplicate_and_old_revision_fence")

        # Change the source inspector after first assignment; the durable
        # responsibility record must keep the original inspector in ledger.
        error_step = "verify_first_inspector"
        await mutate_fixture(
            token, same_day, state="无需登记", revision=4, business_date=today,
            inspector=f"ShadowOther-{token}", community=community,
        )
        await process_once_and_require()
        first_owner_ledger = await read_ledger(same_day, today)
        require(first_owner_ledger[4] == first_inspector, "first inspector responsibility changed")
        results["checks"].append("first_inspector_is_preserved")

        error_step = "create_cross_day_fixture"
        cross_day = await create_fixture(
            token, label="cross-day", state="无法核实", revision=1,
            business_date=yesterday, inspector=first_inspector, community=community,
        )
        error_step = "process_cross_day_initial"
        await process_once_and_require()
        error_step = "process_cross_day_completed"
        await mutate_fixture(
            token, cross_day, state="无需登记", revision=2, business_date=today,
            inspector=first_inspector, community=community,
        )
        await process_once_and_require()
        error_step = "verify_cross_day_ledger"
        yesterday_ledger = await read_ledger(cross_day, yesterday)
        today_ledger = await read_ledger(cross_day, today)
        require(yesterday_ledger[1] == 1 and today_ledger[1] == 1,
                "cross-day workload did not cap at one per day")
        require(int(yesterday_ledger[1]) + int(today_ledger[1]) == 2,
                "cross-day workload exceeded two total transitions")
        results["checks"].append("cross_day_workload_cap")

        # Read the public summary directly after the worker.  No snapshot or
        # summary marker is fabricated here; the reader must consume the
        # worker's incremental materialization and real attendance service.
        error_step = "read_public_summary"
        from services.report_builders.summary import get_summary

        summary = await get_summary(today.isoformat())
        require(summary.get("exists") is True, "public summary read failed")
        require(summary.get("attendance", {}).get("complete") is True,
                "public summary attendance is incomplete")
        community_rows = summary.get("community", {}).get("data", [])
        require(any(int(row.get("数据总数") or 0) >= 2 for row in community_rows),
                "public summary does not include synthetic tasks")
        results["checks"].append("public_summary_read_after_worker_and_attendance")

        # A business transaction rollback must remove its durable trigger.
        error_step = "verify_business_rollback"
        conn = await online_pool.acquire()
        try:
            async with conn.cursor() as cur:
                await conn.begin()
                await cur.execute(
                    "INSERT INTO _online_summary_updates "
                    "(task_id,parser_type,row_key,revision,business_date,operation_id,status) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'pending')",
                    (999000000, PARSER_TYPE, key_for(token, "rollback"), 1, today,
                     f"shadow-summary-{token}-rollback"),
                )
                await conn.rollback()
        finally:
            online_pool.release(conn)
        rollback_row = await pool_query(
            online_pool,
            "SELECT COUNT(*) FROM _online_summary_updates WHERE task_id=%s",
            (999000000,),
        )
        require(int(rollback_row[0]) == 0, "business rollback left summary trigger")
        results["checks"].append("business_transaction_rollback")

        results["status"] = "passed"
        results["fixture"] = {
            "task_count": 2,
            "business_date": today.isoformat(),
            "previous_business_date": yesterday.isoformat(),
            "materialized_table_preexisting": had_materialized_table,
        }
    except BaseException as exc:
        results["error_type"] = type(exc).__name__
        results["error_step"] = error_step
        results["error_code"] = (
            "acceptance_check_failed" if isinstance(exc, CheckFailure)
            else "database_timeout" if isinstance(exc, (TimeoutError, asyncio.TimeoutError))
            else "summary_verification_failed"
        )
        failure = exc
    finally:
        if initialized:
            try:
                await close_db()
            except Exception:
                # The evidence file must remain available even if pool
                # shutdown itself is unhealthy.  Do not expose driver text.
                results["close_status"] = "error"
        ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
        ARTIFACT.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(json.dumps(results, ensure_ascii=False))
    if failure is not None:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
