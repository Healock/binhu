"""Peace Code and Manager Code daily summary APIs."""

from __future__ import annotations

import json
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from database import get_db
from deps import require_permission
from services.audit import record_admin_audit, request_audit_fields
from services.code_summary import (
    CLASSIFIER_VERSION,
    CodeSummaryError,
    SOURCE_META,
    aggregate_rows,
    count_fullchain_registrations,
    fetch_sources,
    normalize_label,
)
from services.permissions import CODE_SUMMARY_MANAGE, VISIT_SOURCE_MANAGE, VISIT_SUMMARY_VIEW
from services.external_acquisition_jobs import create_job
from database import db_manager


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


LOCATION_CLASSIFICATIONS = {
    "social": "社会面",
    "patrol": "巡防",
    "dispatch_hall": "接警大厅",
    "household_hall": "户政大厅",
    "ignored": "忽略/无效位置",
    "other": "其他",
}


class LocationSearchRequest(DateRangeRequest):
    source: Literal["peace", "manager"] = "peace"
    keyword: str = Field(default="", max_length=255)
    status: Literal["all", "unclassified", "classified"] = "all"
    page: int = 1
    page_size: int = 20

    @model_validator(mode="after")
    def validate_page(self):
        self.page = max(1, self.page)
        self.page_size = min(100, max(1, self.page_size))
        return self


class LocationClassificationItem(BaseModel):
    location_key: str = Field(max_length=255)
    display_name: str = Field(max_length=255)
    classification: Literal[
        "social", "patrol", "dispatch_hall", "household_hall", "ignored", "other"
    ]


class LocationClassificationsRequest(BaseModel):
    source: Literal["peace", "manager"] = "peace"
    items: list[LocationClassificationItem]

    @model_validator(mode="after")
    def validate_items(self):
        if not self.items or len(self.items) > 100:
            raise ValueError("每次最多标记100个位置")
        return self


async def _location_rows(conn, payload: LocationSearchRequest):
    keyword = normalize_label(payload.keyword)
    offset = (payload.page - 1) * payload.page_size
    params: list[object] = [payload.source, payload.start_date, payload.end_date]
    conditions = [
        "c.source_kind=%s", "c.business_date BETWEEN %s AND %s",
        "s.version_no=(SELECT MAX(s2.version_no) FROM _code_daily_snapshots s2 "
        "WHERE s2.source_kind=s.source_kind AND s2.business_date=s.business_date)",
    ]
    if keyword:
        conditions.append("c.location_key LIKE %s")
        params.append(f"%{keyword}%")
    if payload.status == "unclassified":
        conditions.append("COALESCE(label.classification,c.classification)='unclassified'")
    elif payload.status == "classified":
        conditions.append("COALESCE(label.classification,c.classification)<>'unclassified'")
    where = " AND ".join(conditions)
    base = (
        "FROM _code_summary_location_counts c "
        "JOIN _code_daily_snapshots s ON s.run_id=c.run_id AND s.business_date=c.business_date "
        "LEFT JOIN _code_summary_location_labels label ON label.source_kind=c.source_kind "
        "AND label.location_key=c.location_key AND label.enabled=1 WHERE " + where
    )
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT c.location_key,MAX(c.display_name),SUM(c.row_count),MAX(c.business_date),"
            "COALESCE(MAX(label.classification),MAX(c.classification),'unclassified') " + base +
            " GROUP BY c.location_key ORDER BY SUM(c.row_count) DESC,c.location_key LIMIT %s OFFSET %s",
            (*params, payload.page_size, offset),
        )
        rows = await cur.fetchall()
        await cur.execute("SELECT COUNT(*) FROM (SELECT c.location_key " + base + " GROUP BY c.location_key) x", tuple(params))
        total = int((await cur.fetchone())[0] or 0)
        await cur.execute("SELECT COALESCE(SUM(c.row_count),0) " + base, tuple(params))
        record_count = int((await cur.fetchone())[0] or 0)
        await cur.execute(
            "SELECT COALESCE(SUM(c.row_count),0) FROM _code_summary_location_counts c "
            "JOIN _code_daily_snapshots s ON s.run_id=c.run_id AND s.business_date=c.business_date "
            "LEFT JOIN _code_summary_location_labels label ON label.source_kind=c.source_kind "
            "AND label.location_key=c.location_key AND label.enabled=1 "
            "WHERE c.source_kind=%s AND c.business_date BETWEEN %s AND %s "
            "AND s.version_no=(SELECT MAX(s2.version_no) FROM _code_daily_snapshots s2 "
            "WHERE s2.source_kind=s.source_kind AND s2.business_date=s.business_date) "
            "AND COALESCE(label.classification,c.classification)='unclassified'",
            (payload.source, payload.start_date, payload.end_date),
        )
        unclassified_count = int((await cur.fetchone())[0] or 0)
    return [
        {"location_key": row[0], "display_name": row[1], "record_count": int(row[2] or 0),
         "last_seen_date": row[3].isoformat() if row[3] else None, "classification": row[4]}
        for row in rows
    ], total, record_count, unclassified_count


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


async def _manual_location_labels(conn, source: str = "peace") -> dict[str, str]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT location_key,classification FROM _code_summary_location_labels "
            "WHERE source_kind=%s AND enabled=1",
            (source,),
        )
        return {str(row[0]): str(row[1]) for row in await cur.fetchall()}


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
            if source == "peace":
                for item in result.get("location_counts", []):
                    await cur.execute(
                        "INSERT INTO _code_summary_location_counts "
                        "(run_id,source_kind,business_date,location_key,display_name,classification,row_count) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                        "ON DUPLICATE KEY UPDATE display_name=VALUES(display_name),classification=VALUES(classification),row_count=VALUES(row_count)",
                        (run_id, source, date.fromisoformat(item["date"]), item["location_key"], item["display_name"], item["classification"], item["row_count"]),
                    )
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


@router.post("/fetch", status_code=202)
async def fetch_code_summaries(
    payload: DateRangeRequest,
    request: Request,
    user: dict = Depends(require_permission(VISIT_SOURCE_MANAGE)),
    conn=Depends(get_db),
):
    async def runner(job):
        pool = db_manager.get_pool("online_data")
        async with pool.acquire() as work_conn:
            async with work_conn.cursor() as cur:
                await cur.execute("SELECT GET_LOCK(%s, 0)", (FETCH_LOCK,))
                lock_row = await cur.fetchone()
            if not lock_row or lock_row[0] != 1:
                raise HTTPException(409, "当前已有平安码或管家码数据正在获取")
            try:
                await job.update(phase="fetching", total=2, message="正在读取平安码和管家码来源")
                personnel, places = await _directories(work_conn)
                manual_labels = await _manual_location_labels(work_conn, "peace")
                try:
                    fetched = await fetch_sources(payload.start_date, payload.end_date)
                except CodeSummaryError as exc:
                    fetched = {source: {"error": exc} for source in ("peace", "manager")}
                registration_counts: dict[date, int] = {}
                if not fetched["peace"].get("error"):
                    try:
                        registration_counts = await count_fullchain_registrations(
                            work_conn, payload.start_date, payload.end_date
                        )
                    except CodeSummaryError as exc:
                        fetched["peace"] = {"error": exc}
                results = []
                for index, source in enumerate(("peace", "manager"), 1):
                    source_result = fetched[source]
                    if source_result.get("error"):
                        error = source_result["error"]
                        run_id = await _insert_failed_run(work_conn, source=source, user_id=int(user["id"]), payload=payload, error=error)
                        results.append({"source": source, "run_id": run_id, "status": "failed", "error_code": error.code, "error_message": error.message})
                    else:
                        try:
                            aggregated = aggregate_rows(
                                source,
                                source_result["rows"],
                                payload.start_date,
                                payload.end_date,
                                personnel_names=personnel,
                                place_names=places,
                                manual_labels=manual_labels,
                                new_registration_counts=(
                                    registration_counts if source == "peace" else None
                                ),
                            )
                            run_id, status = await _commit_source(work_conn, source=source, user_id=int(user["id"]), payload=payload, result=aggregated)
                            results.append({"source": source, "run_id": run_id, "status": status, "raw_count": aggregated["raw_count"], "valid_count": aggregated["valid_count"], "excluded_count": aggregated["excluded_count"], "duplicate_count": aggregated["duplicate_count"], "unclassified_count": aggregated["unclassified_count"], "invalid_time_count": aggregated.get("invalid_time_count", 0)})
                        except CodeSummaryError as exc:
                            run_id = await _insert_failed_run(work_conn, source=source, user_id=int(user["id"]), payload=payload, error=exc)
                            results.append({"source": source, "run_id": run_id, "status": "failed", "error_code": exc.code, "error_message": exc.message})
                    await job.update(phase="processing", current=index, total=2, message=f"已完成 {index}/2 个来源")
                await record_admin_audit(user, "code_summary.fetch", target_type="code_summary", target_name=f"{payload.start_date} 至 {payload.end_date}", result="success" if all(item["status"] != "failed" for item in results) else "partial", detail={"start_date": payload.start_date.isoformat(), "end_date": payload.end_date.isoformat(), "runs": [{"source": item["source"], "run_id": item["run_id"], "status": item["status"]} for item in results]}, **request_audit_fields(request))
                return {"results": results, "message": "平安码、管家码获取完成"}
            finally:
                async with work_conn.cursor() as cur:
                    await cur.execute("SELECT RELEASE_LOCK(%s)", (FETCH_LOCK,))
                    await cur.fetchone()

    job, reused = await create_job("code_summary_fetch", int(user["id"]), {"start_date": payload.start_date.isoformat(), "end_date": payload.end_date.isoformat()}, runner, dedupe_key=f"{payload.start_date}:{payload.end_date}")
    return {"run": job, "reused": reused}


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
    if payload.source == "peace":
        try:
            registration_counts = await count_fullchain_registrations(
                conn, payload.start_date, payload.end_date
            )
        except CodeSummaryError as exc:
            raise HTTPException(503, exc.message) from exc
        for row in rows:
            row["new_registration_count"] = registration_counts.get(
                date.fromisoformat(row["business_date"]), 0
            )
    data = [_public_metrics(payload.source, row) for row in rows]
    latest_summary = _json_value(latest[13]) if latest else {}
    latest_run = None if not latest else {
        "id": int(latest[0]), "status": latest[1],
        "start_date": latest[2].isoformat(), "end_date": latest[3].isoformat(),
        "raw_count": int(latest[4] or 0), "valid_count": int(latest[5] or 0),
        "excluded_count": int(latest[6] or 0), "duplicate_count": int(latest[7] or 0),
        "unclassified_count": (
            sum(int(row.get("unclassified_scan_count") or 0) for row in data)
            if payload.source == "peace" else int(latest[8] or 0)
        ), "error_code": latest[9],
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


@router.post("/locations/search")
async def search_code_summary_locations(
    payload: LocationSearchRequest,
    _user: dict = Depends(require_permission(VISIT_SUMMARY_VIEW)),
    conn=Depends(get_db),
):
    if payload.source != "peace":
        return {"data": [], "total": 0, "record_count": 0, "unclassified_count": 0,
                "classifications": LOCATION_CLASSIFICATIONS}
    rows, total, record_count, unclassified_count = await _location_rows(conn, payload)
    return {
        "source": payload.source,
        "start_date": payload.start_date.isoformat(),
        "end_date": payload.end_date.isoformat(),
        "data": rows,
        "total": total,
        "record_count": record_count,
        "unclassified_count": unclassified_count,
        "classifications": LOCATION_CLASSIFICATIONS,
    }


@router.post("/locations/classifications")
async def save_code_summary_location_classifications(
    payload: LocationClassificationsRequest,
    request: Request,
    user: dict = Depends(require_permission(CODE_SUMMARY_MANAGE)),
    conn=Depends(get_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            for item in payload.items:
                location_key = "__empty__" if item.location_key == "__empty__" else normalize_label(item.location_key)
                if not location_key:
                    raise HTTPException(422, "位置标识不能为空")
                await cur.execute(
                    "INSERT INTO _code_summary_location_labels "
                    "(source_kind,location_key,display_name,classification,enabled,created_by,updated_by) "
                    "VALUES (%s,%s,%s,%s,1,%s,%s) "
                    "ON DUPLICATE KEY UPDATE display_name=VALUES(display_name),classification=VALUES(classification),enabled=1,updated_by=VALUES(updated_by)",
                    (payload.source, location_key, item.display_name[:255], item.classification, int(user["id"]), int(user["id"])),
                )
        await conn.commit()
    except HTTPException:
        await conn.rollback()
        raise
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user, "code_summary.location_classify", target_type="code_summary_location",
        target_name=str(len(payload.items)), result="success",
        detail={"source": payload.source, "count": len(payload.items),
                "classifications": sorted({item.classification for item in payload.items})},
        **request_audit_fields(request),
    )
    return {"updated": len(payload.items), "message": "位置分类已保存"}


@router.post("/locations/recompute")
async def recompute_code_summary_locations(
    payload: DateRangeRequest,
    request: Request,
    user: dict = Depends(require_permission(CODE_SUMMARY_MANAGE)),
    conn=Depends(get_db),
):
    await conn.begin()
    changed = 0
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT c.business_date,c.location_key,SUM(c.row_count),COALESCE(MAX(label.classification),MAX(c.classification),'unclassified') "
                "FROM _code_summary_location_counts c "
                "JOIN _code_daily_snapshots s ON s.run_id=c.run_id AND s.business_date=c.business_date "
                "LEFT JOIN _code_summary_location_labels label ON label.source_kind=c.source_kind "
                "AND label.location_key=c.location_key AND label.enabled=1 "
                "WHERE c.source_kind='peace' AND c.business_date BETWEEN %s AND %s "
                "AND s.version_no=(SELECT MAX(s2.version_no) FROM _code_daily_snapshots s2 WHERE s2.source_kind='peace' AND s2.business_date=c.business_date) "
                "GROUP BY c.business_date,c.location_key, label.classification",
                (payload.start_date, payload.end_date),
            )
            grouped: dict[date, dict[str, int]] = {}
            for business_date, _key, count, classification in await cur.fetchall():
                metrics = grouped.setdefault(business_date, {"patrol": 0, "dispatch_hall": 0, "household_hall": 0, "social": 0, "unclassified": 0})
                if classification in metrics:
                    metrics[classification] += int(count or 0)
            for business_date, metrics in grouped.items():
                await cur.execute(
                    "UPDATE _code_daily_snapshots SET patrol_scan_count=%s,dispatch_hall_scan_count=%s,household_hall_scan_count=%s,social_scan_count=%s,unclassified_scan_count=%s,classifier_version=%s "
                    "WHERE source_kind='peace' AND business_date=%s AND version_no=(SELECT max_version FROM (SELECT MAX(version_no) AS max_version FROM _code_daily_snapshots WHERE source_kind='peace' AND business_date=%s) latest)",
                    (metrics["patrol"], metrics["dispatch_hall"], metrics["household_hall"], metrics["social"], metrics["unclassified"], CLASSIFIER_VERSION, business_date, business_date),
                )
                changed += cur.rowcount or 0
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user, "code_summary.location_recompute", target_type="code_summary", target_name="peace",
        result="success", detail={"start_date": payload.start_date.isoformat(), "end_date": payload.end_date.isoformat(), "days": changed},
        **request_audit_fields(request),
    )
    return {"updated_days": changed, "message": "位置分类汇总已重新计算"}
