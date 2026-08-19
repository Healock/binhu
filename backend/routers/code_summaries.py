"""Peace Code and Manager Code daily summary APIs."""

from __future__ import annotations

import json
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, model_validator

from database import get_db
from deps import require_permission
from services.audit import record_admin_audit, request_audit_fields
from services.code_summary import (
    CLASSIFIER_VERSION,
    CodeSummaryError,
    SOURCE_META,
    aggregate_rows,
    fetch_sources,
    normalize_label,
)
from services.permissions import VISIT_SOURCE_MANAGE, VISIT_SUMMARY_VIEW


router = APIRouter(prefix="/api/code-summaries", tags=["平安码与管家码汇总"])
FETCH_LOCK = "binhu_code_summary_fetch"
MAX_RANGE_DAYS = 31


def _json_value(value):
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return value or {}


class DateRangeRequest(BaseModel):
    start_date: date
    end_date: date

    @model_validator(mode="after")
    def validate_range(self):
        if self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")
        if (self.end_date - self.start_date).days + 1 > MAX_RANGE_DAYS:
            raise ValueError(f"单次最多获取 {MAX_RANGE_DAYS} 天")
        return self


class SummarySearchRequest(DateRangeRequest):
    source: Literal["peace", "manager"]


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6) if denominator else 0.0


def _public_metrics(source: str, row: dict) -> dict:
    result = {
        "business_date": row["business_date"],
        "raw_count": int(row.get("raw_count") or 0),
        "total_people": int(row.get("total_people") or 0),
        "instruction_count": int(row.get("instruction_count") or 0),
        "excluded_identity_count": int(row.get("excluded_identity_count") or 0),
        "duplicate_removed_count": int(row.get("duplicate_removed_count") or 0),
        "version": int(row.get("version") or 0),
        "run_id": int(row.get("run_id") or 0),
    }
    result["effective_warning_rate"] = _rate(
        result["instruction_count"], result["total_people"]
    )
    if source == "peace":
        result.update({
            "patrol_scan_count": int(row.get("patrol_scan_count") or 0),
            "dispatch_hall_scan_count": int(row.get("dispatch_hall_scan_count") or 0),
            "household_hall_scan_count": int(row.get("household_hall_scan_count") or 0),
            "social_scan_count": int(row.get("social_scan_count") or 0),
            "unclassified_scan_count": int(row.get("unclassified_scan_count") or 0),
            "new_registration_count": int(row.get("new_registration_count") or 0),
        })
        result["effective_scan_rate"] = _rate(
            result["new_registration_count"], result["total_people"]
        )
    else:
        result["active_accounts"] = int(row.get("active_accounts") or 0)
    return result


def _total(source: str, rows: list[dict]) -> dict:
    keys = [
        "raw_count", "total_people", "instruction_count",
        "excluded_identity_count", "duplicate_removed_count",
    ]
    if source == "peace":
        keys.extend([
            "patrol_scan_count", "dispatch_hall_scan_count",
            "household_hall_scan_count", "social_scan_count",
            "unclassified_scan_count", "new_registration_count",
        ])
    else:
        keys.append("active_accounts")
    result = {key: sum(int(row.get(key) or 0) for row in rows) for key in keys}
    result["business_date"] = "总计"
    result["effective_warning_rate"] = _rate(
        result["instruction_count"], result["total_people"]
    )
    if source == "peace":
        result["effective_scan_rate"] = _rate(
            result["new_registration_count"], result["total_people"]
        )
    return result


async def _directories(conn) -> tuple[set[str], set[str]]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT name FROM _grid_members "
            "WHERE status='在岗' AND TRIM(COALESCE(name,''))<>''"
        )
        personnel = {normalize_label(row[0]) for row in await cur.fetchall()}
        await cur.execute(
            "SELECT name,aliases_json FROM _police_address_entries WHERE enabled=1"
        )
        places: set[str] = set()
        for name, aliases_json in await cur.fetchall():
            places.add(normalize_label(name))
            try:
                aliases = json.loads(aliases_json) if isinstance(aliases_json, str) else aliases_json or []
            except (TypeError, ValueError, json.JSONDecodeError):
                aliases = []
            places.update(normalize_label(value) for value in aliases)
    personnel.discard("")
    places.discard("")
    return personnel, places


async def _insert_failed_run(
    conn,
    *,
    source: str,
    user_id: int,
    payload: DateRangeRequest,
    error: CodeSummaryError,
) -> int:
    async with conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO _code_summary_runs "
            "(source_kind,status,requested_by,requested_start_date,requested_end_date,"
            "source_endpoint,classifier_version,error_code,error_message,finished_at) "
            "VALUES (%s,'failed',%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP())",
            (
                source, user_id, payload.start_date, payload.end_date,
                SOURCE_META[source]["endpoint"], CLASSIFIER_VERSION,
                error.code[:60], error.message[:500],
            ),
        )
        run_id = int(cur.lastrowid)
    await conn.commit()
    return run_id


async def _commit_source(
    conn,
    *,
    source: str,
    user_id: int,
    payload: DateRangeRequest,
    result: dict,
) -> tuple[int, str]:
    status = "warning" if (
        result["excluded_count"]
        or result["unclassified_count"]
        or result.get("invalid_time_count", 0)
    ) else "success"
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO _code_summary_runs "
                "(source_kind,status,requested_by,requested_start_date,requested_end_date,"
                "source_endpoint,raw_count,valid_count,excluded_count,duplicate_count,"
                "unclassified_count,source_hash,classifier_version,summary_json,finished_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP())",
                (
                    source, status, user_id, payload.start_date, payload.end_date,
                    SOURCE_META[source]["endpoint"], result["raw_count"],
                    result["valid_count"], result["excluded_count"],
                    result["duplicate_count"], result["unclassified_count"],
                    result["source_hash"], CLASSIFIER_VERSION,
                    json.dumps({
                        "days": len(result["rows"]),
                        "raw_count": result["raw_count"],
                        "valid_count": result["valid_count"],
                        "excluded_count": result["excluded_count"],
                        "duplicate_count": result["duplicate_count"],
                        "unclassified_count": result["unclassified_count"],
                        "invalid_time_count": result.get("invalid_time_count", 0),
                    }, ensure_ascii=False),
                ),
            )
            run_id = int(cur.lastrowid)
            for item in result["rows"]:
                business_date = date.fromisoformat(item["date"])
                await cur.execute(
                    "SELECT COALESCE(MAX(version_no),0) FROM _code_daily_snapshots "
                    "WHERE source_kind=%s AND business_date=%s",
                    (source, business_date),
                )
                version = int((await cur.fetchone())[0] or 0) + 1
                await cur.execute(
                    "INSERT INTO _code_daily_snapshots "
                    "(source_kind,business_date,version_no,run_id,raw_count,total_people,"
                    "patrol_scan_count,dispatch_hall_scan_count,household_hall_scan_count,"
                    "social_scan_count,unclassified_scan_count,active_accounts,"
                    "instruction_count,new_registration_count,excluded_identity_count,"
                    "duplicate_removed_count,classifier_version) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        source, business_date, version, run_id, item["raw_count"],
                        item["total_people"], item["patrol_scan_count"],
                        item["dispatch_hall_scan_count"], item["household_hall_scan_count"],
                        item["social_scan_count"], item["unclassified_scan_count"],
                        item["active_accounts"], item["instruction_count"],
                        item["new_registration_count"], item["excluded_identity_count"],
                        item["duplicate_removed_count"], CLASSIFIER_VERSION,
                    ),
                )
        await conn.commit()
        return run_id, status
    except Exception:
        await conn.rollback()
        raise


@router.post("/fetch")
async def fetch_code_summaries(
    payload: DateRangeRequest,
    request: Request,
    user: dict = Depends(require_permission(VISIT_SOURCE_MANAGE)),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await cur.execute("SELECT GET_LOCK(%s, 0)", (FETCH_LOCK,))
        lock_row = await cur.fetchone()
    if not lock_row or lock_row[0] != 1:
        raise HTTPException(409, "当前已有平安码或管家码数据正在获取")
    results = []
    try:
        personnel, places = await _directories(conn)
        try:
            fetched = await fetch_sources(payload.start_date, payload.end_date)
        except CodeSummaryError as exc:
            fetched = {source: {"error": exc} for source in ("peace", "manager")}
        for source in ("peace", "manager"):
            source_result = fetched[source]
            if source_result.get("error"):
                error = source_result["error"]
                run_id = await _insert_failed_run(
                    conn, source=source, user_id=int(user["id"]), payload=payload, error=error
                )
                results.append({
                    "source": source, "run_id": run_id, "status": "failed",
                    "error_code": error.code, "error_message": error.message,
                })
                continue
            try:
                aggregated = aggregate_rows(
                    source,
                    source_result["rows"],
                    payload.start_date,
                    payload.end_date,
                    personnel_names=personnel,
                    place_names=places,
                )
                run_id, status = await _commit_source(
                    conn,
                    source=source,
                    user_id=int(user["id"]),
                    payload=payload,
                    result=aggregated,
                )
                results.append({
                    "source": source, "run_id": run_id, "status": status,
                    "raw_count": aggregated["raw_count"],
                    "valid_count": aggregated["valid_count"],
                    "excluded_count": aggregated["excluded_count"],
                    "duplicate_count": aggregated["duplicate_count"],
                    "unclassified_count": aggregated["unclassified_count"],
                    "invalid_time_count": aggregated.get("invalid_time_count", 0),
                })
            except CodeSummaryError as exc:
                run_id = await _insert_failed_run(
                    conn, source=source, user_id=int(user["id"]), payload=payload, error=exc
                )
                results.append({
                    "source": source, "run_id": run_id, "status": "failed",
                    "error_code": exc.code, "error_message": exc.message,
                })
        await record_admin_audit(
            user,
            "code_summary.fetch",
            target_type="code_summary",
            target_name=f"{payload.start_date} 至 {payload.end_date}",
            result="success" if all(item["status"] != "failed" for item in results) else "partial",
            detail={
                "start_date": payload.start_date.isoformat(),
                "end_date": payload.end_date.isoformat(),
                "runs": [{"source": item["source"], "run_id": item["run_id"], "status": item["status"]} for item in results],
            },
            **request_audit_fields(request),
        )
        return {"data": results}
    finally:
        async with conn.cursor() as cur:
            await cur.execute("SELECT RELEASE_LOCK(%s)", (FETCH_LOCK,))
            await cur.fetchone()


@router.post("/search")
async def search_code_summaries(
    payload: SummarySearchRequest,
    _user: dict = Depends(require_permission(VISIT_SUMMARY_VIEW)),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT snapshot.business_date,snapshot.version_no,snapshot.run_id,"
            "snapshot.raw_count,snapshot.total_people,snapshot.patrol_scan_count,"
            "snapshot.dispatch_hall_scan_count,snapshot.household_hall_scan_count,"
            "snapshot.social_scan_count,snapshot.unclassified_scan_count,"
            "snapshot.active_accounts,snapshot.instruction_count,"
            "snapshot.new_registration_count,snapshot.excluded_identity_count,"
            "snapshot.duplicate_removed_count "
            "FROM _code_daily_snapshots snapshot "
            "JOIN (SELECT business_date,MAX(version_no) version_no "
            "FROM _code_daily_snapshots WHERE source_kind=%s "
            "AND business_date BETWEEN %s AND %s GROUP BY business_date) latest "
            "ON latest.business_date=snapshot.business_date "
            "AND latest.version_no=snapshot.version_no "
            "WHERE snapshot.source_kind=%s ORDER BY snapshot.business_date",
            (payload.source, payload.start_date, payload.end_date, payload.source),
        )
        rows = [
            {
                "business_date": row[0].isoformat(), "version": row[1], "run_id": row[2],
                "raw_count": row[3], "total_people": row[4], "patrol_scan_count": row[5],
                "dispatch_hall_scan_count": row[6], "household_hall_scan_count": row[7],
                "social_scan_count": row[8], "unclassified_scan_count": row[9],
                "active_accounts": row[10], "instruction_count": row[11],
                "new_registration_count": row[12], "excluded_identity_count": row[13],
                "duplicate_removed_count": row[14],
            }
            for row in await cur.fetchall()
        ]
        await cur.execute(
            "SELECT id,status,requested_start_date,requested_end_date,raw_count,"
            "valid_count,excluded_count,duplicate_count,unclassified_count,"
            "error_code,error_message,finished_at,created_at,summary_json "
            "FROM _code_summary_runs WHERE source_kind=%s ORDER BY id DESC LIMIT 1",
            (payload.source,),
        )
        latest = await cur.fetchone()
        await cur.execute(
            "SELECT finished_at FROM _code_summary_runs "
            "WHERE source_kind=%s AND status IN ('success','warning') "
            "ORDER BY id DESC LIMIT 1",
            (payload.source,),
        )
        latest_success = await cur.fetchone()
    data = [_public_metrics(payload.source, row) for row in rows]
    latest_summary = _json_value(latest[13]) if latest else {}
    latest_run = None if not latest else {
        "id": int(latest[0]), "status": latest[1],
        "start_date": latest[2].isoformat(), "end_date": latest[3].isoformat(),
        "raw_count": int(latest[4] or 0), "valid_count": int(latest[5] or 0),
        "excluded_count": int(latest[6] or 0), "duplicate_count": int(latest[7] or 0),
        "unclassified_count": int(latest[8] or 0), "error_code": latest[9],
        "error_message": latest[10],
        "finished_at": latest[11].isoformat() + "Z" if latest[11] else None,
        "created_at": latest[12].isoformat() + "Z" if latest[12] else None,
        "invalid_time_count": int(latest_summary.get("invalid_time_count") or 0),
    }
    return {
        "source": payload.source,
        "start_date": payload.start_date.isoformat(),
        "end_date": payload.end_date.isoformat(),
        "columns": (
            ["business_date", "total_people", "patrol_scan_count", "dispatch_hall_scan_count",
             "household_hall_scan_count", "social_scan_count", "unclassified_scan_count",
             "instruction_count", "effective_warning_rate", "new_registration_count",
             "effective_scan_rate"]
            if payload.source == "peace"
            else ["business_date", "total_people", "active_accounts", "instruction_count", "effective_warning_rate"]
        ),
        "data": data,
        "total": _total(payload.source, data),
        "latest_run": latest_run,
        "latest_success_at": (
            latest_success[0].isoformat() + "Z"
            if latest_success and latest_success[0]
            else None
        ),
    }
