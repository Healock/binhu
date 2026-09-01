"""辖区人房档案和人员标签的第一版 API。"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from services.xlsx_export import XLSX_MEDIA_TYPE, build_xlsx
from pydantic import BaseModel, Field

from config import settings
from database import db_manager
from deps import require_permission
from services.audit import record_admin_audit, request_audit_fields
from services.permissions import (
    REGISTRY_PROPERTY_MANAGE,
    REGISTRY_PROPERTY_VIEW,
    REGISTRY_WATCH_MANAGE,
    REGISTRY_WATCH_VIEW,
    has_permission,
    permitted_communities,
)
from services.registry_security import hmac_digest, normalize_identity, normalize_phone
from services.registry_certificate_status import certificate_status_summary
from services.registry_visit_history import filter_property_ids_by_visit, load_property_visit_summaries
from services.watch_matching import backfill_assignment_snapshots
from services.registry_watch_backfill import ensure_watch_person_registry_link
from services.address_matching import MATCHER_VERSION


router = APIRouter(prefix="/api/registry", tags=["辖区档案"])

StarRating = Literal["一星出租房", "二星出租房", "三星出租房", "四星出租房", "五星出租房"]
PropertyAddressMatchStatus = Literal[
    "", "unmatched", "suggested", "ambiguous", "conflict", "confirmed", "invalid", "disabled",
]


class PropertyCreate(BaseModel):
    street: str = Field(default="", max_length=200)
    community_id: int | None = None
    community_name_snapshot: str = Field(default="", max_length=200)
    natural_address: str = Field(default="", max_length=500)
    building: str = Field(default="", max_length=100)
    room: str = Field(default="", max_length=100)
    housing_type: str = Field(default="", max_length=50)
    residence_type: str = Field(default="", max_length=100)
    source_house_no: str = Field(default="", max_length=100)
    source_updated_at: datetime | None = None
    source_type: str = Field(default="manual", max_length=30)
    source_ref: str = Field(default="", max_length=190)
    normalized_address: str = Field(default="", max_length=1000)


class HousingPersonCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    identity_number: str | None = Field(default=None, max_length=50)
    is_temporary: bool = False
    verification_status: Literal["unverified", "pending", "verified"] = "unverified"


class RegistrySearch(BaseModel):
    name: str = Field(default="", max_length=100)
    identity_number: str = Field(default="", max_length=50)
    phone: str = Field(default="", max_length=200)
    category_ids: list[int] = Field(default_factory=list, max_length=20)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class PropertySearch(BaseModel):
    keyword: str = Field(default="", max_length=200)
    community_id: int | None = None
    community_name: str = Field(default="", max_length=200)
    housing_category: Literal["", "rental", "self_owned", "other", "unmarked"] = ""
    certificate_status: Literal[
        "", "normal_signed", "not_required", "not_uploaded", "renter_needs_correction",
        "actual_renter_missing", "multiple_or_conflict", "not_applicable",
    ] = ""
    status: Literal["", "active", "inactive"] = "active"
    visit_start_date: date | None = None
    visit_end_date: date | None = None
    star_ratings: list[StarRating] = Field(default_factory=list, max_length=5)
    small_community_ids: list[int] = Field(default_factory=list, max_length=50)
    address_match_statuses: list[PropertyAddressMatchStatus] = Field(default_factory=list, max_length=10)
    sort: Literal["id_desc", "address_asc", "community_asc", "updated_desc", "visit_desc"] = "id_desc"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class PropertySmallCommunityConfirmItem(BaseModel):
    property_id: int = Field(gt=0)
    small_community_id: int = Field(gt=0)


class PropertySmallCommunityConfirm(BaseModel):
    items: list[PropertySmallCommunityConfirmItem] = Field(min_length=1, max_length=200)


class PropertyPersonRoleCreate(BaseModel):
    person_id: int = Field(gt=0)
    role_type_id: int = Field(gt=0)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    verified: bool = False


class WatchCategoryCreate(BaseModel):
    code: str = Field(min_length=1, max_length=60)
    name: str = Field(min_length=1, max_length=100)
    parent_id: int | None = None
    color: str = Field(default="#1677ff", max_length=20)
    alert_level: Literal["normal", "notice", "warning", "critical"] = "normal"


class WatchPersonCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    identity_number: str | None = Field(default=None, max_length=50)
    is_temporary: bool = False
    verification_status: Literal["unverified", "pending", "verified"] = "unverified"


class WatchAssignmentCreate(BaseModel):
    person_id: int = Field(gt=0)
    category_id: int = Field(gt=0)
    valid_from: datetime
    valid_to: datetime | None = None
    basis: str = Field(default="", max_length=1000)


class WatchPersonSearch(BaseModel):
    keyword: str = Field(default="", max_length=100)
    category_ids: list[int] = Field(default_factory=list, max_length=20)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


async def _domain_conn(name: str):
    try:
        pool = db_manager.get_pool(name)
    except ValueError as exc:
        raise HTTPException(503, f"{name} 数据库尚未完成初始化") from exc
    conn = await pool.acquire()
    try:
        yield conn
    finally:
        pool.release(conn)


async def get_registry_db():
    if not settings.REGISTRY_FEATURE_ENABLED:
        raise HTTPException(503, "辖区档案功能尚未完成生产迁移和启用")
    async for conn in _domain_conn("registry"):
        yield conn


async def _allowed_community_ids(user: dict, permission: str) -> list[int] | None:
    names = _allowed_community_names(user, permission)
    if names is None:
        return None
    if not names:
        return []
    try:
        pool = db_manager.get_pool("online_data")
    except ValueError:
        return []
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(names))
            await cur.execute(
                f"SELECT id FROM _communities WHERE name IN ({placeholders}) AND is_active=1",
                tuple(names),
            )
            return [int(row[0]) for row in await cur.fetchall()]
    finally:
        pool.release(conn)


def _allowed_community_names(user: dict, permission: str) -> list[str] | None:
    scope = (user.get("permission_scopes") or {}).get(permission, user.get("data_scope"))
    if scope == "all":
        return None
    return sorted({
        str(name).strip()
        for name in permitted_communities(user, permission) or []
        if str(name).strip()
    })


def _person_payload(row, include_identity: bool = True) -> dict:
    return {
        "id": int(row[0]),
        "name": str(row[1]),
        "identity_number": str(row[2] or "") if include_identity else "",
        "has_identity": bool(row[2]) if include_identity else False,
        "is_temporary": bool(row[3]),
        "verification_status": str(row[4]),
        "status": str(row[5]),
        "created_at": row[6].isoformat() if row[6] else None,
        "updated_at": row[7].isoformat() if row[7] else None,
    }


async def _load_registry_person_categories(cur, person_ids: list[int], *, history: bool = False) -> dict[int, list[dict]]:
    """Return tags through the explicit registry link, with an identity fallback for legacy rows."""
    result: dict[int, list[dict]] = {int(person_id): [] for person_id in person_ids}
    if not person_ids:
        return result
    placeholders = ",".join(["%s"] * len(person_ids))
    current_clause = "" if history else (
        "AND assignment.status='active' "
        "AND assignment.valid_from<=UTC_TIMESTAMP() "
        "AND (assignment.valid_to IS NULL OR assignment.valid_to>=UTC_TIMESTAMP()) "
        "AND (assignment.released_at IS NULL OR assignment.released_at>UTC_TIMESTAMP()) "
        "AND category.is_active=1 "
    )
    await cur.execute(
        "SELECT registry_person.id, assignment.id, category.id, category.code, category.name, "
        "category.color, category.alert_level, assignment.valid_from, assignment.valid_to, "
        "assignment.released_at, assignment.status, assignment.basis, assignment.source_type, assignment.source_ref "
        "FROM registry_housing_people registry_person "
        "JOIN watch_people watch_person ON (watch_person.registry_person_id=registry_person.id "
        "OR (watch_person.registry_person_id IS NULL AND watch_person.identity_hmac=registry_person.identity_hmac)) "
        "JOIN watch_assignments assignment ON assignment.person_id=watch_person.id "
        "JOIN watch_categories category ON category.id=assignment.category_id "
        f"WHERE registry_person.id IN ({placeholders}) {current_clause} "
        "ORDER BY registry_person.id, category.sort_order, category.id, assignment.id DESC",
        tuple(person_ids),
    )
    seen: set[tuple[int, int]] = set()
    for row in await cur.fetchall():
        registry_person_id = int(row[0])
        assignment_id = int(row[1])
        key = (registry_person_id, assignment_id)
        if key in seen:
            continue
        seen.add(key)
        result[registry_person_id].append({
            "assignment_id": assignment_id,
            "category_id": int(row[2]),
            "category_code": str(row[3]),
            "category_name": str(row[4]),
            "color": str(row[5]),
            "alert_level": str(row[6]),
            "valid_from": row[7].isoformat() if row[7] else None,
            "valid_to": row[8].isoformat() if row[8] else None,
            "released_at": row[9].isoformat() if row[9] else None,
            "status": str(row[10]),
            "basis": str(row[11] or ""),
            "source_type": str(row[12] or ""),
            "source_ref": str(row[13] or ""),
        })
    return result


def _registry_person_category_payload(item: dict) -> dict:
    return {
        "assignment_id": item["assignment_id"],
        "id": item["category_id"],
        "code": item["category_code"],
        "name": item["category_name"],
        "color": item["color"],
        "alert_level": item["alert_level"],
    }


async def _watch_people_result(data: WatchPersonSearch, user: dict, conn) -> dict:
    allowed_names = _allowed_community_names(user, REGISTRY_WATCH_VIEW)
    where = ["watch_people.status='active'"]
    params: list[object] = []
    if allowed_names is not None:
        if not allowed_names:
            return {"total": 0, "page": data.page, "page_size": data.page_size, "data": []}
        online_schema = settings.MYSQL_ONLINE_DATA_DB.replace("`", "")
        placeholders = ",".join(["%s"] * len(allowed_names))
        where.append(
            "EXISTS (SELECT 1 FROM watch_assignments scoped_assignment "
            "JOIN online_task_watch_snapshots scoped_snapshot "
            "ON scoped_snapshot.assignment_id=scoped_assignment.id "
            f"JOIN `{online_schema}`._online_source_projection scoped_projection "
            "ON scoped_projection.parser_type=scoped_snapshot.parser_type "
            "AND scoped_projection.row_key=scoped_snapshot.row_key "
            "WHERE scoped_assignment.person_id=watch_people.id "
            f"AND scoped_projection.community IN ({placeholders}))"
        )
        params.extend(allowed_names)
    category_ids = list(dict.fromkeys(data.category_ids))
    if category_ids:
        placeholders = ",".join(["%s"] * len(category_ids))
        where.append(
            "EXISTS (SELECT 1 FROM watch_assignments category_assignment "
            "WHERE category_assignment.person_id=watch_people.id "
            f"AND category_assignment.category_id IN ({placeholders}) "
            "AND category_assignment.status='active' "
            "AND category_assignment.valid_from<=UTC_TIMESTAMP() "
            "AND (category_assignment.valid_to IS NULL OR category_assignment.valid_to>=UTC_TIMESTAMP()) "
            "AND (category_assignment.released_at IS NULL OR category_assignment.released_at>UTC_TIMESTAMP()))"
        )
        params.extend(category_ids)
    keyword = data.keyword.strip()
    if keyword:
        normalized = normalize_identity(keyword)
        digest, _ = hmac_digest(normalized, kind="identity")
        if user.get("role") == "super_admin" and digest and len(normalized) in {15, 18}:
            where.append("(watch_people.name LIKE %s OR watch_people.identity_hmac=%s)")
            params.extend((f"%{keyword}%", digest))
        else:
            where.append("watch_people.name LIKE %s")
            params.append(f"%{keyword}%")
    clause = " WHERE " + " AND ".join(where)
    offset = (data.page - 1) * data.page_size
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT COUNT(*) FROM watch_people{clause}", tuple(params))
        total = int((await cur.fetchone())[0])
        await cur.execute(
            "SELECT id, name, identity_number, is_temporary, verification_status, status, created_at, updated_at, registry_person_id "
            f"FROM watch_people{clause} ORDER BY id DESC LIMIT %s OFFSET %s",
            tuple(params) + (data.page_size, offset),
        )
        rows = await cur.fetchall()
        person_ids = [int(row[0]) for row in rows]
        categories_by_person: dict[int, list[dict]] = {person_id: [] for person_id in person_ids}
        if person_ids:
            placeholders = ",".join(["%s"] * len(person_ids))
            await cur.execute(
                "SELECT assignment.person_id, category.id, category.code, category.name, category.color, category.alert_level "
                "FROM watch_assignments assignment "
                "JOIN watch_categories category ON category.id=assignment.category_id "
                f"WHERE assignment.person_id IN ({placeholders}) "
                "AND assignment.status='active' AND category.is_active=1 "
                "AND assignment.valid_from<=UTC_TIMESTAMP() "
                "AND (assignment.valid_to IS NULL OR assignment.valid_to>=UTC_TIMESTAMP()) "
                "AND (assignment.released_at IS NULL OR assignment.released_at>UTC_TIMESTAMP()) "
                "ORDER BY category.sort_order, category.id",
                tuple(person_ids),
            )
            for item in await cur.fetchall():
                categories_by_person[int(item[0])].append({
                    "id": int(item[1]), "code": str(item[2]), "name": str(item[3]),
                    "color": str(item[4]), "alert_level": str(item[5]),
                })
    include_identity = user.get("role") == "super_admin"
    data_rows = []
    for row in rows:
        item = _person_payload(row, include_identity)
        item["registry_person_id"] = int(row[8]) if row[8] is not None else None
        item["is_registry_linked"] = row[8] is not None
        item["categories"] = categories_by_person.get(int(row[0]), [])
        data_rows.append(item)
    return {"total": total, "page": data.page, "page_size": data.page_size, "data": data_rows}


def _property_payload(row) -> dict:
    payload = {
        "id": int(row[0]),
        "street": row[1],
        "community_id": row[2],
        "community_name": row[3],
        "natural_address": row[4],
        "building": row[5],
        "room": row[6],
        "housing_type": row[7],
        "residence_type": row[8],
        "source_house_no": row[9],
        "source_updated_at": row[10].isoformat() if row[10] else None,
        "source_type": row[11],
        "source_ref": row[12],
        "normalized_address": row[13],
        "status": row[14],
        "version": int(row[15]),
        "created_at": row[16].isoformat() if row[16] else None,
        "updated_at": row[17].isoformat() if row[17] else None,
    }
    payload.update(certificate_status_summary(
        housing_type=row[7],
        certificate_count=int(row[18] or 0),
        certificate_issue_count=int(row[24] or 0),
        source_ready=bool(row[25]),
        landlord_name=row[19],
        actual_renter_name=row[20],
        signed_status=row[21],
        sign_type=row[22],
        updated_at=row[23].isoformat() if row[23] else None,
    ))
    evidence = row[34]
    if isinstance(evidence, (bytes, bytearray)):
        evidence = evidence.decode("utf-8", errors="replace")
    if isinstance(evidence, str):
        try:
            evidence = json.loads(evidence)
        except (TypeError, ValueError):
            evidence = {}
    if not isinstance(evidence, dict):
        evidence = {}
    payload.update({
        "small_community_id": int(row[26]) if row[26] is not None else None,
        "small_community_name": str(row[27] or ""),
        "small_community_community_id": int(row[28]) if row[28] is not None else None,
        "small_community_community_name": str(row[29] or ""),
        "address_match_status": str(row[30] or "unmatched"),
        "address_match_score": float(row[31] or 0),
        "address_match_method": str(row[32] or ""),
        "address_match_reason": str(row[33] or ""),
        "address_match_candidates": evidence.get("candidates", []),
        "address_match_version": str(row[35] or ""),
        "address_match_confirmed_by": int(row[36]) if row[36] is not None else None,
        "address_match_confirmed_at": row[37].isoformat() if row[37] else None,
    })
    return payload


async def _property_search_result(
    data: PropertySearch,
    user: dict,
    conn,
    *,
    export_all: bool = False,
) -> dict:
    if data.visit_start_date and data.visit_end_date and data.visit_end_date < data.visit_start_date:
        raise HTTPException(422, "走访结束日期不能早于开始日期")
    if data.visit_start_date and data.visit_end_date:
        if (data.visit_end_date - data.visit_start_date).days > 366:
            raise HTTPException(422, "走访日期范围不能超过 367 天")
    allowed = await _allowed_community_ids(user, REGISTRY_PROPERTY_VIEW)
    if allowed is not None and data.community_id is not None and data.community_id not in allowed:
        raise HTTPException(403, "无权查看该社区档案")

    where: list[str] = []
    params: list[object] = []
    if allowed is not None:
        if not allowed:
            return {
                "total": 0,
                "page": data.page,
                "page_size": data.page_size,
                "data": [],
                "match_status_counts": {},
            }
        where.append("property.community_id IN (" + ",".join(["%s"] * len(allowed)) + ")")
        params.extend(allowed)
    if data.community_id is not None:
        where.append("property.community_id=%s")
        params.append(data.community_id)
    elif data.community_name.strip():
        online_schema = settings.MYSQL_ONLINE_DATA_DB.replace("`", "")
        async with conn.cursor() as community_cur:
            await community_cur.execute(
                f"""SELECT DISTINCT community.id FROM `{online_schema}`._communities community
                   LEFT JOIN `{online_schema}`._community_aliases alias ON alias.community_id=community.id
                   WHERE community.is_active=1 AND (community.name=%s OR alias.alias=%s)""",
                (data.community_name.strip(), data.community_name.strip()),
            )
            community_ids = [int(row[0]) for row in await community_cur.fetchall()]
        if not community_ids:
            return {
                "total": 0,
                "page": data.page,
                "page_size": data.page_size,
                "data": [],
                "match_status_counts": {},
            }
        where.append("property.community_id IN (" + ",".join(["%s"] * len(community_ids)) + ")")
        params.extend(community_ids)
    if data.status:
        where.append("property.status=%s")
        params.append(data.status)
    if data.housing_category == "rental":
        where.append("property.housing_type IN (%s,%s)")
        params.extend(["个人出租", "单位出租"])
    elif data.housing_category == "self_owned":
        where.append("property.housing_type=%s")
        params.append("自购房屋")
    elif data.housing_category == "other":
        where.append("COALESCE(property.housing_type,'')<>'' AND property.housing_type NOT IN (%s,%s,%s)")
        params.extend(["个人出租", "单位出租", "自购房屋"])
    elif data.housing_category == "unmarked":
        where.append("COALESCE(property.housing_type,'')='' ")
    if data.small_community_ids:
        small_ids = list(dict.fromkeys(int(value) for value in data.small_community_ids))
        where.append("property_match.small_community_id IN (" + ",".join(["%s"] * len(small_ids)) + ")")
        params.extend(small_ids)
    match_statuses = [value for value in dict.fromkeys(data.address_match_statuses) if value]
    if match_statuses:
        placeholders = ",".join(["%s"] * len(match_statuses))
        where.append(f"COALESCE(property_match.match_status,'unmatched') IN ({placeholders})")
        params.extend(match_statuses)

    certificate_count = "COALESCE(certificate_totals.certificate_count,0)"
    certificate_issue_count = "COALESCE(certificate_issues.issue_count,0)"
    certificate_source_ready = "COALESCE(certificate_source_state.source_ready,0)"
    signed = "LOWER(TRIM(COALESCE(certificate.signed_status,''))) IN ('是','已签署','已签','true','1','yes')"
    renter_present = "TRIM(COALESCE(certificate.actual_renter_name,''))<>''"
    if data.certificate_status == "not_applicable":
        where.append("property.housing_type NOT IN (%s,%s)")
        params.extend(["个人出租", "单位出租"])
    elif data.certificate_status == "multiple_or_conflict":
        where.append(f"({certificate_count}>1 OR {certificate_issue_count}>0)")
    elif data.certificate_status == "not_required":
        where.append(
            "property.housing_type IN (%s,%s) AND "
            f"{certificate_source_ready}=1 AND {certificate_count}=0 AND {certificate_issue_count}=0"
        )
        params.extend(["个人出租", "单位出租"])
    elif data.certificate_status == "actual_renter_missing":
        where.append(f"property.housing_type IN (%s,%s) AND {certificate_count}=1 AND NOT ({renter_present})")
        params.extend(["个人出租", "单位出租"])
    elif data.certificate_status == "normal_signed":
        where.append(f"{certificate_count}=1 AND {renter_present} AND {signed}")
    elif data.certificate_status == "renter_needs_correction":
        where.append(
            f"{certificate_count}=1 AND {renter_present} AND NOT ({signed}) "
            "AND TRIM(COALESCE(certificate.sign_type,''))<>''"
        )
    elif data.certificate_status == "not_uploaded":
        where.append(
            "property.housing_type IN (%s,%s) AND ("
            f"({certificate_source_ready}=0 AND {certificate_count}=0) OR "
            f"({certificate_count}=1 AND {certificate_issue_count}=0 AND {renter_present} "
            f"AND NOT ({signed}) AND TRIM(COALESCE(certificate.sign_type,''))=''))"
        )
        params.extend(["个人出租", "单位出租"])

    keyword = data.keyword.strip()
    if keyword:
        like_value = f"%{keyword}%"
        where.append(
            "(property.community_name_snapshot LIKE %s OR property.natural_address LIKE %s OR property.normalized_address LIKE %s "
            "OR property.source_house_no LIKE %s OR property.building LIKE %s OR property.room LIKE %s "
            "OR property.housing_type LIKE %s OR property.residence_type LIKE %s "
            "OR property_match.small_community_name LIKE %s "
            "OR EXISTS (SELECT 1 FROM registry_address_aliases alias "
            "WHERE alias.property_id=property.id AND alias.enabled=1 AND alias.alias LIKE %s))"
        )
        params.extend([like_value] * 10)

    clause = " WHERE " + " AND ".join(where) if where else ""
    joins = (
        " LEFT JOIN registry_property_small_community_links property_match "
        "ON property_match.property_id=property.id "
        "LEFT JOIN (SELECT property_id,COUNT(*) certificate_count,MAX(id) latest_id "
        "FROM registry_property_certificates GROUP BY property_id) certificate_totals "
        "ON certificate_totals.property_id=property.id "
        "LEFT JOIN registry_property_certificates certificate "
        "ON certificate.id=certificate_totals.latest_id "
        "LEFT JOIN (SELECT entity_key,COUNT(*) issue_count "
        "FROM registry_import_issues WHERE source_type='certificate' AND status='pending' "
        "GROUP BY entity_key) certificate_issues "
        "ON certificate_issues.entity_key=property.normalized_address "
        "CROSS JOIN (SELECT EXISTS(SELECT 1 FROM registry_source_batches "
        "WHERE source_type='certificate' AND status IN ('imported','partially_imported')) source_ready) "
        "certificate_source_state"
    )
    # Visit fields live in VisitData. Resolve matching property IDs before
    # pagination so a filter never applies only to the visible page.
    if data.visit_start_date or data.visit_end_date or data.star_ratings:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT property.id,property.community_name_snapshot,property.natural_address,property.normalized_address "
                f"FROM registry_properties property{joins}{clause}",
                tuple(params),
            )
            candidate_rows = await cur.fetchall()
            candidate_properties = [
                {
                    "id": int(row[0]),
                    "community_name": row[1],
                    "natural_address": row[2],
                    "normalized_address": row[3],
                }
                for row in candidate_rows
            ]
            matching_ids = await filter_property_ids_by_visit(
                cur,
                candidate_properties,
                visit_start_date=data.visit_start_date,
                visit_end_date=data.visit_end_date,
                star_ratings=data.star_ratings,
            )
        if not matching_ids:
            return {
                "total": 0,
                "page": data.page,
                "page_size": data.page_size,
                "data": [],
                "match_status_counts": {},
            }
        where.append("property.id IN (" + ",".join(["%s"] * len(matching_ids)) + ")")
        params.extend(sorted(matching_ids))
        clause = " WHERE " + " AND ".join(where) if where else ""
    offset = (data.page - 1) * data.page_size
    visit_sort = data.sort == "visit_desc"
    order_sql = {
        "id_desc": "property.id DESC",
        "address_asc": "COALESCE(property.normalized_address, property.natural_address, '') ASC, property.id DESC",
        "community_asc": "COALESCE(property.community_name_snapshot, '') ASC, property.id DESC",
        "updated_desc": "property.updated_at DESC, property.id DESC",
        "visit_desc": "property.id DESC",
    }.get(data.sort, "property.id DESC")
    async with conn.cursor() as cur:
        await cur.execute(
            f"SELECT COUNT(*) FROM registry_properties property{joins}{clause}",
            tuple(params),
        )
        total = int((await cur.fetchone())[0])
        await cur.execute(
            "SELECT COALESCE(property_match.match_status,'unmatched'),COUNT(*) "
            f"FROM registry_properties property{joins}{clause} "
            "GROUP BY COALESCE(property_match.match_status,'unmatched')",
            tuple(params),
        )
        match_status_counts = {
            str(row[0] or "unmatched"): int(row[1] or 0)
            for row in await cur.fetchall()
        }
        await cur.execute(
            "SELECT property.id,property.street,property.community_id,property.community_name_snapshot,property.natural_address, "
            "property.building,property.room,property.housing_type,property.residence_type,property.source_house_no,property.source_updated_at, "
            "property.source_type,property.source_ref,property.normalized_address,property.status,property.current_version,property.created_at,property.updated_at, "
            "COALESCE(certificate_totals.certificate_count,0),certificate.landlord_name,certificate.actual_renter_name,"
            "certificate.signed_status,certificate.sign_type,certificate.updated_at,"
            "COALESCE(certificate_issues.issue_count,0),certificate_source_state.source_ready, "
            "property_match.small_community_id,property_match.small_community_name,"
            "property_match.community_id,property_match.community_name_snapshot,"
            "COALESCE(property_match.match_status,'unmatched'),"
            "COALESCE(property_match.match_score,0),property_match.match_method,"
            "property_match.match_reason,property_match.match_evidence,"
            "property_match.matcher_version,property_match.confirmed_by,property_match.confirmed_at "
            f"FROM registry_properties property{joins}{clause} ORDER BY {order_sql} "
            + ("" if export_all or visit_sort else "LIMIT %s OFFSET %s"),
            tuple(params) if export_all or visit_sort else tuple(params) + (data.page_size, offset),
        )
        rows = await cur.fetchall()
    payloads = [_property_payload(row) for row in rows]
    async with conn.cursor() as cur:
        visit_summaries = await load_property_visit_summaries(cur, payloads)
    for payload in payloads:
        payload.update(visit_summaries.get(int(payload["id"]), {}))
    if visit_sort:
        payloads.sort(
            key=lambda item: (
                str(item.get("latest_visit_date") or ""),
                int(item["id"]),
            ),
            reverse=True,
        )
        if not export_all:
            payloads = payloads[offset:offset + data.page_size]
    return {
        "total": total,
        "page": data.page,
        "page_size": data.page_size,
        "data": payloads,
        "match_status_counts": match_status_counts,
    }


@router.get("/properties")
async def list_properties(
    community_id: int | None = Query(default=None),
    housing_category: Literal["", "rental", "self_owned", "other", "unmarked"] = Query(default=""),
    certificate_status: Literal[
        "", "normal_signed", "not_required", "not_uploaded", "renter_needs_correction",
        "actual_renter_missing", "multiple_or_conflict", "not_applicable",
    ] = Query(default=""),
    status: Literal["", "active", "inactive"] = Query(default="active"),
    visit_start_date: date | None = Query(default=None),
    visit_end_date: date | None = Query(default=None),
    star_ratings: list[StarRating] = Query(default=[]),
    small_community_id: list[int] = Query(default=[]),
    address_match_status: list[PropertyAddressMatchStatus] = Query(default=[]),
    sort: Literal["id_desc", "address_asc", "community_asc", "updated_desc", "visit_desc"] = Query(default="id_desc"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(require_permission(REGISTRY_PROPERTY_VIEW)),
    conn=Depends(get_registry_db),
):
    return await _property_search_result(
        PropertySearch(
            community_id=community_id,
            housing_category=housing_category,
            certificate_status=certificate_status,
            status=status,
            visit_start_date=visit_start_date,
            visit_end_date=visit_end_date,
            star_ratings=star_ratings,
            small_community_ids=small_community_id,
            address_match_statuses=address_match_status,
            sort=sort,
            page=page,
            page_size=page_size,
        ),
        user,
        conn,
    )


@router.post("/properties/search")
async def search_properties(
    data: PropertySearch,
    user: dict = Depends(require_permission(REGISTRY_PROPERTY_VIEW)),
    conn=Depends(get_registry_db),
):
    """搜索房屋档案；地址和户号关键词放在请求正文，避免进入访问日志 URL。"""
    return await _property_search_result(data, user, conn)


@router.get("/properties/small-community-options")
async def property_small_community_options(
    community_id: int | None = Query(default=None),
    user: dict = Depends(require_permission(REGISTRY_PROPERTY_VIEW)),
):
    allowed = await _allowed_community_ids(user, REGISTRY_PROPERTY_VIEW)
    if allowed is not None and community_id is not None and community_id not in allowed:
        raise HTTPException(403, "无权查看该社区的小区地址库")
    where = ["entry.enabled=1", "entry.community_id IS NOT NULL", "community.is_active=1"]
    params: list[object] = []
    if community_id is not None:
        where.append("entry.community_id=%s")
        params.append(community_id)
    elif allowed is not None:
        if not allowed:
            return {"data": []}
        where.append("entry.community_id IN (" + ",".join(["%s"] * len(allowed)) + ")")
        params.extend(allowed)
    try:
        pool = db_manager.get_pool("online_data")
    except ValueError as exc:
        raise HTTPException(503, "本地小区地址库尚未完成初始化") from exc
    online_conn = await pool.acquire()
    try:
        async with online_conn.cursor() as cur:
            await cur.execute(
                "SELECT entry.id,entry.name,entry.community_id,community.name,"
                "entry.detail_address,entry.aliases_json "
                "FROM _police_address_entries entry "
                "JOIN _communities community ON community.id=entry.community_id "
                "WHERE " + " AND ".join(where) + " ORDER BY community.name,entry.name,entry.id",
                tuple(params),
            )
            rows = await cur.fetchall()
    finally:
        pool.release(online_conn)
    result = []
    for row in rows:
        aliases = row[5]
        if isinstance(aliases, str):
            try:
                aliases = json.loads(aliases)
            except (TypeError, ValueError):
                aliases = []
        result.append({
            "id": int(row[0]),
            "name": str(row[1] or ""),
            "community_id": int(row[2]),
            "community_name": str(row[3] or ""),
            "detail_address": str(row[4] or ""),
            "aliases": aliases if isinstance(aliases, list) else [],
        })
    return {"data": result}


@router.post("/properties/small-community-links/confirm")
async def confirm_property_small_communities(
    data: PropertySmallCommunityConfirm,
    request: Request,
    user: dict = Depends(require_permission(REGISTRY_PROPERTY_MANAGE)),
    conn=Depends(get_registry_db),
):
    item_by_property: dict[int, int] = {}
    for item in data.items:
        if item.property_id in item_by_property:
            raise HTTPException(422, "同一套房屋不能在一次确认中选择多个小区")
        item_by_property[item.property_id] = item.small_community_id
    entry_ids = sorted(set(item_by_property.values()))
    try:
        pool = db_manager.get_pool("online_data")
    except ValueError as exc:
        raise HTTPException(503, "本地小区地址库尚未完成初始化") from exc
    online_conn = await pool.acquire()
    try:
        async with online_conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(entry_ids))
            await cur.execute(
                "SELECT entry.id,entry.name,entry.community_id,community.name "
                "FROM _police_address_entries entry "
                "JOIN _communities community ON community.id=entry.community_id "
                f"WHERE entry.enabled=1 AND community.is_active=1 AND entry.id IN ({placeholders})",
                tuple(entry_ids),
            )
            entries = {
                int(row[0]): {
                    "id": int(row[0]), "name": str(row[1] or ""),
                    "community_id": int(row[2]), "community_name": str(row[3] or ""),
                }
                for row in await cur.fetchall()
            }
    finally:
        pool.release(online_conn)
    if len(entries) != len(entry_ids):
        raise HTTPException(409, "所选小区不存在、已停用或尚未设置所属社区")

    allowed = await _allowed_community_ids(user, REGISTRY_PROPERTY_MANAGE)
    property_ids = sorted(item_by_property)
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            placeholders = ",".join(["%s"] * len(property_ids))
            await cur.execute(
                "SELECT id,community_id,community_name_snapshot,current_version,status "
                f"FROM registry_properties WHERE id IN ({placeholders}) FOR UPDATE",
                tuple(property_ids),
            )
            properties = {
                int(row[0]): {
                    "community_id": int(row[1]) if row[1] is not None else None,
                    "community_name": str(row[2] or ""),
                    "version": int(row[3] or 1), "status": str(row[4] or ""),
                }
                for row in await cur.fetchall()
            }
            if len(properties) != len(property_ids):
                raise HTTPException(404, "部分房屋档案不存在")
            rows = []
            for property_id, entry_id in item_by_property.items():
                property_row = properties[property_id]
                entry = entries[entry_id]
                if property_row["status"] != "active":
                    raise HTTPException(409, f"房屋 {property_id} 已停用，不能确认小区")
                if allowed is not None and property_row["community_id"] not in allowed:
                    raise HTTPException(403, "只能维护有权限社区内的房屋档案")
                if property_row["community_id"] is None or property_row["community_id"] != entry["community_id"]:
                    raise HTTPException(409, f"房屋 {property_id} 与所选小区的社区归属不一致")
                rows.append((
                    property_id, entry_id, entry["name"], entry["community_id"],
                    entry["community_name"], MATCHER_VERSION,
                    int(user["id"]), property_row["version"],
                ))
            await cur.executemany(
                "INSERT INTO registry_property_small_community_links ("
                "property_id,small_community_id,small_community_name,community_id,"
                "community_name_snapshot,match_status,match_score,match_method,match_reason,"
                "match_evidence,matcher_version,confirmed_by,confirmed_at,property_version) "
                "VALUES (%s,%s,%s,%s,%s,'confirmed',1,'人工确认','管理员已确认小区归属',"
                "JSON_OBJECT('source','manual'),%s,%s,UTC_TIMESTAMP(),%s) "
                "ON DUPLICATE KEY UPDATE small_community_id=VALUES(small_community_id),"
                "small_community_name=VALUES(small_community_name),community_id=VALUES(community_id),"
                "community_name_snapshot=VALUES(community_name_snapshot),match_status='confirmed',"
                "match_score=1,match_method='人工确认',match_reason='管理员已确认小区归属',"
                "match_evidence=VALUES(match_evidence),matcher_version=VALUES(matcher_version),"
                "confirmed_by=VALUES(confirmed_by),confirmed_at=VALUES(confirmed_at),"
                "property_version=VALUES(property_version)",
                rows,
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "registry.property_small_community.confirm",
        target_type="registry_property",
        target_name="批量确认小区归属",
        detail={"property_count": len(property_ids), "small_community_count": len(entry_ids)},
        **request_audit_fields(request),
    )
    return {"message": f"已确认 {len(property_ids)} 套房屋的小区归属", "confirmed": len(property_ids)}


@router.post("/properties/export")
async def export_properties(
    data: PropertySearch,
    request: Request,
    user: dict = Depends(require_permission(REGISTRY_PROPERTY_VIEW)),
    conn=Depends(get_registry_db),
):
    result = await _property_search_result(data, user, conn, export_all=True)
    rows = result.get("data") or []
    workbook = build_xlsx(
        "房屋档案",
        [
            "房屋ID", "社区", "小区", "小区匹配状态", "标准详细地址", "街道", "幢", "室", "户号",
            "住房类型", "居住处所", "最近走访日期", "星级评定", "责任书状态",
            "房屋状态", "档案版本", "更新时间",
        ],
        [
            [
                row.get("id"), row.get("community_name"), row.get("small_community_name"),
                row.get("address_match_status"),
                row.get("natural_address") or row.get("normalized_address"),
                row.get("street"), row.get("building"), row.get("room"),
                row.get("source_house_no"), row.get("housing_type"),
                row.get("residence_type"), row.get("latest_visit_date") or "",
                row.get("latest_star_rating") or "", row.get("certificate_status_label") or "",
                row.get("status"), row.get("version"), row.get("updated_at") or "",
            ]
            for row in rows
        ],
    )
    await record_admin_audit(
        user,
        "registry.properties_export",
        target_type="registry_properties",
        target_name="房屋档案",
        detail={
            "file_format": "XLSX",
            "rows": len(rows),
            "sort": data.sort,
            "community_filtered": data.community_id is not None,
            "keyword_present": bool(data.keyword.strip()),
        },
        **request_audit_fields(request),
    )
    filename = f"房屋档案-{datetime.now():%Y%m%d%H%M%S}.xlsx"
    return StreamingResponse(
        workbook,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.post("/properties")
async def create_property(
    data: PropertyCreate,
    request: Request,
    user: dict = Depends(require_permission(REGISTRY_PROPERTY_MANAGE)),
    conn=Depends(get_registry_db),
):
    allowed = await _allowed_community_ids(user, REGISTRY_PROPERTY_MANAGE)
    from routers.registry_extended import _canonical_community
    async with conn.cursor() as cur:
        community_id, community_name = await _canonical_community(
            cur, data.community_id, data.community_name_snapshot
        )
    if allowed is not None and community_id not in allowed:
        raise HTTPException(403, "只能维护所属社区的房屋档案")
    normalized = data.normalized_address.strip() or " ".join(
        item for item in [data.street, data.natural_address, data.building, data.room] if item.strip()
    )
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO registry_properties "
                "(street, community_id, community_name_snapshot, natural_address, building, room, housing_type, residence_type, "
                "source_house_no, source_updated_at, source_type, source_ref, normalized_address, created_by, updated_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (data.street.strip(), community_id, community_name,
                 data.natural_address.strip(), data.building.strip(), data.room.strip(),
                 data.housing_type.strip(), data.residence_type.strip(), data.source_house_no.strip(),
                 data.source_updated_at, data.source_type.strip() or "manual", data.source_ref.strip(),
                 normalized, user["id"], user["id"]),
            )
            property_id = int(cur.lastrowid)
            await cur.execute(
                "INSERT INTO registry_property_address_versions "
                "(property_id, version_no, street, natural_address, building, room, normalized_address, "
                "effective_from, source_type, change_reason, changed_by) "
                "VALUES (%s,1,%s,%s,%s,%s,%s,UTC_TIMESTAMP(),'manual','初始建档',%s)",
                (property_id, data.street.strip(), data.natural_address.strip(), data.building.strip(),
                 data.room.strip(), normalized, user["id"]),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "registry.property.create",
        target_type="registry_property",
        target_name=str(property_id),
        detail={"community_id": community_id},
        **request_audit_fields(request),
    )
    return {"id": property_id, "message": "房屋档案已创建"}


@router.post("/properties/{property_id}/people")
async def attach_property_person(
    property_id: int,
    data: PropertyPersonRoleCreate,
    request: Request,
    user: dict = Depends(require_permission(REGISTRY_PROPERTY_MANAGE)),
    conn=Depends(get_registry_db),
):
    if data.valid_to and data.valid_from and data.valid_to < data.valid_from:
        raise HTTPException(422, "关系结束时间不能早于生效时间")
    allowed = await _allowed_community_ids(user, REGISTRY_PROPERTY_MANAGE)
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            from routers.registry_extended import _ensure_relation_interval_available
            await cur.execute("SELECT community_id FROM registry_properties WHERE id=%s FOR UPDATE", (property_id,))
            property_row = await cur.fetchone()
            if not property_row:
                raise HTTPException(404, "房屋档案不存在")
            if allowed is not None and property_row[0] not in allowed:
                raise HTTPException(403, "只能维护所属社区的房屋档案")
            await cur.execute("SELECT id FROM registry_housing_people WHERE id=%s AND status='active'", (data.person_id,))
            if not await cur.fetchone():
                raise HTTPException(404, "辖区人员档案不存在")
            await cur.execute("SELECT subject_type FROM registry_role_types WHERE id=%s AND is_active=1", (data.role_type_id,))
            role = await cur.fetchone()
            if not role or role[0] != "person":
                raise HTTPException(400, "该角色不能关联人员")
            await _ensure_relation_interval_available(
                cur,
                "registry_property_person_roles",
                "property_id=%s AND person_id=%s AND role_type_id=%s",
                (property_id, data.person_id, data.role_type_id),
                data.valid_from,
                data.valid_to,
            )
            await cur.execute(
                "INSERT INTO registry_property_person_roles "
                "(property_id, person_id, role_type_id, valid_from, valid_to, verified, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (property_id, data.person_id, data.role_type_id, data.valid_from,
                 data.valid_to, int(data.verified), user["id"]),
            )
            relation_id = int(cur.lastrowid)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "registry.property_person.attach",
        target_type="registry_property_person_role",
        target_name=str(relation_id),
        detail={"property_id": property_id, "person_id": data.person_id},
        **request_audit_fields(request),
    )
    return {"id": relation_id, "message": "房屋人员关系已保存"}


@router.get("/people")
async def list_housing_people(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    category_ids: list[int] = Query(default=[]),
    user: dict = Depends(require_permission(REGISTRY_PROPERTY_VIEW)),
    conn=Depends(get_registry_db),
):
    if len(category_ids) > 20:
        raise HTTPException(422, "人员标签筛选最多选择 20 项")
    can_view_tags = has_permission(user, REGISTRY_WATCH_VIEW)
    if category_ids and not can_view_tags:
        raise HTTPException(403, "无权查看人员标签")
    allowed = await _allowed_community_ids(user, REGISTRY_PROPERTY_VIEW)
    relation_filter = ""
    relation_params: list[object] = []
    if allowed is not None:
        if not allowed:
            return {"total": 0, "page": page, "page_size": page_size, "data": []}
        relation_filter = (
            " AND EXISTS (SELECT 1 FROM registry_property_person_roles rel "
            "JOIN registry_properties prop ON prop.id=rel.property_id "
            "WHERE rel.person_id=registry_housing_people.id "
            "AND prop.community_id IN (" + ",".join(["%s"] * len(allowed)) + "))"
        )
        relation_params.extend(allowed)
    category_ids = list(dict.fromkeys(category_ids))
    if category_ids:
        placeholders = ",".join(["%s"] * len(category_ids))
        relation_filter += (
            " AND EXISTS (SELECT 1 FROM watch_people watch_person "
            "JOIN watch_assignments assignment ON assignment.person_id=watch_person.id "
            "JOIN watch_categories category ON category.id=assignment.category_id "
            "WHERE (watch_person.registry_person_id=registry_housing_people.id "
            "OR (watch_person.registry_person_id IS NULL AND watch_person.identity_hmac=registry_housing_people.identity_hmac)) "
            f"AND assignment.category_id IN ({placeholders}) AND assignment.status='active' "
            "AND assignment.valid_from<=UTC_TIMESTAMP() "
            "AND (assignment.valid_to IS NULL OR assignment.valid_to>=UTC_TIMESTAMP()) "
            "AND (assignment.released_at IS NULL OR assignment.released_at>UTC_TIMESTAMP()) "
            "AND category.is_active=1)"
        )
        relation_params.extend(category_ids)
    offset = (page - 1) * page_size
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM registry_housing_people WHERE status='active'" + relation_filter,
            tuple(relation_params),
        )
        total = int((await cur.fetchone())[0])
        await cur.execute(
            "SELECT id, name, identity_number, is_temporary, verification_status, status, created_at, updated_at "
            "FROM registry_housing_people WHERE status='active'" + relation_filter + " ORDER BY id DESC LIMIT %s OFFSET %s",
            tuple(relation_params) + (page_size, offset),
        )
        rows = await cur.fetchall()
        categories_by_person = (
            await _load_registry_person_categories(cur, [int(row[0]) for row in rows])
            if can_view_tags else {}
        )
    include_identity = user.get("role") == "super_admin"
    return {"total": total, "page": page, "page_size": page_size,
            "data": [{**_person_payload(row, include_identity),
                      "categories": [
                          _registry_person_category_payload(item)
                          for item in categories_by_person.get(int(row[0]), [])
                      ]}
                     for row in rows]}


@router.post("/people")
async def create_housing_person(
    data: HousingPersonCreate,
    request: Request,
    user: dict = Depends(require_permission(REGISTRY_PROPERTY_MANAGE)),
    conn=Depends(get_registry_db),
):
    identity = normalize_identity(data.identity_number)
    if identity and user.get("role") != "super_admin":
        raise HTTPException(403, "身份证号只能由超级管理员录入")
    identity_hmac, hmac_version = hmac_digest(identity, kind="identity")
    async with conn.cursor() as cur:
        if identity_hmac:
            await cur.execute(
                "SELECT id FROM registry_housing_people WHERE identity_hmac=%s",
                (identity_hmac,),
            )
            if await cur.fetchone():
                raise HTTPException(409, "该身份证号已存在辖区人员档案")
        await cur.execute(
            "INSERT INTO registry_housing_people "
            "(name, identity_number, identity_hmac, identity_hmac_version, is_temporary, verification_status, created_by, updated_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (data.name.strip(), identity or None, identity_hmac, hmac_version,
             int(data.is_temporary), data.verification_status, user["id"], user["id"]),
        )
        person_id = int(cur.lastrowid)
    await record_admin_audit(
        user,
        "registry.person.create",
        target_type="registry_housing_person",
        target_name=str(person_id),
        detail={},
        **request_audit_fields(request),
    )
    return {"id": person_id, "message": "辖区人员档案已创建"}


async def _housing_people_search_result(
    data: RegistrySearch,
    user: dict,
    conn,
    *,
    export_all: bool = False,
) -> dict:
    can_view_tags = has_permission(user, REGISTRY_WATCH_VIEW)
    if data.category_ids and not can_view_tags:
        raise HTTPException(403, "无权查看人员标签")
    where = ["status='active'"]
    params: list[object] = []
    allowed = await _allowed_community_ids(user, REGISTRY_PROPERTY_VIEW)
    if allowed is not None:
        if not allowed:
            return {"total": 0, "page": data.page, "page_size": data.page_size, "data": []}
        where.append(
            "EXISTS (SELECT 1 FROM registry_property_person_roles rel "
            "JOIN registry_properties prop ON prop.id=rel.property_id "
            "WHERE rel.person_id=registry_housing_people.id "
            "AND prop.community_id IN (" + ",".join(["%s"] * len(allowed)) + "))"
        )
        params.extend(allowed)
    if data.name.strip():
        where.append("name LIKE %s")
        params.append(f"%{data.name.strip()}%")
    if data.identity_number.strip():
        if user.get("role") != "super_admin":
            raise HTTPException(403, "身份证号只能由超级管理员查询")
        digest, _ = hmac_digest(data.identity_number, kind="identity")
        where.append("identity_hmac=%s")
        params.append(digest)
    if data.phone.strip():
        digest, _ = hmac_digest(data.phone, kind="phone")
        where.append("EXISTS (SELECT 1 FROM registry_person_phones p WHERE p.person_id=registry_housing_people.id AND p.phone_hmac=%s)")
        params.append(digest)
    category_ids = list(dict.fromkeys(data.category_ids))
    if category_ids:
        placeholders = ",".join(["%s"] * len(category_ids))
        where.append(
            "EXISTS (SELECT 1 FROM watch_people watch_person "
            "JOIN watch_assignments assignment ON assignment.person_id=watch_person.id "
            "JOIN watch_categories category ON category.id=assignment.category_id "
            "WHERE (watch_person.registry_person_id=registry_housing_people.id "
            "OR (watch_person.registry_person_id IS NULL AND watch_person.identity_hmac=registry_housing_people.identity_hmac)) "
            f"AND assignment.category_id IN ({placeholders}) AND assignment.status='active' "
            "AND assignment.valid_from<=UTC_TIMESTAMP() "
            "AND (assignment.valid_to IS NULL OR assignment.valid_to>=UTC_TIMESTAMP()) "
            "AND (assignment.released_at IS NULL OR assignment.released_at>UTC_TIMESTAMP()) "
            "AND category.is_active=1)"
        )
        params.extend(category_ids)
    clause = " AND ".join(where)
    offset = (data.page - 1) * data.page_size
    async with conn.cursor() as cur:
        await cur.execute(f"SELECT COUNT(*) FROM registry_housing_people WHERE {clause}", tuple(params))
        total = int((await cur.fetchone())[0])
        await cur.execute(
            "SELECT id, name, identity_number, is_temporary, verification_status, status, created_at, updated_at "
            f"FROM registry_housing_people WHERE {clause} ORDER BY id DESC "
            + ("" if export_all else "LIMIT %s OFFSET %s"),
            tuple(params) if export_all else tuple(params) + (data.page_size, offset),
        )
        rows = await cur.fetchall()
        categories_by_person = (
            await _load_registry_person_categories(cur, [int(row[0]) for row in rows])
            if can_view_tags else {}
        )
    include_identity = user.get("role") == "super_admin"
    return {"total": total, "page": data.page, "page_size": data.page_size,
            "data": [{**_person_payload(row, include_identity),
                      "categories": [
                          _registry_person_category_payload(item)
                          for item in categories_by_person.get(int(row[0]), [])
                      ]}
                     for row in rows]}


@router.post("/people/search")
async def search_housing_people(
    data: RegistrySearch,
    user: dict = Depends(require_permission(REGISTRY_PROPERTY_VIEW)),
    conn=Depends(get_registry_db),
):
    return await _housing_people_search_result(data, user, conn)


@router.post("/people/export")
async def export_housing_people(
    data: RegistrySearch,
    request: Request,
    user: dict = Depends(require_permission(REGISTRY_PROPERTY_VIEW)),
    conn=Depends(get_registry_db),
):
    result = await _housing_people_search_result(data, user, conn, export_all=True)
    rows = result.get("data") or []
    workbook = build_xlsx(
        "人员档案",
        ["人员ID", "姓名", "身份证号", "临时人员", "核验状态", "人员标签", "档案状态", "更新时间"],
        [
            [
                row.get("id"), row.get("name"), row.get("identity_number") or "",
                "是" if row.get("is_temporary") else "否", row.get("verification_status"),
                "、".join(str(item.get("name") or "") for item in row.get("categories") or []),
                row.get("status"), row.get("updated_at") or "",
            ]
            for row in rows
        ],
    )
    await record_admin_audit(
        user,
        "registry.people_export",
        target_type="registry_people",
        target_name="人员档案",
        detail={
            "file_format": "XLSX",
            "rows": len(rows),
            "keyword_present": bool(data.name.strip()),
            "category_count": len(data.category_ids),
        },
        **request_audit_fields(request),
    )
    filename = f"人员档案-{datetime.now():%Y%m%d%H%M%S}.xlsx"
    return StreamingResponse(
        workbook,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/role-types")
async def list_role_types(
    user: dict = Depends(require_permission(REGISTRY_PROPERTY_VIEW)),
    conn=Depends(get_registry_db),
):
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, code, name, subject_type, is_active, sort_order "
            "FROM registry_role_types ORDER BY sort_order, id"
        )
        rows = await cur.fetchall()
    return {"data": [
        {"id": int(row[0]), "code": row[1], "name": row[2],
         "subject_type": row[3], "is_active": bool(row[4]), "sort_order": row[5]}
        for row in rows
    ]}


@router.get("/watch/categories")
async def list_watch_categories(
    user: dict = Depends(require_permission(REGISTRY_WATCH_VIEW)),
    conn=Depends(get_registry_db),
):
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, code, name, parent_id, color, alert_level, is_active, sort_order "
            "FROM watch_categories ORDER BY sort_order, id"
        )
        rows = await cur.fetchall()
    return {"data": [
        {"id": int(row[0]), "code": row[1], "name": row[2], "parent_id": row[3],
         "color": row[4], "alert_level": row[5], "is_active": bool(row[6]), "sort_order": row[7]}
        for row in rows
    ]}


@router.post("/watch/categories")
async def create_watch_category(
    data: WatchCategoryCreate,
    request: Request,
    user: dict = Depends(require_permission(REGISTRY_WATCH_MANAGE)),
    conn=Depends(get_registry_db),
):
    async with conn.cursor() as cur:
        try:
            await cur.execute(
                "INSERT INTO watch_categories (code, name, parent_id, color, alert_level, created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (data.code.strip(), data.name.strip(), data.parent_id, data.color,
                 data.alert_level, user["id"]),
            )
        except Exception as exc:
            if "duplicate" in str(exc).lower():
                raise HTTPException(409, "标记分类代码或名称已存在") from exc
            raise
        category_id = int(cur.lastrowid)
    await record_admin_audit(
        user,
        "registry.watch_category.create",
        target_type="watch_category",
        target_name=str(category_id),
        detail={"code": data.code.strip()},
        **request_audit_fields(request),
    )
    return {"id": category_id, "message": "人员标签分类已创建"}


@router.get("/watch/people")
async def list_watch_people(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    user: dict = Depends(require_permission(REGISTRY_WATCH_VIEW)),
    conn=Depends(get_registry_db),
):
    return await _watch_people_result(
        WatchPersonSearch(page=page, page_size=page_size),
        user,
        conn,
    )


@router.post("/watch/people/search")
async def search_watch_people(
    data: WatchPersonSearch,
    user: dict = Depends(require_permission(REGISTRY_WATCH_VIEW)),
    conn=Depends(get_registry_db),
):
    return await _watch_people_result(data, user, conn)


@router.post("/watch/people")
async def create_watch_person(
    data: WatchPersonCreate,
    request: Request,
    user: dict = Depends(require_permission(REGISTRY_WATCH_MANAGE)),
    conn=Depends(get_registry_db),
):
    identity = normalize_identity(data.identity_number)
    if identity and user.get("role") != "super_admin":
        raise HTTPException(403, "身份证号只能由超级管理员录入")
    identity_hmac, hmac_version = hmac_digest(identity, kind="identity")
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            if identity_hmac:
                await cur.execute("SELECT id FROM watch_people WHERE identity_hmac=%s", (identity_hmac,))
                if await cur.fetchone():
                    raise HTTPException(409, "该身份证号已存在人员标签档案")
            await cur.execute(
                "INSERT INTO watch_people "
                "(name, identity_number, identity_hmac, identity_hmac_version, is_temporary, verification_status, created_by, updated_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (data.name.strip(), identity or None, identity_hmac, hmac_version,
                 int(data.is_temporary), data.verification_status, user["id"], user["id"]),
            )
            person_id = int(cur.lastrowid)
            await ensure_watch_person_registry_link(
                cur,
                person_id,
                source_type="watch_manual",
                source_ref=f"watch_person:{person_id}",
                actor_id=user["id"],
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "registry.watch_person.create",
        target_type="watch_person",
        target_name=str(person_id),
        detail={},
        **request_audit_fields(request),
    )
    return {"id": person_id, "message": "人员标签档案已创建"}


@router.post("/watch/assignments")
async def create_watch_assignment(
    data: WatchAssignmentCreate,
    request: Request,
    user: dict = Depends(require_permission(REGISTRY_WATCH_MANAGE)),
    conn=Depends(get_registry_db),
):
    if data.valid_to and data.valid_to < data.valid_from:
        raise HTTPException(422, "标记结束时间不能早于生效时间")
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id FROM watch_people WHERE id=%s AND status='active' FOR UPDATE", (data.person_id,))
            if not await cur.fetchone():
                raise HTTPException(404, "人员标签档案不存在")
            await cur.execute("SELECT id FROM watch_categories WHERE id=%s AND is_active=1", (data.category_id,))
            if not await cur.fetchone():
                raise HTTPException(404, "标记分类不存在或已停用")
            await cur.execute(
                "INSERT INTO watch_assignments "
                "(person_id, category_id, valid_from, valid_to, basis, created_by, updated_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (data.person_id, data.category_id, data.valid_from, data.valid_to,
                 data.basis.strip(), user["id"], user["id"]),
            )
            assignment_id = int(cur.lastrowid)
            await cur.execute(
                "INSERT INTO watch_assignment_versions "
                "(assignment_id, version_no, snapshot_json, changed_by) "
                "VALUES (%s,1,%s,%s)",
                (assignment_id, "{}", user["id"]),
            )
        backfilled = await backfill_assignment_snapshots(conn, assignment_id)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "registry.watch_assignment.create",
        target_type="watch_assignment",
        target_name=str(assignment_id),
        detail={"person_id": data.person_id, "category_id": data.category_id},
        **request_audit_fields(request),
    )
    return {
        "id": assignment_id,
        "backfilled_snapshots": backfilled,
        "message": "人员标签已保存",
    }
