"""统一外部来源调度器。

调度器只负责计划、租约和运行合同；具体来源仍复用已有只读 runner。所有
计划默认关闭，旧 scheduler 可以继续兼容读取历史配置，但不会读取本表的计划。
"""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from typing import Any

from services.external_acquisition_jobs import create_job, get_job
from services.external_acquisition_registry import (
    claim_due_schedule,
    mark_schedule_result,
    notify_schedule_failure,
    sanitize_source_params,
    SOURCE_ALIASES, source_definition,
)

_tasks: set[asyncio.Task] = set()


def _date_param(params: dict[str, Any], key: str, fallback: date) -> date:
    value = params.get(key)
    if value in (None, ""):
        return fallback
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{key} 必须是 YYYY-MM-DD") from exc


async def _previous_business_date() -> date:
    from database import db_manager
    from services.business_time import get_business_date

    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            current = await get_business_date(cur)
    return current - timedelta(days=1)


async def _wait_certificate(run_id: int, ctx) -> dict[str, Any]:
    from services.registry_certificate_jobs import get_certificate_source_run

    for _ in range(720):
        current = await get_certificate_source_run(run_id)
        if not current:
            return {"status": "failed", "safety_gate_code": "certificate_run_missing"}
        await ctx.update(
            phase=str(current.get("phase") or "reading"),
            current=int(current.get("current_page") or 0),
            total=current.get("total_pages"),
            message="责任告知书自动读取中",
        )
        if current.get("status") not in {"pending", "running"}:
            if current.get("status") == "completed":
                return {"status": "success", "auto_commit_status": "committed", "certificate_run_id": run_id}
            return {"status": "failed", "auto_commit_status": "skipped", "safety_gate_code": current.get("error_code") or "certificate_source_failed"}
        await asyncio.sleep(1)
    return {"status": "failed", "auto_commit_status": "skipped", "safety_gate_code": "certificate_timeout"}


async def run_registered_source(source_key: str, requested_by: int | None, params: dict[str, Any],
                                *, schedule_id: int | None = None,
                                trigger_source: str = "manual") -> tuple[dict[str, Any], bool]:
    source_key = SOURCE_ALIASES.get(source_key, source_key)
    definition = source_definition(source_key)
    params = sanitize_source_params(source_key, params)

    async def runner(ctx):
        if source_key == "qmf_source":
            from services.qmf_source_sync import run_qmf_source_sync
            result = await run_qmf_source_sync(ctx, require_complete=True)
            result.setdefault("auto_commit_status", "committed" if result.get("status", "success") == "success" else "skipped")
            return result
        if source_key == "residence_full_scan":
            from services.residence_status_scan import run_residence_full_scan_job
            result = await run_residence_full_scan_job(ctx)
            result.setdefault("auto_commit_status", "committed" if result.get("status", "success") == "success" else "skipped")
            return result
        if source_key == "registry_certificate":
            from services.registry_certificate_jobs import create_certificate_source_run
            run, _ = await create_certificate_source_run(requested_by, trigger_source=trigger_source)
            return await _wait_certificate(int(run["id"]), ctx)
        if source_key == "qmf_status_scan":
            from services.qmf_status_scan import create_status_scan_run, status_scan_payload, launch_status_scan
            run_id, total = await create_status_scan_run(trigger_source=trigger_source, requested_by=requested_by)
            launch_status_scan(run_id)
            for _ in range(720):
                current = await status_scan_payload(run_id)
                if current and current.get("total_count"):
                    await ctx.update(phase="checking", current=current.get("processed_count"), total=current.get("total_count"), message="全民防反馈核对中")
                if current and current.get("status") not in {"queued", "running"}:
                    status = str(current.get("status"))
                    complete = status == "completed" and not int(current.get("error_count") or 0)
                    return {"status": "success" if complete else "failed", "auto_commit_status": "committed" if complete else "skipped", "safety_gate_code": None if complete else "qmf_scan_incomplete", "qmf_scan_run_id": run_id}
                await asyncio.sleep(1)
            return {"status": "failed", "auto_commit_status": "skipped", "safety_gate_code": "qmf_scan_timeout"}
        if source_key == "venue_cloud":
            from services.venue_cloud import pull_venue_cloud_now
            result = await pull_venue_cloud_now()
            return {"status": "success", "auto_commit_status": "committed", "pull": {key: result.get(key) for key in ("pulled", "accepted", "rejected", "retry_later", "uncertain") if key in result}}
        if source_key == "visit_source":
            from services.business_time import get_business_timezone_name
            from services.visit_source import VisitSourceError, commit_rows, fetch_rows, preview_diff
            from database import db_manager

            business_date = await _previous_business_date()
            start_date = _date_param(params, "start_date", business_date)
            end_date = _date_param(params, "end_date", start_date)
            if start_date > end_date:
                raise ValueError("开始日期不能晚于结束日期")
            try:
                detail, rating = await asyncio.gather(
                    fetch_rows("detail", start_date, end_date),
                    fetch_rows("rating", start_date, end_date),
                )
            except VisitSourceError as exc:
                return {"status": "failed", "auto_commit_status": "skipped", "safety_gate_code": exc.code}
            if any(int(item.get("issue_count") or 0) for item in (detail, rating)):
                return {"status": "failed", "auto_commit_status": "skipped", "safety_gate_code": "source_incomplete"}
            pool = db_manager.get_pool("online_data")
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    timezone_name = await get_business_timezone_name(cur)
                detail_diff = await preview_diff(conn, kind="detail", rows=detail["rows"], timezone_name=timezone_name)
                rating_diff = await preview_diff(
                    conn, kind="rating", rows=rating["rows"], timezone_name=timezone_name,
                    projected_detail_rows=detail["rows"],
                )
                if any(int(rating_diff.get(key) or 0) for key in ("unmatched", "ambiguous")):
                    return {"status": "failed", "auto_commit_status": "skipped", "safety_gate_code": "visit_match_conflict", "detail_count": len(detail["rows"]), "rating_count": len(rating["rows"])}
                await conn.begin()
                try:
                    detail_result = await commit_rows(
                        conn, kind="detail", rows=detail["rows"], start_date=start_date,
                        user_id=0, source_type="scheduled_source", source_run_id=ctx.run_id,
                        manage_transaction=False,
                    )
                    if detail_result.get("status") != "success":
                        raise RuntimeError("visit_detail_commit_failed")
                    rating_result = await commit_rows(
                        conn, kind="rating", rows=rating["rows"], start_date=start_date,
                        user_id=0, source_type="scheduled_source", source_run_id=ctx.run_id,
                        manage_transaction=False,
                    )
                    if rating_result.get("status") != "success":
                        raise RuntimeError("visit_rating_commit_failed")
                    await conn.commit()
                except RuntimeError as exc:
                    await conn.rollback()
                    return {"status": "failed", "auto_commit_status": "skipped", "safety_gate_code": str(exc)}
                except Exception:
                    await conn.rollback()
                    raise
            return {"status": "success", "auto_commit_status": "committed", "detail_count": len(detail["rows"]), "rating_count": len(rating["rows"]), "detail_diff": detail_diff, "rating_diff": rating_diff}
        if source_key == "code_summary":
            from database import db_manager
            from routers.code_summaries import (
                DateRangeRequest, _commit_source, _directories, _manual_location_labels,
            )
            from services.code_summary import aggregate_rows, count_fullchain_registrations, fetch_sources, CodeSummaryError

            business_date = await _previous_business_date()
            start_date = _date_param(params, "start_date", business_date)
            end_date = _date_param(params, "end_date", start_date)
            if start_date > end_date:
                raise ValueError("开始日期不能晚于结束日期")
            payload = DateRangeRequest(start_date=start_date, end_date=end_date)
            pool = db_manager.get_pool("online_data")
            async with pool.acquire() as conn:
                personnel, places = await _directories(conn)
                labels = await _manual_location_labels(conn, "peace")
                fetched = await fetch_sources(start_date, end_date)
                registration_counts: dict[date, int] = {}
                if not fetched["peace"].get("error"):
                    try:
                        registration_counts = await count_fullchain_registrations(conn, start_date, end_date)
                    except CodeSummaryError as exc:
                        fetched["peace"] = {"error": exc}
                aggregated: dict[str, dict[str, Any]] = {}
                for source in ("peace", "manager"):
                    source_result = fetched[source]
                    if source_result.get("error"):
                        error = source_result["error"]
                        return {"status": "failed", "auto_commit_status": "skipped", "safety_gate_code": getattr(error, "code", "source_failed")}
                    aggregate = aggregate_rows(
                        source, source_result["rows"], start_date, end_date,
                        personnel_names=personnel, place_names=places,
                        manual_labels=labels,
                        new_registration_counts=registration_counts if source == "peace" else None,
                    )
                    if any(int(aggregate.get(key) or 0) for key in ("excluded_count", "unclassified_count", "invalid_time_count")):
                        return {"status": "failed", "auto_commit_status": "skipped", "safety_gate_code": "code_summary_incomplete"}
                    aggregated[source] = aggregate
                await conn.begin()
                try:
                    for source, aggregate in aggregated.items():
                        await _commit_source(conn, source=source, user_id=0, payload=payload, result=aggregate, manage_transaction=False)
                    await conn.commit()
                except Exception:
                    await conn.rollback()
                    raise
            return {"status": "success", "auto_commit_status": "committed", "sources": {source: {"raw_count": value["raw_count"], "valid_count": value["valid_count"]} for source, value in aggregated.items()}}
        # Unknown or preview-only sources are fail-closed.  They remain visible
        # in the catalog without silently converting a manual confirmation into
        # an automatic write.
        return {"status": "failed", "auto_commit_status": "skipped", "safety_gate_code": "manual_confirmation_required", "message": f"{definition.name} 需要先完成现有预览确认流程"}

    dedupe = f"schedule:{schedule_id}" if schedule_id else f"manual:{source_key}"
    return await create_job(
        source_key,
        requested_by,
        params,
        runner,
        dedupe_key=dedupe,
        schedule_id=schedule_id,
        trigger_source=trigger_source,
    )


async def _run_claimed(schedule: dict[str, Any]) -> None:
    key = str(schedule["source_key"])
    try:
        run, _ = await run_registered_source(key, None, schedule.get("params") or {}, schedule_id=int(schedule["id"]), trigger_source="scheduled")
        long_running_reported = False
        elapsed = 0
        missing_polls = 0
        while True:
            current = await get_job(int(run["id"]))
            if current is None:
                missing_polls += 1
                if missing_polls >= 3:
                    await mark_schedule_result(int(schedule["id"]), "failed", "source_run_missing")
                    await notify_schedule_failure(key, int(run.get("id") or 0), "source_run_missing")
                    return
            else:
                missing_polls = 0
            if current and current.get("status") not in {"queued", "running"}:
                run = current
                break
            elapsed += 1
            if elapsed == 720 and not long_running_reported:
                # Keep the schedule leased while the source runner is still
                # alive.  A failed status here would allow a second process to
                # start the same source and could overwrite a later success.
                await mark_schedule_result(int(schedule["id"]), "running", "source_run_long_running")
                long_running_reported = True
            await asyncio.sleep(1)
        status = str(run.get("status") or "failed")
        if status in {"failed", "warning"}:
            code = str(run.get("safety_gate_code") or run.get("error_code") or "source_run_failed")
            await mark_schedule_result(int(schedule["id"]), status, code)
            await notify_schedule_failure(key, int(run.get("id") or 0), code)
        else:
            await mark_schedule_result(int(schedule["id"]), status, None)
    except Exception as exc:  # noqa: BLE001 - scheduler must keep serving other sources
        await mark_schedule_result(int(schedule["id"]), "failed", type(exc).__name__[:80])


async def run_external_acquisition_scheduler() -> None:
    while True:
        try:
            schedule = await claim_due_schedule()
            if schedule:
                task = asyncio.create_task(_run_claimed(schedule))
                _tasks.add(task)
                task.add_done_callback(_tasks.discard)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[EXTERNAL_SCHEDULE] scheduler failed: {type(exc).__name__}")
        await asyncio.sleep(30)


async def stop_external_acquisition_scheduler() -> None:
    tasks = list(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
