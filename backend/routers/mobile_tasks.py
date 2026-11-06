"""组长、组员使用的手机任务首页和卡片式处理接口。"""

from __future__ import annotations

from datetime import datetime
import json
import logging
from io import BytesIO
import time
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import load_workbook
from pydantic import BaseModel, Field

from database import get_db
from deps import get_current_user, require_permission
from routers.query import (
    _enabled_spreadsheets,
    _managed_column_metadata,
    _source_ready,
    _writeback_enabled,
    queue_source_fields,
)
from services.business_time import get_business_date
from services.data_scope import community_names_for_scopes
from services.online_edit_permissions import (
    effective_view_communities,
    formal_community,
    inspector_option_context,
    row_edit_capabilities,
)
from services.online_source import (
    active_source_sql_filter,
    json_value,
    rebuild_projection,
    rebuild_projection_rows,
    stable_json,
    _task_annotation_addresses,
)
from services.address_matching import MATCHER_VERSION
from services.address_match_feedback import (
    MANUAL_UNMATCHED_REASON_LABELS,
    annotation_hmac,
    record_feedback_confirmation,
    record_feedback_unmatched,
)
from services.online_local_writeback import (
    apply_local_system_changes,
    load_local_changes,
    local_sync_state,
    overlay_local_values,
)
from services.parsers import get_parser
from services.permissions import (
    ONLINE_RAW_EDIT,
    ONLINE_RAW_VIEW,
    ONLINE_TASK_MANAGE,
    QMF_REGISTRATION_EXECUTE,
    has_permission,
)
from services.report_overview import SUMMARY_TYPE, get_online_overview
from services.qmf_registration import (
    MODEL_THREE_PARSER,
    preview_capability,
    registration_capability,
)
from services.qmf_status import normalize_qmf_status_result
from services.qmf_config import load_qmf_config
from services.qmf_runs import WRITE_STEP_KEYS, parse_steps, utc_text
from services.task_workflow import (
    MOBILE_TASK_TYPES,
    PHONE_FIELD_ALIASES,
    SUMMARY_TASK_TYPES,
    TASK_WORKFLOWS,
)
from services.task_assignment_responsibility import (
    capture_first_assignment,
    record_internal_transfer,
)
from services.domain_events import stable_task_id
from services.task_commands import (
    TaskCommandService,
    TaskCommandSpec,
    TaskMutationResult,
    TaskAuthorizationResult,
)
from services.task_graph import online_task_blocked
from services.unverifiable_review import (
    ACTIVE_STATES as UNVERIFIABLE_ACTIVE_STATES,
    DEEP_EXTENSION,
    DEEP_PENDING,
    FINAL_UNVERIFIABLE,
    INITIAL_EXTENSION,
    INITIAL_PENDING,
    STATE_LABELS as UNVERIFIABLE_STATE_LABELS,
UNVERIFIABLE_REVIEW_TYPES,
is_unverifiable_result,
    apply_decision,
    is_unverifiable_result,
    prepare_decision,
    NEW_CLUE,
    NO_NEW_CLUE,
    review_events_for_flow,
    review_flows_by_rows,
    supports_unverifiable_review,
)
from services.audit import record_admin_audit, request_audit_fields
from services.xlsx_export import XLSX_MEDIA_TYPE, build_xlsx
from config import settings
from services.local_source import local_data_source_enabled, local_row_hash
from services.watch_matching import task_watch_payload
from services.residence_platform import ResidencePlatformError
from services.residence_status_scan import (
    residence_detail_for_values,
    residence_status_by_rows,
    wake_residence_lookup_scheduler,
)
from services.task_registration import (
    REGISTRATION_TASK_TYPES,
    cancel_registration_link,
    save_pending_registration_address,
    is_registration_task,
    migrate_registration_link,
    refresh_registration_source_context_after_writeback,
    registration_links_by_rows,
    select_registration_property,
    validate_registration_property,
    record_registration_event,
)
from services.residence_platform_config import load_residence_config
from services.residence_status_scan import _load_current_target, _lookup_registration_address_target


router = APIRouter(prefix="/api/mobile-tasks", tags=["手机任务工作台"])


def _require_legacy_read_model_disabled() -> None:
    if settings.TASK_READ_MODEL_ENABLED:
        raise HTTPException(503, {"error": "projection_pending"})


def _task_index_filters(
    parser_type: str, *, communities: list[str] | None = None,
    inspectors: list[str] | None = None, results: list[str] | None = None,
) -> dict[str, list[str]]:
    parser = get_parser(parser_type)
    filters: dict[str, list[str]] = {}
    if communities:
        filters[parser.COMMUNITY_COLUMN] = list(communities)
    if inspectors:
        filters["核查人"] = list(inspectors)
    if results:
        filters[TASK_WORKFLOWS[parser_type].result_field] = list(results)
    return filters


async def _task_read_model_records(
    conn,
    parser_type: str,
    context: dict,
    *,
    scope: FlowScope,
    keyword: str = "",
    filters: dict[str, list[str]] | None = None,
) -> list[dict]:
    """Load task facts from Redis and use MySQL only to verify source identity.

    Community and inspector indexes narrow the Redis query. The returned
    payload always comes from the encrypted read model; the source lookup does
    not read business values and rejects missing or stale projections.
    """
    if not settings.TASK_READ_MODEL_ENABLED:
        raise RuntimeError("task read model is not enabled")
    if scope == "all" and not context.get("admin_mode"):
        raise HTTPException(403, "组员和组长只能查看本人或本社区任务")
    allowed_communities = context.get("community_values")
    if allowed_communities is not None and not allowed_communities:
        return []
    parser = get_parser(parser_type)

    run_id = str(settings.TASK_READ_MODEL_RUN_ID or "").strip()
    secret = str(settings.TASK_EVENT_ENCRYPTION_KEY or "")
    if not run_id or len(secret.encode()) < 32:
        raise HTTPException(503, {"error": "projection_pending"})

    try:
        import redis.asyncio as redis
        from services.task_read_model import require_task_read_model_ready

        client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
        try:
            await client.ping()
            await require_task_read_model_ready(
                client, settings.APP_ENVIRONMENT, run_id,
            )
            records = await _scan_task_read_model_records(
                client,
                parser=parser,
                allowed_communities=allowed_communities,
                scope=scope,
                inspector=str(context.get("name") or ""),
                keyword=keyword,
                filters=filters,
                environment=settings.APP_ENVIRONMENT,
                run_id=run_id,
            )
        finally:
            await client.aclose()

        physical_ids: list[int] = []
        record_by_physical: dict[int, list[dict]] = {}
        for record in records:
            task_id = str(record.get("task_id") or "")
            table_name, separator, raw_physical = task_id.rpartition(":")
            if not separator or table_name != parser.table_name or not raw_physical.isdigit():
                raise ValueError("task identity mismatch")
            physical_row = int(raw_physical)
            if physical_row < 1:
                raise ValueError("task identity mismatch")
            physical_ids.append(physical_row)
            record_by_physical.setdefault(physical_row, []).append(record)

        locators: dict[tuple[int, int], dict] = {}
        locator_counts: dict[int, int] = {}
        async with conn.cursor() as cur:
            for offset in range(0, len(physical_ids), 400):
                batch = list(dict.fromkeys(physical_ids[offset:offset + 400]))
                if not batch:
                    continue
                placeholders = ",".join(["%s"] * len(batch))
                await cur.execute(
                    "SELECT source.id,source.physical_row,source.row_key,source.revision,source.row_hash "
                    "FROM _online_source_rows AS source "
                    "WHERE source.parser_type=%s AND source.physical_row IN ("
                    + placeholders + ") AND source.archived_at IS NULL "
                    + active_source_sql_filter(parser_type, "source"),
                    [parser_type, *batch],
                )
                for source_id, physical_row, row_key, revision, row_hash in await cur.fetchall():
                    locator_counts[int(physical_row)] = locator_counts.get(int(physical_row), 0) + 1
                    locators[(int(physical_row), int(source_id))] = {
                        "row_key": str(row_key),
                        "revision": int(revision or 0),
                        "row_hash": str(row_hash or ""),
                    }

        by_row_key: dict[str, list[dict]] = {}
        allowed_set = set(allowed_communities or [])
        for physical_row, same_task_records in record_by_physical.items():
            if locator_counts.get(physical_row, 0) != len(same_task_records):
                raise ValueError("task source identity is ambiguous")
            for record in same_task_records:
                source_id = int(record.get("source_id") or 0)
                locator = locators.get((physical_row, source_id))
                if (
                    locator is None
                    or locator["revision"] != int(record.get("revision") or 0)
                    or not locator["row_key"]
                    or locator["row_key"] != record.get("row_key")
                ):
                    raise ValueError("task source revision mismatch")
                values = record.get("values")
                if not isinstance(values, dict):
                    raise ValueError("task snapshot missing")
                community = parser.community_value(values).strip()
                inspector = str(values.get("核查人") or "").strip()
                if allowed_communities is not None and community not in allowed_set:
                    raise ValueError("task scope identity mismatch")
                if scope == "mine" and inspector.casefold() != str(context["name"]).strip().casefold():
                    raise ValueError("task scope identity mismatch")
                by_row_key.setdefault(locator["row_key"], []).append({
                    **record,
                    "physical_row": physical_row,
                    "row_key": locator["row_key"],
                    "source_row_hash": locator["row_hash"],
                })
        source_counts: dict[str, int] = {}
        row_keys = list(by_row_key)
        for offset in range(0, len(row_keys), 400):
            batch = row_keys[offset:offset + 400]
            if not batch:
                continue
            placeholders = ",".join(["%s"] * len(batch))
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT row_key,COUNT(*) FROM _online_source_rows AS source "
                    "WHERE parser_type=%s AND row_key IN (" + placeholders + ") "
                    "AND source.archived_at IS NULL "
                    + active_source_sql_filter(parser_type, "source")
                    + " GROUP BY row_key",
                    [parser_type, *batch],
                )
                source_counts.update({
                    str(row_key): int(count or 0)
                    for row_key, count in await cur.fetchall()
                })
        result: list[dict] = []
        for row_key, sources in by_row_key.items():
            sources.sort(key=lambda source: (int(source["source_id"]), str(source["task_id"])))
            canonical = dict(sources[0])
            canonical_values = canonical.get("values") or {}
            canonical["sources"] = sources
            total_sources = source_counts.get(row_key, 0)
            if total_sources != len(sources) or total_sources == 0:
                raise ValueError("task source identity is inconsistent")
            canonical["source_count"] = total_sources
            canonical["conflict"] = any(
                stable_json(source.get("values") or {}) != stable_json(canonical_values)
                for source in sources[1:]
            )
            canonical["row_key"] = row_key
            canonical["parser_type"] = parser_type
            result.append(canonical)
        return result
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, {"error": "projection_pending"}) from None


async def _scan_task_read_model_records(
    client,
    *,
    parser,
    allowed_communities: list[str] | None,
    scope: FlowScope,
    inspector: str,
    keyword: str,
    filters: dict[str, list[str]] | None,
    environment: str,
    run_id: str,
) -> list[dict]:
    from services.task_read_model import (
        query_task_read_model_group_ids,
        read_task_read_model_group_records,
    )
    records = []
    query_filters = {field: list(values) for field, values in (filters or {}).items() if values}
    if scope == "mine":
        scoped_inspector = inspector.strip()
        requested_inspectors = query_filters.get("核查人")
        if requested_inspectors:
            query_filters["核查人"] = [
                value for value in requested_inspectors
                if value.strip().casefold() == scoped_inspector.casefold()
            ]
            if not query_filters["核查人"]:
                return []
        elif scoped_inspector:
            query_filters["核查人"] = [scoped_inspector]
        else:
            return []

    total = None
    offset = 0
    page_size = 400
    allowed_set = None if allowed_communities is None else set(allowed_communities)
    needle = keyword.strip().casefold()
    while total is None or offset < total:
        group_page = await query_task_read_model_group_ids(
            client, environment, run_id, parser.table_name, query_filters,
            allowed_communities, keyword if len(needle) >= 2 else None,
            offset, page_size,
        )
        page_total = group_page.get("total")
        group_tokens = group_page.get("group_tokens")
        if (type(page_total) is not int or page_total < 0
                or not isinstance(group_tokens, list)
                or len(group_tokens) != min(page_size, max(0, page_total - offset))):
            raise ValueError("task read-model group page contract invalid")
        if total is None:
            total = page_total
        elif page_total != total:
            raise ValueError("task read-model group total changed")
        if not group_tokens:
            break
        groups = await read_task_read_model_group_records(
            client, environment, run_id, parser.table_name, group_tokens,
        )
        if not isinstance(groups, list) or len(groups) != len(group_tokens):
            raise ValueError("task read-model group page mismatch")
        for group in groups:
            sources = group.get("sources")
            canonical = group.get("values")
            row_key = group.get("row_key")
            if (not isinstance(sources, list) or not sources
                    or not isinstance(canonical, dict) or not isinstance(row_key, str)
                    or group.get("source_count") != len(sources)
                    or canonical != sources[0].get("values")
                    or parser.make_row_key(canonical) != row_key):
                raise ValueError("task read-model group identity mismatch")
            if any(
                not isinstance(source, dict)
                or not isinstance(source.get("values"), dict)
                or not isinstance(source.get("task_id"), str)
                or type(source.get("source_id")) is not int
                or type(source.get("revision")) is not int
                for source in sources
            ):
                raise ValueError("task read-model group source invalid")
            ordered = sorted(sources, key=lambda source: (source["source_id"], source["task_id"]))
            if sources != ordered:
                raise ValueError("task read-model group source order invalid")
            communities = [parser.community_value(source["values"]).strip() for source in sources]
            if allowed_set is not None and any(value not in allowed_set for value in communities):
                raise ValueError("task read-model group scope mismatch")
            if scope == "mine" and any(
                str(source["values"].get("核查人") or "").strip().casefold()
                != inspector.strip().casefold()
                for source in sources
            ):
                continue
            if any(
                str(canonical.get(field, "") or "") not in accepted
                for field, accepted in query_filters.items()
            ):
                continue
            if needle and not any(needle in str(value or "").casefold() for value in canonical.values()):
                continue
            for source in sources:
                records.append({
                    **source,
                    "row_key": row_key,
                    "source_count": len(sources),
                    "conflict": bool(group.get("conflict")),
                })
        offset += len(group_tokens)
    return records


async def _read_task_snapshots_for_source_rows(
    parser_type: str,
    source_rows: list[tuple],
) -> dict[int, dict]:
    """Read exact source snapshots from the active Redis generation."""
    if not settings.TASK_READ_MODEL_ENABLED:
        raise RuntimeError("task read model is not enabled")
    run_id = str(settings.TASK_READ_MODEL_RUN_ID or "").strip()
    secret = str(settings.TASK_EVENT_ENCRYPTION_KEY or "")
    if not run_id or len(secret.encode()) < 32:
        raise HTTPException(503, {"error": "projection_pending"})
    try:
        import redis.asyncio as redis
        from services.task_read_model import (
            read_task_snapshot, require_task_read_model_ready, task_read_model_keys,
        )

        client = redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
        try:
            await client.ping()
            await require_task_read_model_ready(
                client, settings.APP_ENVIRONMENT, run_id,
            )
            snapshots: dict[int, dict] = {}
            for offset in range(0, len(source_rows), 200):
                batch = source_rows[offset:offset + 200]
                pipe = client.pipeline(transaction=False)
                keyed_rows = []
                for source in batch:
                    source_id, physical_row, revision = int(source[0]), int(source[1]), int(source[2])
                    task_id = stable_task_id(get_parser(parser_type).table_name, physical_row)
                    task_key = task_read_model_keys(
                        settings.APP_ENVIRONMENT, run_id, task_id,
                    )[0]
                    keyed_rows.append((source_id, task_id, task_key, revision))
                    pipe.hgetall(task_key)
                stored_rows = await pipe.execute()
                if not isinstance(stored_rows, (list, tuple)) or len(stored_rows) != len(keyed_rows):
                    raise ValueError("task snapshot read failed")
                for (source_id, task_id, task_key, expected_revision), stored in zip(keyed_rows, stored_rows):
                    if (
                        not stored
                        or stored.get("tombstone") == "1"
                        or stored.get("source_id") != str(source_id)
                        or int(stored.get("revision", 0) or 0) != expected_revision
                    ):
                        raise ValueError("task snapshot identity mismatch")
                    values = await read_task_snapshot(
                        client,
                        task_key,
                        environment=settings.APP_ENVIRONMENT,
                        run_id=run_id,
                        task_id=task_id,
                        event_id=stored.get("event_id", ""),
                        command_id=stored.get("command_id", ""),
                        revision=expected_revision,
                        source_id=source_id,
                    )
                    if not isinstance(values, dict):
                        raise ValueError("task snapshot missing")
                    snapshots[source_id] = values
        finally:
            await client.aclose()
        return snapshots
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, {"error": "projection_pending"}) from None


async def _read_task_view_states_for_records(records: list[dict]) -> dict[str, dict]:
    if not settings.TASK_READ_MODEL_ENABLED:
        raise RuntimeError("task read model is not enabled")
    run_id = str(settings.TASK_READ_MODEL_RUN_ID or "").strip()
    if not run_id:
        raise HTTPException(503, {"error": "projection_pending"})
    tasks = [
        {"task_id": str(record.get("task_id") or ""),
         "revision": int(record.get("revision") or 0)}
        for record in records
    ]
    if not tasks:
        return {}
    try:
        import redis.asyncio as redis
        from services.task_read_model import require_task_read_model_ready
        from services.task_view_state import read_task_view_states_for_tasks

        client = redis.from_url(
            settings.REDIS_URL, decode_responses=True,
            socket_connect_timeout=3, socket_timeout=5,
        )
        try:
            await client.ping()
            await require_task_read_model_ready(
                client, settings.APP_ENVIRONMENT, run_id,
            )
            return await read_task_view_states_for_tasks(
                client, environment=settings.APP_ENVIRONMENT,
                run_id=run_id, tasks=tasks,
            )
        finally:
            await client.aclose()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, {"error": "projection_pending"}) from None


async def _mobile_filter_options_from_read_model(
    conn,
    parser_type: str,
    records: list[dict],
    context: dict,
    user: dict,
    *,
    communities: list[str] | None = None,
    small_communities: list[str] | None = None,
    match_statuses: list[str] | None = None,
    results: list[str] | None = None,
    review_stage: ReviewStage = "all",
    include_assignment_metadata: bool = True,
) -> dict:
    parser = get_parser(parser_type)
    workflow = TASK_WORKFLOWS[parser_type]
    states_by_task = await _read_task_view_states_for_records(records)

    entries = []
    for record in records:
        values = record["values"]
        row_key = str(record["row_key"])
        domains = states_by_task[str(record["task_id"])]
        review_flow = domains["review"]["state"]
        stage = str(review_flow.get("state")
                    or workflow.review_stage(values))
        if review_stage != "all" and stage != review_stage:
            continue
        community = parser.community_value(values).strip()
        inspector = str(values.get("核查人") or "").strip()
        result = str(values.get(workflow.result_field) or "").strip()
        entries.append({
            "row_key": row_key, "community": community, "inspector": inspector,
            "result": result,
            "address_match": domains["address_match"]["state"],
            "watch": domains["watch"]["state"],
        })

    normalized_communities = list(dict.fromkeys(
        str(value).strip() for value in (communities or []) if str(value).strip()
    ))
    for entry in entries:
        candidate = entry["address_match"].get("candidate") or {}
        entry["address_match"] = {
            **entry["address_match"],
            "small_community_name": candidate.get("name") or candidate.get("community_name") or "",
        }
        entry["small_community"] = str(
            entry["address_match"].get("small_community_name") or ""
        )
        entry["match_status"] = str(entry["address_match"].get("status") or "unmatched")
    community_entries = [
        entry for entry in entries
        if not normalized_communities or entry["community"] in normalized_communities
    ]
    normalized_small_communities = set(small_communities or [])
    normalized_match_statuses = set(match_statuses or [])
    scoped_entries = [
        entry for entry in community_entries
        if (not normalized_small_communities
            or str(entry["address_match"].get("small_community_name") or "")
            in normalized_small_communities)
        and (not normalized_match_statuses
             or str(entry["address_match"].get("status") or "unmatched")
             in normalized_match_statuses)
    ]
    normalized_results = set(results or [])
    inspector_entries = [
        entry for entry in scoped_entries
        if not normalized_results or entry["result"] in normalized_results
    ]

    def options_for(key: str, rows: list[dict], empty_label: str) -> list[dict]:
        counts: dict[str, int] = {}
        for entry in rows:
            value = str(entry.get(key) or "").strip()
            counts[value] = counts.get(value, 0) + 1
        return [
            {"value": value or EMPTY_FILTER_VALUE,
             "label": value or empty_label, "count": count}
            for value, count in sorted(counts.items(), key=lambda pair: (not pair[0], pair[0]))
        ]

    result_counts: dict[str, int] = {}
    for entry in scoped_entries:
        result_counts[entry["result"]] = result_counts.get(entry["result"], 0) + 1
    result_options = [
        {"value": "" if not value else value,
         "label": value or "未填写", "count": result_counts.get(value, 0)}
        for value in ["", *workflow.result_options]
    ]
    small_community_options = options_for(
        "small_community", community_entries, "小区未关联",
    )
    match_status_options = options_for(
        "match_status", community_entries, "未匹配",
    )
    assignment_enabled = (
        include_assignment_metadata
        and _can_assign_tasks(context)
        and (has_permission(user, ONLINE_RAW_EDIT) or has_permission(user, ONLINE_TASK_MANAGE))
    )
    if assignment_enabled:
        async with conn.cursor() as cur:
            assignment_context = await inspector_option_context(
                cur, _task_capability_user(user), assignment_only=True,
            )
    else:
        assignment_context = {"community_aliases": {}, "inspectors_by_community": {}}
    category_counts: dict[int, dict] = {}
    for entry in entries:
        seen_categories: set[int] = set()
        for mark in entry["watch"].get("watch_marks", []):
            if not isinstance(mark, dict):
                continue
            try:
                category_id = int(mark.get("category_id"))
            except (TypeError, ValueError):
                continue
            if category_id in seen_categories:
                continue
            seen_categories.add(category_id)
            item = category_counts.setdefault(category_id, {
                "value": category_id, "label": str(mark.get("name") or ""),
                "color": str(mark.get("color") or ""),
                "alert_level": str(mark.get("alert_level") or ""), "count": 0,
            })
            item["count"] += 1
    watch_categories = sorted(
        category_counts.values(), key=lambda item: (item["label"], item["value"]),
    )
    return {
        "communities": options_for("community", entries, "社区未填写"),
        "small_communities": small_community_options,
        "match_statuses": match_status_options,
        "results": result_options,
        "inspectors": options_for("inspector", inspector_entries, "未分配核查人"),
        "watch_categories": watch_categories,
        "assignment": {
            "enabled": assignment_enabled,
            "community_aliases": assignment_context["community_aliases"],
            "inspectors_by_community": assignment_context["inspectors_by_community"],
        },
    }


def _merge_mobile_filter_option_batches(target: dict, current: dict) -> None:
    for key in ("communities", "small_communities", "match_statuses", "results", "inspectors", "watch_categories"):
        values_by_key = {
            str(item.get("value")): item for item in target.get(key, [])
        }
        for item in current.get(key, []):
            identity = str(item.get("value"))
            if identity in values_by_key:
                values_by_key[identity]["count"] = (
                    int(values_by_key[identity].get("count") or 0)
                    + int(item.get("count") or 0)
                )
            else:
                values_by_key[identity] = dict(item)
        values = list(values_by_key.values())
        if key == "watch_categories":
            values.sort(key=lambda item: (str(item.get("label") or ""), str(item.get("value") or "")))
        else:
            values.sort(key=lambda item: (
                not bool(str(item.get("value") or "").strip()),
                str(item.get("value") or "").casefold(),
            ))
        target[key] = values

    target["assignment"] = current.get("assignment", target.get("assignment", {
        "enabled": False, "community_aliases": {}, "inspectors_by_community": {},
    }))


@router.get("/my-history")
async def my_task_history(
    limit: int = Query(default=100, ge=1, le=200),
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Return the current user's recent task operations, without sensitive payloads."""
    user_id = int(user.get("id") or 0)
    if not user_id:
        return {"data": []}
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id,action,target_type,target_name,result,created_at "
            "FROM _admin_audit_log WHERE user_id=%s "
            "AND (target_type IN ('online_task','mobile_task','online_source') "
            "OR action LIKE 'mobile_tasks.%%' OR action LIKE 'online.local_%%') "
            "ORDER BY id DESC LIMIT %s",
            (user_id, limit),
        )
        rows = await cur.fetchall()
    return {"data": [
        {
            "id": int(row[0]), "action": str(row[1] or ""),
            "target_type": str(row[2] or ""), "task_key": str(row[3] or ""),
            "result": str(row[4] or ""),
            "created_at": row[5].isoformat() + "Z" if row[5] else "",
        }
        for row in rows
    ]}
logger = logging.getLogger(__name__)
FlowScope = Literal["mine", "community", "all"]
TaskStatus = Literal[
    "pending",
    "unchecked",
    "checked",
    "review",
    "registration_review",
    "completed",
    "all",
]
ReviewStage = Literal[
    "all", "waiting_analysis", "analyzed",
    "initial_pending", "initial_extension", "deep_pending", "deep_extension",
]
Priority = Literal[
    "all",
    "analyzed",
    "source_exception",
    "pending_sync",
    "ordinary",
    "waiting_analysis",
    "completed",
]
SortMode = Literal[
    "priority",
    "address_asc",
    "identity_asc",
    "updated_desc",
    "updated_asc",
]
AssignmentMode = Literal["single", "balanced"]
QmfFeedbackState = Literal[
    "not_scanned",
    "stale",
    "pending",
    "completed_match",
    "completed_mismatch",
    "not_found",
    "non_jurisdiction",
    "error",
]
EMPTY_FILTER_VALUE = "__empty__"
MAX_BULK_ASSIGNMENT_TASKS = 2000
MAX_BULK_ASSIGNMENT_CHUNK = 100


async def _task_source_ready(cur, parser_type: str) -> bool:
    """Local mode is ready when the local projection exists, not when a Tencent
    spreadsheet configuration exists.  An empty local table is still a valid
    ready state and should render an empty task pool rather than a sync error.
    """
    if local_data_source_enabled():
        await cur.execute(
            "SELECT 1 FROM _online_source_rows AS source "
            "WHERE source.parser_type=%s AND source.archived_at IS NULL "
            f"{active_source_sql_filter(parser_type, 'source')} LIMIT 1",
            (parser_type,),
        )
        await cur.fetchone()
        return True
    return await _source_ready(cur, await _enabled_spreadsheets(cur, parser_type))


async def _active_source_counts(cur, parser_type: str, row_keys: list[str]) -> dict[str, int]:
    if not local_data_source_enabled() or not row_keys:
        return {}
    placeholders = ",".join(["%s"] * len(row_keys))
    await cur.execute(
        "SELECT row_key, COUNT(*) FROM _online_source_rows AS source "
        "WHERE source.parser_type=%s AND source.row_key IN (" + placeholders + ") "
        "AND source.archived_at IS NULL "
        f"{active_source_sql_filter(parser_type, 'source')} GROUP BY row_key",
        [parser_type, *row_keys],
    )
    return {
        str(row_key): int(count or 0)
        for row_key, count in await cur.fetchall()
    }


async def _qmf_registration_state(
    conn,
    *,
    parser_type: str,
    sources: list[dict],
    user: dict,
) -> tuple[dict | None, dict | None]:
    """Return a private resumable run plus the public successful summary.

    Only users with the全民防 registration permission receive prepared,
    executing, failed or uncertain details. Other task viewers can only see
    that the external feedback succeeded and when it completed.
    """
    if parser_type != MODEL_THREE_PARSER or not sources:
        return None, None
    source_ids = [int(item["id"]) for item in sources]
    placeholders = ",".join(["%s"] * len(source_ids))
    latest_run = None
    async with conn.cursor() as cur:
        if has_permission(user, QMF_REGISTRATION_EXECUTE):
            await cur.execute(
                "UPDATE _qmf_registration_runs "
                "SET status='expired', result_code='prepare_expired' "
                f"WHERE parser_type=%s AND source_id IN ({placeholders}) "
                "AND requested_by=%s AND status='prepared' "
                "AND expires_at<=UTC_TIMESTAMP()",
                (parser_type, *source_ids, int(user["id"])),
            )
            await cur.execute(
                "SELECT id, source_id, expected_revision, status, steps_json, "
                "result_code, tencent_marker_status, tencent_marker_error, "
                "prepared_at, expires_at, execution_started_at, completed_at, "
                "created_at, updated_at "
                f"FROM _qmf_registration_runs WHERE parser_type=%s "
                f"AND source_id IN ({placeholders}) AND requested_by=%s "
                "ORDER BY id DESC LIMIT 1",
                (parser_type, *source_ids, int(user["id"])),
            )
            row = await cur.fetchone()
            if row:
                status = str(row[3] or "")
                marker_status = str(row[6] or "not_started")
                steps = parse_steps(row[4])
                write_progress = any(
                    item["key"] in WRITE_STEP_KEYS
                    and item["status"] in {"sending", "succeeded", "uncertain"}
                    for item in steps
                )
                latest_run = {
                    "id": int(row[0]),
                    "parser_type": parser_type,
                    "source_id": int(row[1]),
                    "expected_revision": int(row[2]),
                    "status": status,
                    "steps": steps,
                    "result_code": str(row[5] or ""),
                    "photo": {"sha256": "", "mime_type": "", "size_bytes": 0},
                    "tencent_marker_status": marker_status,
                    "tencent_marker_error": str(row[7] or ""),
                    "prepared_at": utc_text(row[8]),
                    "expires_at": utc_text(row[9]),
                    "execution_started_at": utc_text(row[10]),
                    "completed_at": utc_text(row[11]),
                    "created_at": utc_text(row[12]),
                    "updated_at": utc_text(row[13]),
                    "can_execute": status == "prepared",
                    "can_reprepare": status in {"failed", "uncertain"}
                    and not write_progress,
                    "can_retry_marker": status == "succeeded" and marker_status in {
                        "not_started", "pending", "conflict", "failed",
                    },
                }
        await cur.execute(
            "SELECT id, completed_at, tencent_marker_status "
            f"FROM _qmf_registration_runs WHERE parser_type=%s "
            f"AND source_id IN ({placeholders}) AND status='succeeded' "
            "ORDER BY id DESC LIMIT 1",
            (parser_type, *source_ids),
        )
        succeeded = await cur.fetchone()
    feedback = None
    if succeeded:
        feedback = {
            "run_id": int(succeeded[0]),
            "status": "succeeded",
            "completed_at": utc_text(succeeded[1]),
            "tencent_marker_status": str(succeeded[2] or "not_started"),
        }
    return latest_run, feedback


PRIORITY_KEYS = (
    "analyzed",
    "source_exception",
    "pending_sync",
    "ordinary",
    "waiting_analysis",
    "completed",
)
PRIORITY_LABELS = {
    "analyzed": "已研判",
    "source_exception": "来源异常",
    "pending_sync": "待同步",
    "ordinary": "普通待处理",
    "waiting_analysis": "等待研判",
    "completed": "已完成",
}
FLOW_TASK_ELEVATED_POSITIONS = {
    "片长",
    "基础管控",
    "中队长",
    "社区民警",
    "所队领导",
}


class TaskBatchUpdate(BaseModel):
    changes: dict[str, str]
    base_values: dict[str, str] = Field(default_factory=dict)
    expected_revision: int = Field(gt=0)
    registration_property_id: int | None = Field(default=None, gt=0)
    registration_property_version: int | None = Field(default=None, gt=0)
    registration_pending_address: str | None = Field(default=None, max_length=500)


class UnverifiableDecision(BaseModel):
    """Structured two-stage decision for an unresolved task."""

    stage: Literal["initial_pending", "deep_pending"]
    outcome: Literal["success", "failure", "new_clue", "no_new_clue"]
    opinion: str = Field(min_length=1, max_length=2000)
    flow_version: int = Field(gt=0)
    expected_revision: int = Field(gt=0)
    expected_row_hash: str = Field(min_length=1, max_length=128)


class RegistrationManualConfirm(BaseModel):
    reason: Literal["address_mismatch", "address_ambiguous"]
    note: str = Field(default="", max_length=500)
    expected_revision: int = Field(gt=0)


class AddressMatchConfirm(BaseModel):
    source_id: int = Field(gt=0)
    small_community_id: int = Field(gt=0)
    # Address annotation is a write against the current local task snapshot.
    # Require the caller to fence the decision with the source version so a
    # stale confirmation cannot overwrite a newer edit.
    expected_revision: int = Field(gt=0)
    expected_row_hash: str = Field(min_length=1, max_length=128)


class AddressMatchManualUnmatched(BaseModel):
    """固定原因的人工“无匹配小区”结论。"""

    source_id: int = Field(gt=0)
    reason_code: Literal[
        "insufficient_address",
        "outside_existing_communities",
        "community_registry_missing",
        "outside_task_community",
        "other_review_required",
    ]
    expected_revision: int = Field(gt=0)
    expected_row_hash: str = Field(min_length=1, max_length=128)


class AddressMatchConflictResolution(BaseModel):
    """管理员确认冲突候选后，将任务社区改为候选正式社区并重新匹配。"""

    source_id: int = Field(gt=0)
    small_community_id: int = Field(gt=0)
    expected_revision: int = Field(gt=0)
    expected_row_hash: str = Field(min_length=1, max_length=128)


def _validate_address_source_identity(actual_source_id: int, expected_source_id: int) -> None:
    """Fence address writes to the exact source row shown to the operator."""
    if int(actual_source_id) != int(expected_source_id):
        raise HTTPException(409, "任务来源已变化，请刷新后重试")


class SyncConflictResolution(BaseModel):
    choice: Literal["platform", "tencent"]
    fields: list[str] = Field(min_length=1, max_length=5)


class BulkAssignmentRequest(BaseModel):
    row_keys: list[str] = Field(min_length=1, max_length=MAX_BULK_ASSIGNMENT_CHUNK)
    inspector: str = Field(default="", max_length=100)
    mode: AssignmentMode = "single"
    balanced_offset: int = Field(default=0, ge=0)
    balanced_total: int = Field(default=0, ge=0, le=MAX_BULK_ASSIGNMENT_TASKS)


class CancelAssignmentsRequest(BaseModel):
    """按业务和可选社区撤销尚未完成任务的核查人分配。"""

    community: str = Field(default="", max_length=200)


class InternalTransferRequest(BaseModel):
    target_community: str = Field(min_length=1, max_length=200)
    target_leader: str = Field(default="", max_length=100)
    expected_row_key: str = Field(min_length=1, max_length=500)
    expected_revision: int = Field(gt=0)
    expected_row_hash: str = Field(min_length=1, max_length=128)


class TaskSearch(BaseModel):
    scope: FlowScope = "mine"
    status: TaskStatus = "pending"
    review_stage: ReviewStage = "all"
    communities: list[str] = Field(default_factory=list, max_length=50)
    small_communities: list[str] = Field(default_factory=list, max_length=50)
    match_status: list[str] = Field(default_factory=list, max_length=20)
    results: list[str] = Field(default_factory=list, max_length=30)
    inspectors: list[str] = Field(default_factory=list, max_length=50)
    watch_categories: list[int] = Field(default_factory=list, max_length=50)
    qmf_feedback_states: list[QmfFeedbackState] = Field(
        default_factory=list, max_length=10
    )
    priority: Priority = "all"
    sort: SortMode = "priority"
    keyword: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=50)


class AnalysisTaskSearch(BaseModel):
    parser_types: list[str] = Field(min_length=1, max_length=20)
    scope: FlowScope = "all"
    review_stage: ReviewStage = "all"
    communities: list[str] = Field(default_factory=list, max_length=50)
    small_communities: list[str] = Field(default_factory=list, max_length=50)
    match_status: list[str] = Field(default_factory=list, max_length=20)
    inspectors: list[str] = Field(default_factory=list, max_length=50)
    watch_categories: list[int] = Field(default_factory=list, max_length=50)
    sort: SortMode = "priority"
    keyword: str = Field(default="", max_length=100)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=20, ge=1, le=50)


class InlineEditorRequest(BaseModel):
    row_keys: list[str] = Field(min_length=1, max_length=50)


def _iso_utc(value) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat() + "Z"
    return str(value)


def _balanced_assignment_plan(
    row_keys: list[str],
    inspectors: list[str],
    *,
    total_count: int | None = None,
    start_index: int = 0,
) -> tuple[dict[str, str], dict[str, int]]:
    """把已筛出的任务按连续区段尽量平均分给在岗组员。

    连续区段比轮流交错更适合任务列表的地址排序：相邻地址尽量仍由同一
    名组员处理，同时每人的任务数最多相差一条。真正的候选人和任务资格
    仍由批量接口在数据库事务前后重新校验。
    """
    keys = list(dict.fromkeys(str(key).strip() for key in row_keys if str(key).strip()))
    members = list(dict.fromkeys(str(name).strip() for name in inspectors if str(name).strip()))
    if not keys or not members:
        return {}, {name: 0 for name in members}
    total = total_count if total_count is not None else len(keys)
    if total < len(keys) or start_index < 0 or start_index + len(keys) > total:
        return {}, {name: 0 for name in members}
    base, remainder = divmod(total, len(members))
    member_ranges: list[tuple[int, int, str]] = []
    range_start = 0
    for index, member in enumerate(members):
        size = base + (1 if index < remainder else 0)
        member_ranges.append((range_start, range_start + size, member))
        range_start += size
    plan: dict[str, str] = {}
    counts = {name: 0 for name in members}
    for local_index, key in enumerate(keys):
        global_index = start_index + local_index
        member = next(
            name
            for lower, upper, name in member_ranges
            if lower <= global_index < upper
        )
        plan[key] = member
        counts[member] += 1
    return plan, counts


def _bulk_assignment_result(
    *,
    updated: int,
    skipped: list[dict[str, str]],
    failures: list[dict[str, str]],
    inspector: str,
    mode: AssignmentMode,
    assignment_counts: dict[str, int],
    duration_ms: int = 0,
) -> dict:
    """Build mutually exclusive assignment outcome counts and details."""
    return {
        "updated": updated,
        "skipped": len(skipped),
        "failed": len(failures),
        "details": skipped,
        "failed_details": failures,
        "inspector": inspector if mode == "single" else "",
        "mode": mode,
        "assignment_counts": assignment_counts,
        "duration_ms": duration_ms,
    }


def require_flow_user(user: dict) -> tuple[str, str]:
    member = user.get("member") or {}
    position = str(member.get("position") or "").strip()
    name = str(member.get("name") or "").strip()
    communities = [
        str(value).strip()
        for value in user.get("community_names") or []
        if str(value).strip()
    ]
    if position not in {"组员", "组长"}:
        raise HTTPException(403, "手机任务工作台目前仅向组员和组长开放")
    if not name:
        raise HTTPException(403, "当前账号尚未关联有效人员")
    if len(communities) != 1:
        raise HTTPException(403, "当前人员必须配置一个有效社区部门")
    return name, communities[0]


def is_flow_task_admin(user: dict) -> bool:
    group_codes = {
        str(group.get("code") or "")
        for group in user.get("permission_groups") or []
        if isinstance(group, dict)
    }
    legacy_group = str((user.get("permission_group") or {}).get("code") or "")
    return (
        str(user.get("role") or "") in {"admin", "super_admin"}
        or bool(group_codes & {"admin", "super_admin"})
        or legacy_group in {"admin", "super_admin"}
    )


def is_flow_task_elevated(user: dict) -> bool:
    position = str((user.get("member") or {}).get("position") or "").strip()
    return (
        is_flow_task_admin(user)
        or has_permission(user, ONLINE_TASK_MANAGE)
        or position in FLOW_TASK_ELEVATED_POSITIONS
    )


REGISTRATION_CONFIRM_POSITIONS = {"基础管控", "中队长", "所队领导"}


def can_confirm_registration(user: dict) -> bool:
    position = str((user.get("member") or {}).get("position") or "").strip()
    return is_flow_task_admin(user) or position in REGISTRATION_CONFIRM_POSITIONS


def _registration_update_hooks(
    parser_type: str,
    data: TaskBatchUpdate,
    user: dict,
):
    """Build transaction hooks for an atomic result/property save."""
    if not is_registration_task(parser_type):
        if data.registration_property_id or data.registration_property_version:
            raise HTTPException(400, "该业务不支持拟登记房屋关联")
        return None, None, False
    if bool(data.registration_property_id) != bool(data.registration_property_version):
        raise HTTPException(422, "房屋编号和房屋版本必须同时提交")
    if data.registration_pending_address is not None and data.registration_property_id:
        raise HTTPException(422, "已有房屋和待建档地址不能同时提交")

    workflow = TASK_WORKFLOWS[parser_type]
    prepared: dict = {}

    async def transaction_prepare(*, cur, source, current_values, changes):
        current_result = str(current_values.get(workflow.result_field) or "").strip()
        after = dict(current_values)
        after.update(changes)
        result = str(after.get(workflow.result_field) or "").strip()
        submitted_result = (
            str(changes.get(workflow.result_field) or "").strip()
            if workflow.result_field in changes
            else None
        )
        result_changed = submitted_result is not None and submitted_result != current_result
        if submitted_result == "已登记" and current_result != "已登记":
            raise HTTPException(
                403,
                "不能直接选择已登记；请等待居住证自动比对或由有权人员复核确认",
            )
        links = await registration_links_by_rows(
            cur,
            parser_type,
            [str(source["row_key"])],
        )
        existing = links.get(str(source["row_key"]))
        prepared["existing"] = existing
        if result != "待登记":
            if data.registration_property_id:
                raise HTTPException(400, "只有待登记结果可以关联拟登记房屋")
            prepared["action"] = (
                "cancel"
                if current_result == "待登记" and result_changed
                else "preserve"
            )
            return {}

        if (
            existing
            and existing.get("status") == "legacy_completed"
            and current_result == "待登记"
            and not result_changed
            and not data.registration_property_id
        ):
            # Historical rows keep their completed compatibility state when
            # another field is edited.  They are not forced into the new
            # property-selection contract retroactively.
            prepared["action"] = "preserve"
            return {}

        prepared["action"] = "pending"

        if data.registration_property_id:
            try:
                property_row = await validate_registration_property(
                    cur,
                    property_id=int(data.registration_property_id),
                    expected_version=int(data.registration_property_version or 0),
                    task_community=get_parser(parser_type).community_value(after),
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            prepared["property"] = property_row
            return {"现住址": property_row["address"]}

        if data.registration_pending_address is not None:
            pending_address = data.registration_pending_address.strip()
            if not pending_address or any(ord(ch) < 32 for ch in pending_address):
                raise HTTPException(422, "待建档地址不能为空或包含控制字符")
            prepared["pending_address"] = pending_address
            return {"现住址": pending_address}

        existing_usable = bool(
            existing
            and existing.get("property_id")
            and int(existing.get("source_id") or 0) == int(source["id"])
            and existing.get("status") not in {"cancelled", "legacy_completed"}
        )
        if not existing_usable:
            raise HTTPException(422, "选择待登记时必须同时选择唯一的拟登记房屋")
        return {}

    async def transaction_callback(
        *, cur, source, before, after, row_key_before, row_key_after, revision,
    ):
        del before
        action = prepared.get("action")
        if action == "preserve":
            return
        if action == "pending":
            await cur.execute(
                "SELECT COALESCE(identity_hmac,''),community "
                "FROM _online_source_projection WHERE parser_type=%s AND row_key=%s",
                (parser_type, row_key_after),
            )
            projection_context = await cur.fetchone()
            if not projection_context:
                raise HTTPException(409, "任务投影已变化，请刷新后重试")
            if row_key_before != row_key_after and not prepared.get("property"):
                try:
                    await migrate_registration_link(
                        cur,
                        parser_type=parser_type,
                        row_key_before=row_key_before,
                        row_key_after=row_key_after,
                        source_id=int(source["id"]),
                    )
                except ValueError as exc:
                    raise HTTPException(409, str(exc)) from exc
            property_row = prepared.get("property")
            if property_row:
                if row_key_before != row_key_after:
                    existing = prepared.get("existing")
                    if existing:
                        try:
                            await migrate_registration_link(
                                cur,
                                parser_type=parser_type,
                                row_key_before=row_key_before,
                                row_key_after=row_key_after,
                                source_id=int(source["id"]),
                            )
                        except ValueError as exc:
                            raise HTTPException(409, str(exc)) from exc
                await select_registration_property(
                    cur,
                    parser_type=parser_type,
                    row_key=row_key_after,
                    source_id=int(source["id"]),
                    property_id=int(property_row["id"]),
                    property_version=int(property_row["version"]),
                    source_revision=int(revision),
                    source_row_hash=local_row_hash(after),
                    identity_hmac=str(projection_context[0] or ""),
                    task_community=str(projection_context[1] or ""),
                    user_id=int(user.get("id")) if user.get("id") else None,
                )
            elif prepared.get("pending_address"):
                await save_pending_registration_address(
                    cur, parser_type=parser_type, row_key=row_key_after,
                    source_id=int(source["id"]), source_revision=int(revision),
                    source_row_hash=local_row_hash(after),
                    task_community=str(projection_context[1] or ""),
                    user_id=int(user.get("id")) if user.get("id") else None,
                )
            else:
                # A normal edit on an already-linked pending task advances the
                # local source revision and content hash in the same transaction.
                # Carry the link across that exact change so the scanner cannot
                # mistake the platform's own edit for an external source change.
                await refresh_registration_source_context_after_writeback(
                    cur,
                    parser_type=parser_type,
                    source_id=int(source["id"]),
                    previous_revision=int(source["revision"]),
                    previous_row_hash=str(source["row_hash"] or ""),
                    current_revision=int(revision),
                    current_row_hash=local_row_hash(after),
                )
        elif action == "cancel":
            await cancel_registration_link(
                cur,
                parser_type=parser_type,
                row_key=row_key_before,
                source_id=int(source["id"]),
                user_id=int(user.get("id")) if user.get("id") else None,
            )

    return transaction_prepare, transaction_callback, True


def _require_task_edit_user(user: dict) -> dict:
    if not (
        has_permission(user, ONLINE_RAW_EDIT)
        or has_permission(user, ONLINE_TASK_MANAGE)
    ):
        raise HTTPException(403, "当前权限组不能修改流口任务")
    if has_permission(user, ONLINE_RAW_EDIT):
        return user
    # online.task.manage 只在流口任务路由内借用现有逐行回写校验。
    # 不修改登录态中的权限，因此不会开放 Univer 或腾讯原始行管理。
    scoped_user = dict(user)
    scoped_user["permissions"] = list(dict.fromkeys([
        *(user.get("permissions") or []),
        ONLINE_RAW_EDIT,
    ]))
    return scoped_user


def _task_capability_user(user: dict) -> dict:
    if has_permission(user, ONLINE_TASK_MANAGE) and not has_permission(
        user, ONLINE_RAW_EDIT
    ):
        return _require_task_edit_user(user)
    return user


def _require_analysis_user(user: dict) -> dict:
    if not has_permission(user, ONLINE_TASK_MANAGE):
        raise HTTPException(403, "当前权限组不能处理网格核查研判")
    return _require_task_edit_user(user)


def _require_unverifiable_reviewer(user: dict) -> dict:
    position = str((user.get("member") or {}).get("position") or "").strip()
    if not (is_flow_task_admin(user) or position in {"基础管控", "中队长", "所队领导"}):
        raise HTTPException(403, "只有基础管控及以上岗位可以处理两级研判")
    return _require_task_edit_user(user)


def _can_assign_tasks(context: dict) -> bool:
    return bool(context.get("admin_mode")) or context.get("position") == "组长"


def _json_field(field: str) -> str:
    safe = field.replace('"', '\\"')
    return (
        "TRIM(COALESCE(JSON_UNQUOTE(JSON_EXTRACT(" 
        f"projection.values_json, '$.\"{safe}\"')), ''))"
    )


def _active_source_count_sql(parser_type: str, alias: str = "projection") -> str:
    return (
        "(SELECT COUNT(*) FROM _online_source_rows AS active_source "
        f"WHERE active_source.parser_type={alias}.parser_type "
        f"AND active_source.row_key={alias}.row_key "
        "AND active_source.archived_at IS NULL "
        f"{active_source_sql_filter(parser_type, 'active_source')})"
    )


def _source_exception_condition(parser_type: str) -> str:
    if local_data_source_enabled():
        return f"({_active_source_count_sql(parser_type)} > 1)"
    return "(projection.conflict=1 OR projection.source_count>1)"


def _review_condition(parser_type: str) -> str:
    workflow = TASK_WORKFLOWS[parser_type]
    conditions = [_source_exception_condition(parser_type)]
    if not workflow.valid_results:
        conditions.append(
            f"({_json_field(workflow.result_field)} LIKE '%%无法核实%%')"
        )
    if is_registration_task(parser_type):
        conditions.append(_registration_review_condition(parser_type))
    return "(" + " OR ".join(conditions) + ")"


def _registration_review_condition(parser_type: str) -> str:
    if not is_registration_task(parser_type):
        return "1=0"
    return (
        "EXISTS (SELECT 1 FROM _task_registration_links AS registration_link "
        "WHERE registration_link.parser_type=projection.parser_type "
        "AND registration_link.row_key=projection.row_key "
        "AND registration_link.status='review_required')"
    )


def _review_stage_condition(parser_type: str, stage: ReviewStage) -> str:
    workflow = TASK_WORKFLOWS[parser_type]
    if stage == "all":
        return "1=1"
    if supports_unverifiable_review(parser_type):
        structured = {
            "waiting_analysis": (INITIAL_PENDING,),
            "analyzed": (INITIAL_EXTENSION, DEEP_PENDING, DEEP_EXTENSION),
            "initial_pending": (INITIAL_PENDING,),
            "initial_extension": (INITIAL_EXTENSION,),
            "deep_pending": (DEEP_PENDING,),
            "deep_extension": (DEEP_EXTENSION,),
        }.get(stage)
        if structured:
            state_sql = ",".join("'" + value.replace("'", "''") + "'" for value in structured)
            # 保留旧的文本判定作为无流程历史数据的兼容回退。
            analysis = " OR ".join(
                f"{_json_field(field)}<>''" for field in workflow.analysis_fields
            ) or "0"
            unable = f"{_json_field(workflow.result_field)} LIKE '%%无法核实%%'"
            legacy = (
                f"({unable} AND NOT ({analysis}))"
                if stage in {"waiting_analysis", "initial_pending"}
                else f"({unable} AND ({analysis}))"
            )
            return (
                f"(EXISTS (SELECT 1 FROM _unverifiable_review_flows flow "
                f"WHERE flow.parser_type=projection.parser_type "
                f"AND flow.row_key=projection.row_key AND flow.state IN ({state_sql})) "
                f"OR ({legacy} AND NOT EXISTS (SELECT 1 FROM _unverifiable_review_flows flow2 "
                "WHERE flow2.parser_type=projection.parser_type AND flow2.row_key=projection.row_key)))"
            )
    if workflow.valid_results:
        return "1=0"
    analysis = " OR ".join(
        f"{_json_field(field)}<>''" for field in workflow.analysis_fields
    ) or "0"
    unable = f"{_json_field(workflow.result_field)} LIKE '%%无法核实%%'"
    return (
        f"({unable} AND ({analysis}))"
        if stage == "analyzed"
        else f"({unable} AND NOT ({analysis}))"
    )


def _priority_case(parser_type: str) -> str:
    workflow = TASK_WORKFLOWS[parser_type]
    source_exception = _source_exception_condition(parser_type)
    if workflow.valid_results:
        analyzed = "0"
        waiting = "0"
    else:
        analysis = " OR ".join(
            f"{_json_field(field)}<>''" for field in workflow.analysis_fields
        ) or "0"
        unable = f"{_json_field(workflow.result_field)} LIKE '%%无法核实%%'"
        analyzed = f"({unable} AND ({analysis}))"
        waiting = f"({unable} AND NOT ({analysis}))"
    return (
        "CASE "
        "WHEN projection.task_state='completed' THEN 'completed' "
        f"WHEN {analyzed} THEN 'analyzed' "
        f"WHEN {source_exception} THEN 'source_exception' "
        "WHEN projection.pending_state IN ('pending','retry','conflict') THEN 'pending_sync' "
        f"WHEN {waiting} THEN 'waiting_analysis' "
        "ELSE 'ordinary' END"
    )


def _priority_order(parser_type: str) -> str:
    bucket = _priority_case(parser_type)
    return (
        f"CASE {bucket} "
        "WHEN 'analyzed' THEN 0 "
        "WHEN 'source_exception' THEN 1 "
        "WHEN 'pending_sync' THEN 2 "
        "WHEN 'ordinary' THEN 3 "
        "WHEN 'waiting_analysis' THEN 4 "
        "ELSE 5 END"
    )


def _field_order(fields: tuple[str, ...]) -> str:
    candidates = ", ".join(f"NULLIF({_json_field(field)}, '')" for field in fields)
    raw_value = f"COALESCE({candidates}, '')" if candidates else "''"
    normalized = f"LOWER(REGEXP_REPLACE({raw_value}, '[[:space:]]+', ''))"
    return f"CASE WHEN {normalized}='' THEN 1 ELSE 0 END, {normalized}"


def _address_order(parser_type: str) -> str:
    return _field_order(TASK_WORKFLOWS[parser_type].address_fields)


def _identity_order(parser_type: str) -> str:
    return _field_order(TASK_WORKFLOWS[parser_type].identity_fields)


def _original_address_fields(parser_type: str) -> tuple[str, ...]:
    fields = tuple(
        field
        for field in TASK_WORKFLOWS[parser_type].address_fields
        if field != "现住址"
    )
    return fields or TASK_WORKFLOWS[parser_type].address_fields


def _original_address_order(parser_type: str) -> str:
    return _field_order(_original_address_fields(parser_type))


def _analysis_field_order(parser_types: list[str], field_name: str) -> str:
    field_cases: list[str] = []
    for parser_type in parser_types:
        workflow = TASK_WORKFLOWS[parser_type]
        fields = (
            _original_address_fields(parser_type)
            if field_name == "original_address_fields"
            else getattr(workflow, field_name)
        )
        candidates = ", ".join(
            f"NULLIF({_json_field(field)}, '')" for field in fields
        )
        value = f"COALESCE({candidates}, '')" if candidates else "''"
        safe_parser_type = parser_type.replace("'", "''")
        field_cases.append(
            f"WHEN projection.parser_type='{safe_parser_type}' THEN {value}"
        )
    raw_value = "CASE " + " ".join(field_cases) + " ELSE '' END"
    normalized = f"LOWER(REGEXP_REPLACE(({raw_value}), '[[:space:]]+', ''))"
    return f"CASE WHEN {normalized}='' THEN 1 ELSE 0 END, {normalized}"


def _assignment_candidate(
    parser_type: str,
    row_key: str,
    community: str,
    values: dict,
    small_community_name: str = "",
    match_status: str = "unmatched",
) -> dict[str, str]:
    summary = TASK_WORKFLOWS[parser_type].summary(values)
    return {
        "row_key": row_key,
        "community": community,
        "small_community": small_community_name,
        "match_status": match_status,
        "source": summary["source"] or TASK_WORKFLOWS[parser_type].label,
        "address": summary["address"] or "未填写地址",
    }


def _qmf_feedback_state_sql() -> str:
    raw_result = _json_field("核查结果")
    current_result = (
        f"CASE {raw_result} "
        "WHEN '离吴' THEN '离开不返吴' "
        "WHEN '近期反吴' THEN '近期返吴' "
        f"ELSE {raw_result} END"
    )
    return f"""
        COALESCE((
            SELECT CASE
                WHEN snapshot.error_code<>'' THEN 'error'
                WHEN snapshot.platform_result<>{current_result}
                  OR snapshot.last_scanned_at<DATE_SUB(UTC_TIMESTAMP(), INTERVAL 7 DAY)
                  OR source.id IS NULL
                  OR source.revision<>snapshot.source_revision
                  OR source.row_hash<>snapshot.source_row_hash
                THEN 'stale'
                WHEN snapshot.feedback_state='pending' THEN 'pending'
                WHEN snapshot.feedback_state='completed_match' THEN 'completed_match'
                WHEN snapshot.feedback_state='completed_mismatch' THEN 'completed_mismatch'
                WHEN snapshot.feedback_state='not_found' THEN 'not_found'
                WHEN snapshot.feedback_state='non_jurisdiction' THEN 'non_jurisdiction'
                ELSE 'error'
            END
            FROM _qmf_status_snapshots AS snapshot
            LEFT JOIN _online_source_rows AS source
              ON source.id=snapshot.source_id
             AND source.parser_type=snapshot.parser_type
             AND source.row_key=snapshot.row_key
            WHERE snapshot.parser_type=projection.parser_type
              AND snapshot.row_key=projection.row_key
            LIMIT 1
        ), 'not_scanned')
    """.strip()


async def _qmf_status_by_rows(
    cur,
    parser_type: str,
    rows: list[tuple],
) -> dict[str, dict]:
    if parser_type != MODEL_THREE_PARSER or not rows:
        return {}
    row_values = {
        str(row[0]): json_value(row[1], {})
        for row in rows
    }
    keys = list(row_values)
    placeholders = ",".join(["%s"] * len(keys))
    await cur.execute(
        f"""
        SELECT snapshot.row_key,snapshot.source_id,snapshot.source_revision,
               snapshot.source_row_hash,snapshot.platform_result,
               snapshot.feedback_state,snapshot.feedback_result,
               snapshot.checked_at,snapshot.origin,snapshot.error_code,
               snapshot.last_scanned_at,
               source.id,source.revision,source.row_hash
        FROM _qmf_status_snapshots AS snapshot
        LEFT JOIN _online_source_rows AS source
          ON source.id=snapshot.source_id
         AND source.parser_type=snapshot.parser_type
         AND source.row_key=snapshot.row_key
        WHERE snapshot.parser_type=%s
          AND snapshot.row_key IN ({placeholders})
        """,
        (parser_type, *keys),
    )
    result: dict[str, dict] = {}
    now = datetime.utcnow()
    for row in await cur.fetchall():
        row_key = str(row[0])
        platform_result = normalize_qmf_status_result(
            row_values.get(row_key, {}).get("核查结果")
        )
        stale = (
            str(row[4] or "") != platform_result
            or row[11] is None
            or int(row[12] or 0) != int(row[2] or 0)
            or str(row[13] or "") != str(row[3] or "")
            or not row[10]
            or (now - row[10]).total_seconds() > 7 * 24 * 60 * 60
        )
        raw_state = str(row[5] or "")
        state = (
            "error" if str(row[9] or "")
            else "stale" if stale
            else raw_state if raw_state in {
                "pending", "completed_match", "completed_mismatch", "not_found"
            }
            else "error"
        )
        result[row_key] = {
            "state": state,
            "platform_result": str(row[4] or ""),
            "feedback_result": str(row[6] or ""),
            "checked_at": str(row[7] or ""),
            "origin": str(row[8] or ""),
            "error_code": str(row[9] or ""),
            "last_scanned_at": _iso_utc(row[10]),
        }
    for row_key, values in row_values.items():
        result.setdefault(row_key, {
            "state": "not_scanned",
            "platform_result": normalize_qmf_status_result(values.get("核查结果")),
            "feedback_result": "",
            "checked_at": "",
            "origin": "",
            "error_code": "",
            "last_scanned_at": None,
        })
    return result


def _priority_bucket(
    parser_type: str,
    values: dict,
    source_count: int,
    conflict: bool,
    pending: bool,
    task_state_value: str,
) -> str:
    workflow = TASK_WORKFLOWS[parser_type]
    if task_state_value == "completed" or workflow.state(values) == "completed":
        return "completed"
    if workflow.review_stage(values) == "analyzed":
        return "analyzed"
    if conflict or source_count > 1:
        return "source_exception"
    if pending:
        return "pending_sync"
    if workflow.review_stage(values) == "waiting_analysis":
        return "waiting_analysis"
    return "ordinary"


def _multi_filter_condition(
    column: str,
    values: list[str],
) -> tuple[str, list[str]]:
    normalized = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    if not normalized:
        return "1=1", []
    include_empty = EMPTY_FILTER_VALUE in normalized
    non_empty = [value for value in normalized if value != EMPTY_FILTER_VALUE]
    predicates: list[str] = []
    params: list[str] = []
    if non_empty:
        placeholders = ", ".join(["%s"] * len(non_empty))
        field_sql = column if column.startswith("TRIM(") else f"projection.{column}"
        predicates.append(f"{field_sql} IN ({placeholders})")
        params.extend(non_empty)
    if include_empty:
        field_sql = column if column.startswith("TRIM(") else f"projection.{column}"
        predicates.append(f"TRIM(COALESCE({field_sql}, ''))='' ")
    return "(" + " OR ".join(predicates) + ")", params


def _task_where(
    context: dict,
    parser_type: str,
    data: TaskSearch,
    *,
    include_priority: bool = True,
) -> tuple[str, list]:
    scope_where, scope_params = _scope_where(context, data.scope)
    where_parts = ["projection.parser_type=%s", scope_where]
    params: list = [parser_type, *scope_params]
    if local_data_source_enabled():
        where_parts.append(
            "EXISTS (SELECT 1 FROM _online_source_rows AS active_source "
            "WHERE active_source.parser_type=projection.parser_type "
            "AND active_source.row_key=projection.row_key "
            "AND active_source.archived_at IS NULL "
            f"{active_source_sql_filter(parser_type, 'active_source')})"
        )
    review_condition = _review_condition(parser_type)
    if data.status == "pending":
        where_parts.append("projection.task_state<>'completed'")
    elif data.status == "unchecked":
        where_parts.append("projection.task_state='unchecked'")
    elif data.status == "checked":
        where_parts.append("projection.task_state='checked'")
    elif data.status == "completed":
        where_parts.append("projection.task_state='completed'")
    elif data.status == "review":
        where_parts.append(review_condition)
    elif data.status == "registration_review":
        where_parts.append(_registration_review_condition(parser_type))
    if data.review_stage != "all":
        where_parts.append(_review_stage_condition(parser_type, data.review_stage))
    community_condition, community_params = _multi_filter_condition(
        "community", data.communities
    )
    small_community_condition, small_community_params = _multi_filter_condition(
        "small_community_name", data.small_communities
    )
    match_status_condition, match_status_params = _multi_filter_condition(
        "address_match_status", data.match_status
    )
    result_condition, result_params = _multi_filter_condition(
        _json_field(TASK_WORKFLOWS[parser_type].result_field), data.results
    )
    inspector_condition, inspector_params = _multi_filter_condition(
        "inspector", data.inspectors
    )
    where_parts.extend([
        community_condition, small_community_condition,
        match_status_condition, inspector_condition, result_condition,
    ])
    params.extend(community_params)
    params.extend(small_community_params)
    params.extend(match_status_params)
    params.extend(inspector_params)
    params.extend(result_params)
    keyword = data.keyword.strip()
    if keyword:
        where_parts.append("projection.search_text LIKE %s")
        params.append(f"%{keyword}%")
    if data.watch_categories:
        if not settings.REGISTRY_FEATURE_ENABLED:
            where_parts.append("1=0")
        else:
            placeholders = ",".join(["%s"] * len(data.watch_categories))
            registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
            where_parts.append(
                f"EXISTS (SELECT 1 FROM `{registry}`.online_task_watch_snapshots watch_snapshot "
                f"JOIN `{registry}`.watch_assignments watch_assignment "
                "ON watch_assignment.id=watch_snapshot.assignment_id "
                "WHERE watch_snapshot.parser_type=projection.parser_type "
                "AND watch_snapshot.row_key=projection.row_key "
                f"AND watch_assignment.category_id IN ({placeholders}))"
            )
            params.extend(data.watch_categories)
    if data.qmf_feedback_states:
        if parser_type != MODEL_THREE_PARSER:
            where_parts.append("1=0")
        else:
            placeholders = ",".join(["%s"] * len(data.qmf_feedback_states))
            where_parts.append(
                f"({_qmf_feedback_state_sql()}) IN ({placeholders})"
            )
            params.extend(data.qmf_feedback_states)
    if include_priority and data.priority != "all":
        where_parts.append(f"({_priority_case(parser_type)})=%s")
        params.append(data.priority)
    return " AND ".join(where_parts), params


def _analysis_stage_condition(
    parser_types: list[str],
    stage: ReviewStage,
) -> tuple[str, list[str]]:
    conditions: list[str] = []
    params: list[str] = []
    stages = (stage,) if stage != "all" else (
        "initial_pending", "initial_extension", "deep_pending", "deep_extension"
    )
    for parser_type in parser_types:
        stage_conditions = [
            _review_stage_condition(parser_type, current_stage)
            for current_stage in stages
        ]
        conditions.append(
            "(projection.parser_type=%s AND ("
            + " OR ".join(stage_conditions)
            + "))"
        )
        params.append(parser_type)
    return "(" + " OR ".join(conditions) + ")", params


def _analysis_task_where(
    context: dict,
    data: AnalysisTaskSearch,
    *,
    review_stage: ReviewStage | None = None,
) -> tuple[str, list]:
    parser_types = list(dict.fromkeys(data.parser_types))
    type_placeholders = ",".join(["%s"] * len(parser_types))
    scope_where, scope_params = _scope_where(context, "all")
    where_parts = [
        f"projection.parser_type IN ({type_placeholders})",
        scope_where,
    ]
    params: list = [*parser_types, *scope_params]
    stage_sql, stage_params = _analysis_stage_condition(
        parser_types,
        review_stage if review_stage is not None else data.review_stage,
    )
    where_parts.append(stage_sql)
    params.extend(stage_params)
    community_condition, community_params = _multi_filter_condition(
        "community", data.communities
    )
    small_community_condition, small_community_params = _multi_filter_condition(
        "small_community_name", data.small_communities
    )
    match_status_condition, match_status_params = _multi_filter_condition(
        "address_match_status", data.match_status
    )
    inspector_condition, inspector_params = _multi_filter_condition(
        "inspector", data.inspectors
    )
    where_parts.extend([
        community_condition, small_community_condition,
        match_status_condition, inspector_condition,
    ])
    params.extend(community_params)
    params.extend(small_community_params)
    params.extend(match_status_params)
    params.extend(inspector_params)
    if data.keyword.strip():
        where_parts.append("projection.search_text LIKE %s")
        params.append(f"%{data.keyword.strip()}%")
    if data.watch_categories:
        if not settings.REGISTRY_FEATURE_ENABLED:
            where_parts.append("1=0")
        else:
            placeholders = ",".join(["%s"] * len(data.watch_categories))
            registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
            where_parts.append(
                f"EXISTS (SELECT 1 FROM `{registry}`.online_task_watch_snapshots watch_snapshot "
                f"JOIN `{registry}`.watch_assignments watch_assignment "
                "ON watch_assignment.id=watch_snapshot.assignment_id "
                "WHERE watch_snapshot.parser_type=projection.parser_type "
                "AND watch_snapshot.row_key=projection.row_key "
                f"AND watch_assignment.category_id IN ({placeholders}))"
            )
            params.extend(data.watch_categories)
    return " AND ".join(where_parts), params


def _analysis_order(data: AnalysisTaskSearch) -> str:
    parser_types = list(dict.fromkeys(data.parser_types))
    stage_predicates: dict[str, list[str]] = {
        "initial_pending": [],
        "initial_extension": [],
        "deep_pending": [],
        "deep_extension": [],
    }
    for parser_type in parser_types:
        for stage in stage_predicates:
            stage_predicates[stage].append(
                f"(projection.parser_type='{parser_type}' AND "
                f"{_review_stage_condition(parser_type, stage)})"
            )
    stage_order = "CASE "
    for index, stage in enumerate(stage_predicates):
        predicates = stage_predicates[stage]
        if predicates:
            stage_order += f"WHEN {' OR '.join(predicates)} THEN {index} "
    stage_order += "ELSE 4 END"
    if data.sort == "address_asc":
        return (
            f"{stage_order}, "
            f"{_analysis_field_order(parser_types, 'original_address_fields')}, "
            "projection.row_key"
        )
    if data.sort == "identity_asc":
        return (
            f"{stage_order}, "
            f"{_analysis_field_order(parser_types, 'identity_fields')}, "
            f"{_analysis_field_order(parser_types, 'original_address_fields')}, "
            "projection.row_key"
        )
    if data.sort == "updated_asc":
        return f"{stage_order}, projection.updated_at ASC, projection.row_key"
    if data.sort == "updated_desc":
        return f"{stage_order}, projection.updated_at DESC, projection.row_key"
    return f"{stage_order}, projection.row_key"


def _task_order(parser_type: str, sort: SortMode) -> str:
    # Task order is a queue invariant.  Editing results, feedback, address,
    # inspector or updated_at must never move a row while users process it.
    # row_key is immutable for a local task and provides deterministic order
    # across refreshes, pagination and exports.
    return "projection.row_key"


async def _analysis_filter_options(
    cur,
    context: dict,
    user: dict,
    data: AnalysisTaskSearch,
) -> dict:
    _require_legacy_read_model_disabled()
    option_data = data.model_copy(update={
        "communities": [],
    })
    base_where, base_params = _analysis_task_where(
        context,
        option_data,
        review_stage=data.review_stage,
    )
    community_condition, community_params = _multi_filter_condition(
        "community", data.communities
    )
    inspector_where = f"{base_where} AND {community_condition}"
    inspector_params = [*base_params, *community_params]
    result = {"communities": [], "inspectors": [], "watch_categories": []}
    for column, key, empty_label, option_where, option_params in (
        ("community", "communities", "社区未填写", base_where, base_params),
        ("inspector", "inspectors", "未分配核查人", inspector_where, inspector_params),
    ):
        await cur.execute(
            f"""
            SELECT projection.{column}, COUNT(*)
            FROM _online_source_projection AS projection
            WHERE {option_where}
            GROUP BY projection.{column}
            ORDER BY CASE WHEN TRIM(COALESCE(projection.{column}, ''))='' THEN 1 ELSE 0 END,
                     projection.{column}
            """,
            option_params,
        )
        for value, count in await cur.fetchall():
            normalized = str(value or "").strip()
            result[key].append({
                "value": normalized or EMPTY_FILTER_VALUE,
                "label": normalized or empty_label,
                "count": int(count or 0),
            })
    if settings.REGISTRY_FEATURE_ENABLED:
        registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
        await cur.execute(
            f"""
            SELECT category.id, category.name, category.color, category.alert_level,
                   COUNT(DISTINCT projection.row_key)
            FROM _online_source_projection projection
            JOIN `{registry}`.online_task_watch_snapshots snapshot
              ON snapshot.parser_type=projection.parser_type
             AND snapshot.row_key=projection.row_key
            JOIN `{registry}`.watch_assignments assignment
              ON assignment.id=snapshot.assignment_id
            JOIN `{registry}`.watch_categories category
              ON category.id=assignment.category_id
            WHERE {base_where}
            GROUP BY category.id, category.name, category.color, category.alert_level
            ORDER BY category.sort_order, category.id
            """,
            base_params,
        )
        result["watch_categories"] = [
            {"value": int(row[0]), "label": str(row[1]), "color": str(row[2]),
             "alert_level": str(row[3]), "count": int(row[4] or 0)}
            for row in await cur.fetchall()
        ]
    assignment_context = await inspector_option_context(
        cur,
        _task_capability_user(user),
        assignment_only=True,
    )
    result["assignment"] = {
        "enabled": False,
        "community_aliases": assignment_context["community_aliases"],
        "inspectors_by_community": assignment_context["inspectors_by_community"],
    }
    return result


async def _list_analysis_tasks_data(
    data: AnalysisTaskSearch,
    user: dict,
    conn,
) -> dict:
    if settings.TASK_READ_MODEL_ENABLED:
        return await _list_analysis_tasks_data_from_read_model(data, user, conn)
    _require_legacy_read_model_disabled()
    parser_types = list(dict.fromkeys(data.parser_types))
    if not parser_types or any(parser_type not in UNVERIFIABLE_REVIEW_TYPES for parser_type in parser_types):
        raise HTTPException(400, "存在尚未接入研判工作台的业务表")
    context = await _flow_context(conn, user)
    where_sql, query_params = _analysis_task_where(context, data)
    base_data = data.model_copy(update={"review_stage": "all"})
    base_where, base_params = _analysis_task_where(
        context,
        base_data,
        review_stage="all",
    )
    async with conn.cursor() as cur:
        ready_values = []
        for parser_type in parser_types:
            ready_values.append(await _task_source_ready(cur, parser_type))
        if not all(ready_values):
            return {
                "data": [], "total": 0, "page": data.page, "page_size": data.page_size,
                "source_ready": False,
                "message": (
                    "部分业务表本地来源尚未建立"
                    if local_data_source_enabled()
                    else "部分业务表来源尚未建立，请等待一次正常同步"
                ),
                "facets": _empty_facets(), "priority_labels": PRIORITY_LABELS,
                "filters": {"parser_types": parser_types, "scope": data.scope,
                    "review_stage": data.review_stage, "communities": data.communities,
                    "inspectors": data.inspectors, "watch_categories": data.watch_categories,
                    "sort": data.sort, "keyword_present": bool(data.keyword.strip())},
            }
        await cur.execute(
            f"SELECT COUNT(*) FROM _online_source_projection AS projection WHERE {where_sql}",
            query_params,
        )
        total = int((await cur.fetchone())[0] or 0)
        facets = _empty_facets()
        await cur.execute(
            f"SELECT COUNT(*) FROM _online_source_projection AS projection WHERE {base_where}",
            base_params,
        )
        facets["total"] = int((await cur.fetchone())[0] or 0)
        for stage, key in (
            ("initial_pending", "initial_pending"),
            ("initial_extension", "initial_extension"),
            ("deep_pending", "deep_pending"),
            ("deep_extension", "deep_extension"),
        ):
            stage_where, stage_params = _analysis_task_where(context, base_data, review_stage=stage)
            await cur.execute(
                f"SELECT COUNT(*) FROM _online_source_projection AS projection WHERE {stage_where}",
                stage_params,
            )
            facets["review_stage_counts"][key] = int((await cur.fetchone())[0] or 0)
        await cur.execute(
            f"""
            SELECT projection.task_state, COUNT(*)
            FROM _online_source_projection AS projection
            WHERE {base_where}
            GROUP BY projection.task_state
            """,
            base_params,
        )
        for state, count in await cur.fetchall():
            if str(state) in facets["status_counts"]:
                facets["status_counts"][str(state)] = int(count or 0)
        await cur.execute(
            f"""
            SELECT projection.parser_type, projection.row_key, projection.values_json,
                   projection.source_count, projection.conflict,
                   projection.pending_state, projection.task_state
            FROM _online_source_projection AS projection
            WHERE {where_sql}
            ORDER BY {_analysis_order(data)}
            LIMIT %s OFFSET %s
            """,
            [*query_params, data.page_size, (data.page - 1) * data.page_size],
        )
        rows = await cur.fetchall()
        review_by_row = await review_flows_by_rows(
            cur, [(str(row[0]), str(row[1])) for row in rows]
        )
        watch_by_parser: dict[str, dict[str, dict]] = {}
        for parser_type in parser_types:
            keys = [str(row[1]) for row in rows if str(row[0]) == parser_type]
            watch_by_parser[parser_type] = (
                await task_watch_payload(cur, parser_type, keys)
                if settings.REGISTRY_FEATURE_ENABLED and keys else {}
            )
        address_matches_by_parser = {}
        for current_type in parser_types:
            keys = [str(row[1]) for row in rows if str(row[0]) == current_type]
            address_matches_by_parser[current_type] = await _address_matches_by_rows(
                cur, current_type, keys
            )
    return {
        "data": [
            _task_record(
                str(row[0]), str(row[1]), json_value(row[2], {}), int(row[3] or 0),
                bool(row[4]), bool(str(row[5] or "")), str(row[6] or ""),
                watch_by_parser.get(str(row[0]), {}).get(str(row[1])),
                str(row[5] or ""),
                review_flow=review_by_row.get((str(row[0]), str(row[1]))),
                address_match=address_matches_by_parser.get(str(row[0]), {}).get(str(row[1])),
            )
            for row in rows
        ],
        "total": total,
        "page": data.page,
        "page_size": data.page_size,
        "source_ready": True,
        "message": "",
        "facets": facets,
        "priority_labels": PRIORITY_LABELS,
        "filters": {"parser_types": parser_types, "scope": data.scope,
            "review_stage": data.review_stage, "communities": data.communities,
            "inspectors": data.inspectors, "watch_categories": data.watch_categories,
            "sort": data.sort, "keyword_present": bool(data.keyword.strip())},
    }


async def _iter_task_read_model_group_pages(
    conn,
    *,
    parser,
    context: dict,
    scope: FlowScope,
    filters: dict[str, list[str]],
    keyword: str,
    state_filters: dict[str, list[str]] | None = None,
    page_size: int = 200,
):
    """Yield complete, authorized task groups in stable bounded pages."""
    if scope == "all" and not context.get("admin_mode"):
        raise HTTPException(403, "组员和组长只能查看本人或本社区任务")
    allowed_communities = context.get("community_values")
    if allowed_communities is not None and not allowed_communities:
        return
    scoped_inspector = str(context.get("name") or "").strip()
    if scope == "mine" and not scoped_inspector:
        return
    run_id = str(settings.TASK_READ_MODEL_RUN_ID or "").strip()
    if not run_id or len(str(settings.TASK_EVENT_ENCRYPTION_KEY or "").encode()) < 32:
        raise HTTPException(503, {"error": "projection_pending"})

    try:
        import redis.asyncio as redis
        from services.task_read_model import (
            query_task_read_model_group_ids,
            read_task_read_model_group_records,
            require_task_read_model_ready,
        )

        client = redis.from_url(
            settings.REDIS_URL, decode_responses=True,
            socket_connect_timeout=3, socket_timeout=5,
        )
        try:
            await client.ping()
            await require_task_read_model_ready(client, settings.APP_ENVIRONMENT, run_id)
            offset = 0
            expected_total = None
            normalized_filters = {key: list(values) for key, values in filters.items() if values}
            normalized_keyword = keyword.strip().casefold()
            while expected_total is None or offset < expected_total:
                page = await query_task_read_model_group_ids(
                    client, settings.APP_ENVIRONMENT, run_id, parser.table_name,
                    normalized_filters, allowed_communities,
                    keyword if len(normalized_keyword) >= 2 else None,
                    offset, page_size, state_filters=state_filters,
                )
                total = page.get("total")
                tokens = page.get("group_tokens")
                if (type(total) is not int or total < 0 or not isinstance(tokens, list)
                        or len(tokens) != min(page_size, max(0, total - offset))):
                    raise ValueError("task read-model group page contract invalid")
                if expected_total is None:
                    expected_total = total
                elif total != expected_total:
                    raise ValueError("task read-model group total changed")
                if not tokens:
                    break
                groups = await read_task_read_model_group_records(
                    client, settings.APP_ENVIRONMENT, run_id, parser.table_name, tokens,
                )
                if not isinstance(groups, list) or len(groups) != len(tokens):
                    raise ValueError("task read-model group page mismatch")

                expected_sources: dict[int, tuple[str, int, str]] = {}
                row_key_counts: dict[str, int] = {}
                accepted = []
                for group in groups:
                    values = group.get("values")
                    sources = group.get("sources")
                    row_key = group.get("row_key")
                    if (not isinstance(values, dict) or not isinstance(sources, list) or not sources
                            or not isinstance(row_key, str) or parser.make_row_key(values) != row_key
                            or values != sources[0].get("values")
                            or type(group.get("source_count")) is not int
                            or group["source_count"] != len(sources)
                            or type(group.get("conflict")) is not bool):
                        raise ValueError("task read-model group identity mismatch")
                    if group["conflict"] != any(
                        stable_json(source.get("values") or {}) != stable_json(values)
                        for source in sources[1:]
                    ):
                        raise ValueError("task read-model group conflict mismatch")
                    ordered = sorted(
                        sources,
                        key=lambda source: (int(source.get("source_id") or 0), str(source.get("task_id") or "")),
                    )
                    if sources != ordered:
                        raise ValueError("task read-model group source order invalid")
                    if scope == "mine" and any(
                        str(source.get("values", {}).get("核查人") or "").strip().casefold()
                        != scoped_inspector.casefold()
                        for source in sources
                    ):
                        continue
                    if normalized_keyword and not any(
                        normalized_keyword in str(value or "").casefold()
                        for value in values.values()
                    ):
                        continue
                    if any(
                        str(values.get(field, "") or "") not in set(allowed)
                        for field, allowed in normalized_filters.items()
                    ):
                        continue
                    accepted.append(group)
                    row_key_counts[row_key] = len(sources)
                    for source in sources:
                        task_id = str(source.get("task_id") or "")
                        table_name, separator, physical_text = task_id.rpartition(":")
                        source_id = source.get("source_id")
                        revision = source.get("revision")
                        if (not separator or table_name != parser.table_name
                                or not physical_text.isdigit()
                                or type(source_id) is not int or source_id < 1
                                or type(revision) is not int or revision < 1):
                            raise ValueError("task read-model group source invalid")
                        expected_sources[source_id] = (task_id, revision, row_key)

                locators = {}
                source_ids = list(expected_sources)
                async with conn.cursor() as cur:
                    for start in range(0, len(source_ids), 400):
                        batch = source_ids[start:start + 400]
                        if not batch:
                            continue
                        marks = ",".join(["%s"] * len(batch))
                        await cur.execute(
                            "SELECT id,physical_row,row_key,revision FROM _online_source_rows AS source "
                            f"WHERE parser_type=%s AND id IN ({marks}) AND archived_at IS NULL "
                            + active_source_sql_filter(parser.parser_type, "source"),
                            [parser.parser_type, *batch],
                        )
                        for source_id, physical_row, row_key, revision in await cur.fetchall():
                            locators[int(source_id)] = (
                                f"{parser.table_name}:{int(physical_row)}", int(revision or 0), str(row_key),
                            )
                    for source_id, expected in expected_sources.items():
                        if locators.get(source_id) != expected:
                            raise ValueError("task source locator mismatch")
                    row_keys = list(row_key_counts)
                    for start in range(0, len(row_keys), 400):
                        batch = row_keys[start:start + 400]
                        if not batch:
                            continue
                        marks = ",".join(["%s"] * len(batch))
                        await cur.execute(
                            "SELECT row_key,COUNT(*) FROM _online_source_rows AS source "
                            f"WHERE parser_type=%s AND row_key IN ({marks}) AND archived_at IS NULL "
                            + active_source_sql_filter(parser.parser_type, "source")
                            + " GROUP BY row_key",
                            [parser.parser_type, *batch],
                        )
                        actual_counts = {str(key): int(count or 0) for key, count in await cur.fetchall()}
                        for group in accepted:
                            row_key = str(group["row_key"])
                            if row_key in batch and actual_counts.get(row_key) != row_key_counts[row_key]:
                                raise ValueError("task source group is incomplete")
                offset += len(tokens)
                if accepted:
                    yield accepted
        finally:
            await client.aclose()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, {"error": "projection_pending"}) from None


async def _indexed_task_state_counts(
    parser,
    context: dict,
    *,
    scope: FlowScope,
    filters: dict[str, list[str]],
    keyword: str,
    dimensions: dict[str, list[str]],
) -> dict[str, dict[str, int]] | None:
    """Count canonical groups from state indexes when scope is index-expressible."""
    if scope == "mine":
        return None
    communities = context.get("community_values")
    if communities is not None and not communities:
        return {field: {value: 0 for value in values} for field, values in dimensions.items()}
    run_id = str(settings.TASK_READ_MODEL_RUN_ID or "").strip()
    if not run_id or len(str(settings.TASK_EVENT_ENCRYPTION_KEY or "").encode()) < 32:
        raise HTTPException(503, {"error": "projection_pending"})
    try:
        import redis.asyncio as redis
        from services.task_read_model import (
            count_task_read_model_groups,
            require_task_read_model_ready,
        )

        client = redis.from_url(
            settings.REDIS_URL, decode_responses=True,
            socket_connect_timeout=3, socket_timeout=5,
        )
        try:
            await client.ping()
            await require_task_read_model_ready(client, settings.APP_ENVIRONMENT, run_id)
            counts: dict[str, dict[str, int]] = {}
            search = keyword if len(keyword.strip()) >= 2 else None
            for field, values in dimensions.items():
                counts[field] = {}
                for value in values:
                    counts[field][value] = await count_task_read_model_groups(
                        client, settings.APP_ENVIRONMENT, run_id, parser.table_name,
                        filters, communities, search,
                        state_filters={field: [value]},
                    )
            return counts
        finally:
            await client.aclose()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, {"error": "projection_pending"}) from None


async def _list_analysis_tasks_data_from_read_model(
    data: AnalysisTaskSearch,
    user: dict,
    conn,
) -> dict:
    parser_types = list(dict.fromkeys(data.parser_types))
    if not parser_types or any(parser_type not in UNVERIFIABLE_REVIEW_TYPES for parser_type in parser_types):
        raise HTTPException(400, "存在尚未接入研判工作台的业务表")
    context = await _flow_context(conn, user)
    parser_types = sorted(parser_types)
    facets = _empty_facets()
    total = 0
    page_offset = (data.page - 1) * data.page_size
    output: list[dict] = []
    for parser_type in parser_types:
        parser = get_parser(parser_type)
        filter_values = _task_index_filters(parser_type, inspectors=data.inspectors)
        state_filters = {
            **({"address_match.small_community_name": data.small_communities}
               if data.small_communities else {}),
            **({"address_match.status": data.match_status}
               if data.match_status else {}),
            **({"watch.category_id": [str(value) for value in data.watch_categories]}
               if data.watch_categories else {}),
        }
        if data.review_stage != "all":
            state_filters["review.state"] = [data.review_stage]
        async for groups in _iter_task_read_model_group_pages(
            conn, parser=parser, context=context, scope=data.scope,
            filters=filter_values, keyword=data.keyword,
        ):
            records = [{
                **group,
                "parser_type": parser_type,
            } for group in groups]
            states_by_task = await _read_task_view_states_for_records(records)
            for record in records:
                values = record["values"]
                row_key = str(record["row_key"])
                domains = states_by_task.get(str(record["task_id"]), {})
                states = {domain: value.get("state", {}) for domain, value in domains.items()}
                review_flow = states.get("review")
                address_match = states.get("address_match") or {}
                candidate = address_match.get("candidate") or {}
                address_match = {
                    **address_match,
                    "small_community_id": candidate.get("entry_id"),
                    "small_community_name": candidate.get("name") or candidate.get("community_name", ""),
                }
                watch_status = states.get("watch") or {}
                workflow = TASK_WORKFLOWS[parser_type]
                state = workflow.state(values)
                item = _task_record(
                    parser_type, row_key, values,
                    int(record["source_count"]), bool(record["conflict"]), False, state,
                    watch_status, review_flow=review_flow,
                    qmf_status=states.get("qmf"),
                    residence_status=states.get("residence"),
                    registration_link=states.get("registration"),
                    address_match=address_match,
                )
                facets["total"] += 1
                if state in facets["status_counts"]:
                    facets["status_counts"][state] += 1
                if item["priority"] in facets["priority_counts"]:
                    facets["priority_counts"][item["priority"]] += 1
                stage = str((review_flow or {}).get("state") or item["review_stage"])
                if stage in facets["review_stage_counts"]:
                    facets["review_stage_counts"][stage] += 1
                registration_state = str((states.get("registration") or {}).get("status") or "")
                if registration_state in {"review_required", "ambiguous", "conflict", "pending_review"}:
                    facets["registration_review_count"] += 1
                qmf_state = str((states.get("qmf") or {}).get("state") or "")
                if qmf_state in facets["qmf_feedback_counts"]:
                    facets["qmf_feedback_counts"][qmf_state] += 1
                if not state_filters:
                    if data.communities and str(item["community"]).strip() not in set(data.communities):
                        continue
                    if data.inspectors and str(item["inspector"]).strip() not in set(data.inspectors):
                        continue
                    if data.review_stage != "all" and stage != data.review_stage:
                        continue
                    if total >= page_offset and len(output) < data.page_size:
                        output.append(item)
                    total += 1

        if data.scope != "mine":
            indexed_review_counts = {key: 0 for key in facets["review_stage_counts"]}
            indexed_qmf_counts = {key: 0 for key in facets["qmf_feedback_counts"]}
            indexed_registration_count = 0
            indexed_counts_available = False
            for parser_type in parser_types:
                parser = get_parser(parser_type)
                counts = await _indexed_task_state_counts(
                    parser, context, scope=data.scope,
                    filters=_task_index_filters(parser_type, inspectors=data.inspectors),
                    keyword=data.keyword,
                    dimensions={
                        "review.state": list(facets["review_stage_counts"]),
                        "qmf.state": list(facets["qmf_feedback_counts"]),
                        "registration.status": ["review_required"],
                    },
                )
                if counts is None:
                    continue
                indexed_counts_available = True
                for key in indexed_review_counts:
                    indexed_review_counts[key] += counts["review.state"][key]
                for key in indexed_qmf_counts:
                    indexed_qmf_counts[key] += counts["qmf.state"][key]
                indexed_registration_count += counts["registration.status"]["review_required"]
            if indexed_counts_available:
                facets["review_stage_counts"] = indexed_review_counts
                facets["qmf_feedback_counts"] = indexed_qmf_counts
                facets["registration_review_count"] = indexed_registration_count

        if state_filters:
            for parser_type in parser_types:
                parser = get_parser(parser_type)
                filter_values = _task_index_filters(parser_type, inspectors=data.inspectors)
                async for groups in _iter_task_read_model_group_pages(
                    conn, parser=parser, context=context, scope=data.scope,
                    filters=filter_values, keyword=data.keyword,
                    state_filters=state_filters,
                ):
                    records = [{**group, "parser_type": parser_type} for group in groups]
                    states_by_task = await _read_task_view_states_for_records(records)
                    for record in records:
                        values = record["values"]
                        row_key = str(record["row_key"])
                        states = {
                            domain: value.get("state", {})
                            for domain, value in states_by_task[str(record["task_id"])].items()
                        }
                        review_flow = states.get("review")
                        address_match = states.get("address_match") or {}
                        candidate = address_match.get("candidate") or {}
                        address_match = {
                            **address_match,
                            "small_community_id": candidate.get("entry_id"),
                            "small_community_name": candidate.get("name") or candidate.get("community_name", ""),
                        }
                        watch_status = states.get("watch") or {}
                        workflow = TASK_WORKFLOWS[parser_type]
                        item = _task_record(
                            parser_type, row_key, values,
                            int(record["source_count"]), bool(record["conflict"]), False,
                            workflow.state(values), watch_status, review_flow=review_flow,
                            qmf_status=states.get("qmf"), residence_status=states.get("residence"),
                            registration_link=states.get("registration"), address_match=address_match,
                        )
                        stage = str((review_flow or {}).get("state") or item["review_stage"])
                        if data.communities and str(item["community"]).strip() not in set(data.communities):
                            continue
                        if data.inspectors and str(item["inspector"]).strip() not in set(data.inspectors):
                            continue
                        if data.small_communities and address_match["small_community_name"] not in set(data.small_communities):
                            continue
                        if data.match_status and str(address_match.get("status") or "unmatched") not in set(data.match_status):
                            continue
                        if data.review_stage != "all" and stage != data.review_stage:
                            continue
                        if data.watch_categories and not {
                            int(mark.get("category_id"))
                            for mark in watch_status.get("watch_marks", [])
                            if isinstance(mark, dict) and str(mark.get("category_id", "")).isdigit()
                        }.intersection(set(data.watch_categories)):
                            continue
                        if total >= page_offset and len(output) < data.page_size:
                            output.append(item)
                        total += 1
    return {
        "data": output,
        "total": total,
        "page": data.page,
        "page_size": data.page_size,
        "source_ready": True,
        "message": "",
        "facets": facets,
        "priority_labels": PRIORITY_LABELS,
        "filters": {
            "parser_types": parser_types,
            "scope": data.scope,
            "review_stage": data.review_stage,
            "communities": data.communities,
            "inspectors": data.inspectors,
            "watch_categories": data.watch_categories,
            "sort": "stable_group_token",
            "keyword_present": bool(data.keyword.strip()),
        },
    }


async def _flow_context(conn, user: dict) -> dict:
    if is_flow_task_elevated(user):
        # online.task.manage 是流口任务工作台内的全所管理能力。
        # 它不会改变用户在 Univer、辖区档案等其他模块的数据范围。
        view_communities = (
            None
            if has_permission(user, ONLINE_TASK_MANAGE)
            else effective_view_communities(user)
        )
        community_values = (
            None
            if view_communities is None
            else await community_names_for_scopes(conn, view_communities)
        )
        member = user.get("member") or {}
        return {
            "name": str(
                member.get("name")
                or user.get("display_name")
                or user.get("username")
                or "管理员"
            ).strip(),
            "position": str(member.get("position") or "管理员").strip(),
            "community": "全所",
            "community_values": community_values,
            "admin_mode": True,
        }
    name, community = require_flow_user(user)
    aliases = await community_names_for_scopes(conn, [community])
    return {
        "name": name,
        "position": str((user.get("member") or {}).get("position") or ""),
        "community": community,
        "community_values": aliases or [community],
        "admin_mode": False,
    }


def _scope_where(
    context: dict,
    scope: FlowScope,
    *,
    alias: str = "projection",
) -> tuple[str, list[str]]:
    if scope == "all" and not context.get("admin_mode"):
        raise HTTPException(403, "组员和组长只能查看本人或本社区任务")
    community_values = context["community_values"]
    if community_values is None:
        clause = "1=1"
        params: list[str] = []
    elif community_values:
        placeholders = ", ".join(["%s"] * len(community_values))
        clause = f"{alias}.community IN ({placeholders})"
        params = list(community_values)
    else:
        clause = "1=0"
        params = []
    if scope == "mine":
        clause += f" AND LOWER(TRIM({alias}.inspector))=LOWER(TRIM(%s))"
        params.append(context["name"])
    return clause, params


async def _source_readiness(cur) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for parser_type in MOBILE_TASK_TYPES:
        result[parser_type] = await _task_source_ready(cur, parser_type)
    return result


async def _aggregate_live(
    cur,
    context: dict,
    scope: FlowScope,
    parser_types: tuple[str, ...] = MOBILE_TASK_TYPES,
) -> dict[str, dict]:
    where, params = _scope_where(context, scope)
    type_placeholders = ", ".join(["%s"] * len(parser_types))
    active_source_exists = "1=1"
    source_exception = "projection.conflict=1 OR projection.source_count>1"
    if local_data_source_enabled():
        active_source_exists = (
            "EXISTS (SELECT 1 FROM _online_source_rows AS active_source "
            "WHERE active_source.parser_type=projection.parser_type "
            "AND active_source.row_key=projection.row_key "
            "AND active_source.archived_at IS NULL "
            f"{active_source_sql_filter('all', 'active_source')})"
        )
        source_exception = (
            "(SELECT COUNT(*) FROM _online_source_rows AS active_source "
            "WHERE active_source.parser_type=projection.parser_type "
            "AND active_source.row_key=projection.row_key "
            "AND active_source.archived_at IS NULL "
            f"{active_source_sql_filter('all', 'active_source')}) > 1"
        )
    await cur.execute(
        f"""
        SELECT projection.parser_type, projection.task_state,
               COUNT(*),
               SUM(CASE WHEN {source_exception}
                              OR EXISTS (
                                  SELECT 1 FROM _task_registration_links registration_link
                                  WHERE registration_link.parser_type=projection.parser_type
                                    AND registration_link.row_key=projection.row_key
                                    AND registration_link.status='review_required'
                              )
                              OR (projection.parser_type IN (
                                      '全链条','出租房屋核查','寄递业','疑似漏登记'
                                  ) AND {_json_field('核查结果')}
                                      LIKE '%%无法核实%%')
                              OR (projection.parser_type='疑似返苏'
                                  AND {_json_field('核查反馈')}
                                      LIKE '%%无法核实%%')
                        THEN 1 ELSE 0 END)
        FROM _online_source_projection AS projection
        WHERE projection.parser_type IN ({type_placeholders})
          AND {where}
          AND {active_source_exists}
        GROUP BY projection.parser_type, projection.task_state
        """,
        (*parser_types, *params),
    )
    totals = {
        parser_type: {
            "parser_type": parser_type,
            "label": TASK_WORKFLOWS[parser_type].label,
            "pending": 0,
            "unchecked": 0,
            "checked": 0,
            "completed": 0,
            "review": 0,
        }
        for parser_type in parser_types
    }
    for parser_type, state, count, review in await cur.fetchall():
        item = totals[str(parser_type)]
        normalized_state = str(state or "unchecked")
        amount = int(count or 0)
        if normalized_state in {"unchecked", "checked", "completed"}:
            item[normalized_state] += amount
        if normalized_state != "completed":
            item["pending"] += amount
        item["review"] += int(review or 0)
    return totals


async def _validate_assignment(
    cur,
    context: dict,
    changes: dict[str, str],
) -> None:
    if "核查人" not in changes:
        return
    if context.get("admin_mode"):
        return
    if context["position"] != "组长":
        raise HTTPException(403, "只有组长可以在手机任务中转派任务")
    assignee = str(changes.get("核查人") or "").strip()
    if not assignee:
        raise HTTPException(400, "请选择要分配的组员")
    await cur.execute(
        """
        SELECT member.id
        FROM _grid_members AS member
        JOIN _grid_member_department_links AS link
          ON link.member_id=member.id
        JOIN _departments AS department
          ON department.id=link.department_id
         AND department.department_type='community'
         AND department.is_active=1
        JOIN _communities AS community
          ON community.id=department.community_id
        WHERE community.name=%s
          AND member.name=%s
          AND member.position IN ('组长', '组员')
          AND member.status='在岗'
        LIMIT 1
        """,
        (context["community"], assignee),
    )
    if not await cur.fetchone():
        raise HTTPException(400, "只能分配给本社区在岗组长或组员")


def _task_record(
    parser_type: str,
    row_key: str,
    values: dict,
    source_count: int,
    conflict: bool,
    pending: bool,
    task_state_value: str,
    watch: dict | None = None,
    sync_state: str = "",
    qmf_status: dict | None = None,
    residence_status: dict | None = None,
    registration_link: dict | None = None,
    review_flow: dict | None = None,
    address_match: dict | None = None,
) -> dict:
    workflow = TASK_WORKFLOWS[parser_type]
    normalized = {key: str(value or "") for key, value in values.items()}
    watch = watch or {}
    structured_stage = str((review_flow or {}).get("state") or "")
    # 正式结果已经完成后，旧的无法核实流程只作为历史记录保留，不能
    # 再把任务标成“流程已暂停”。来源异常仍可通过独立的来源/同步标记
    # 展示，不影响已完成状态的主口径。
    structured_active = (
        structured_stage in UNVERIFIABLE_ACTIVE_STATES
        and task_state_value != "completed"
    )
    return {
        "task_key": f"{parser_type}:{row_key}",
        "row_key": str(row_key),
        "parser_type": parser_type,
        "summary": workflow.summary(normalized),
        "community": str(normalized.get("社区") or normalized.get("下发社区") or ""),
        "inspector": str(normalized.get("核查人") or ""),
        "state": task_state_value or workflow.state(normalized),
        "needs_review": workflow.needs_review(
            normalized,
            source_count=int(source_count or 0),
            conflict=bool(conflict),
        ),
        "review_stage": structured_stage if structured_active else workflow.review_stage(normalized),
        "review_flow": review_flow,
        "source_count": int(source_count or 0),
        "conflict": bool(conflict),
        "pending_sync": bool(pending),
        "sync_state": sync_state or ("pending" if pending else ""),
        "priority": _priority_bucket(
            parser_type,
            normalized,
            int(source_count or 0),
            bool(conflict),
            bool(pending),
            task_state_value,
        ),
        "watch_marks": list(watch.get("watch_marks") or []),
        "first_dispatch_at": _iso_utc(watch.get("first_dispatch_at")),
        "qmf_status": qmf_status,
        "residence_status": residence_status,
        "registration_link": registration_link,
        "address_match": address_match,
    }


async def _address_matches_by_rows(
    cur,
    parser_type: str,
    row_keys: list[str],
) -> dict[str, dict]:
    if not row_keys:
        return {}
    matches = {}
    for offset in range(0, len(row_keys), 400):
        batch = row_keys[offset:offset + 400]
        placeholders = ",".join(["%s"] * len(batch))
        await cur.execute(
            f"""
            SELECT row_key,
                   COALESCE(match.confirmed_entry_id, match.suggested_entry_id),
                   COALESCE(confirmed_entry.name, suggested_entry.name, ''),
                   match.match_status, match.match_score,
                   match.match_method, match.match_reason, match.candidates_json,
                   match.matcher_version
            FROM _online_task_address_matches AS match
            LEFT JOIN _police_address_entries AS confirmed_entry
              ON confirmed_entry.id=match.confirmed_entry_id
            LEFT JOIN _police_address_entries AS suggested_entry
              ON suggested_entry.id=match.suggested_entry_id
            WHERE match.parser_type=%s AND match.row_key IN ({placeholders})
            """,
            (parser_type, *batch),
        )
        matches.update({
            str(row[0]): {
                "small_community_id": int(row[1]) if row[1] is not None else None,
                "small_community_name": str(row[2] or ""),
                "status": str(row[3] or "unmatched"),
                "score": float(row[4] or 0),
                "method": str(row[5] or ""),
                "reason": str(row[6] or ""),
                "candidates": json_value(row[7], []),
                "version": str(row[8] or ""),
            }
            for row in await cur.fetchall()
        })
    for row_key in row_keys:
        matches.setdefault(str(row_key), {
            "small_community_id": None,
            "small_community_name": "",
            "status": "unmatched",
            "score": 0.0,
            "method": "",
            "reason": "",
            "candidates": [],
            "version": "",
        })
    return matches


def _export_value(values: dict, *keys: str) -> str:
    for key in keys:
        value = values.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _mobile_export_row(
    parser_type: str,
    row_key: str,
    values: dict,
    source_id: int | None,
    source_count: int,
    conflict: bool,
    pending: bool,
    task_state: str,
    source_revision: int | None = None,
    source_row_hash: str = "",
    review_flow: dict | None = None,
    include_internal: bool = False,
) -> list[object]:
    workflow = TASK_WORKFLOWS[parser_type]
    stage = str((review_flow or {}).get("state") or workflow.review_stage(values) or "")
    public_values = [
        _export_value(values, "姓名", "核查对象", "对象姓名"),
        _export_value(values, "身份证号", "身份证号码", "公民身份号码"),
        _export_value(
            values,
            "联系号码",
            "手机号",
            "联系电话",
            "手机号码",
            "电话号码",
            "联系方式",
            "电话",
        ),
        _export_value(values, "原地址", "原住址", "地址", "疑似现住址"),
        _export_value(values, "现住址", "核查补充信息", "拟登记住址"),
        _export_value(values, "社区", "下发社区"),
        _export_value(values, "核查人"),
        task_state,
        UNVERIFIABLE_STATE_LABELS.get(stage, stage),
        _export_value(values, *workflow.analysis_fields),
        _export_value(values, *workflow.secondary_fields),
        _export_value(values, workflow.result_field),
        _export_value(values, *workflow.date_fields),
    ]
    if not include_internal:
        return public_values
    public_values = [
        *public_values[:9],
        "",  # 本次研判结果由基础管控填写
        "",  # 本次研判意见由基础管控填写
        public_values[9],  # 历史研判意见
        public_values[10],  # 已有核查反馈
        *public_values[11:],
    ]
    return [
        parser_type,
        row_key,
        source_id or "",
        source_revision or "",
        source_row_hash,
        int((review_flow or {}).get("flow_version") or 0) or "",
        *public_values,
        "",
        "是" if conflict or source_count != 1 else "否",
        "是" if pending else "否",
    ]


async def _mobile_export_workbook(
    *,
    data: TaskSearch | AnalysisTaskSearch,
    parser_type: str | None,
    user: dict,
    conn,
) -> tuple[BytesIO, int]:
    if settings.TASK_READ_MODEL_ENABLED:
        return await _mobile_export_workbook_from_read_model(
            data=data, parser_type=parser_type, user=user, conn=conn,
        )
    _require_legacy_read_model_disabled()
    context = await _flow_context(conn, user)
    active_source_filter = (
        " AND source.spreadsheet_id=0 "
        "AND source.source_kind IN ('local_table','local_dispatch','one_time_continuation_import')"
        if local_data_source_enabled() else ""
    )
    if isinstance(data, AnalysisTaskSearch):
        parser_types = list(dict.fromkeys(data.parser_types))
        if not parser_types or any(value not in TASK_WORKFLOWS for value in parser_types):
            raise HTTPException(400, "存在尚未接入研判工作台的业务表")
        where_sql, query_params = _analysis_task_where(context, data)
        order_sql = _analysis_order(data)
        type_label = "研判任务"
        include_internal_columns = True
    else:
        if not parser_type or parser_type not in TASK_WORKFLOWS:
            raise HTTPException(400, "该业务尚未接入手机任务工作台")
        parser_types = [parser_type]
        where_sql, query_params = _task_where(context, parser_type, data)
        order_sql = _task_order(parser_type, data.sort)
        type_label = parser_type
        include_internal_columns = False
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT projection.parser_type, projection.row_key, projection.values_json,
                   projection.source_count, projection.conflict, projection.pending_state,
                   projection.task_state,
                   (SELECT MIN(source.id) FROM _online_source_rows source
                    WHERE source.parser_type=projection.parser_type
                      AND source.row_key=projection.row_key
                      AND source.archived_at IS NULL{active_source_filter}) AS source_id,
                   (SELECT source.revision FROM _online_source_rows source
                    WHERE source.parser_type=projection.parser_type
                      AND source.row_key=projection.row_key
                      AND source.archived_at IS NULL{active_source_filter}
                    ORDER BY source.id LIMIT 1) AS source_revision,
                   (SELECT source.row_hash FROM _online_source_rows source
                    WHERE source.parser_type=projection.parser_type
                      AND source.row_key=projection.row_key
                      AND source.archived_at IS NULL{active_source_filter}
                    ORDER BY source.id LIMIT 1) AS source_row_hash
            FROM _online_source_projection AS projection
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT 100000
            """,
            query_params,
        )
        rows = await cur.fetchall()
        review_by_row = await review_flows_by_rows(
            cur, [(str(row[0]), str(row[1])) for row in rows]
        )
    export_rows = [
        _mobile_export_row(
            str(row[0]), str(row[1]), json_value(row[2], {}),
            int(row[7]) if row[7] is not None else None,
            int(row[3] or 0), bool(row[4]), bool(row[5]), str(row[6] or ""),
            int(row[8]) if row[8] is not None else None,
            str(row[9] or ""),
            review_by_row.get((str(row[0]), str(row[1]))),
            include_internal=include_internal_columns,
        )
        for row in rows
    ]
    if include_internal_columns:
        headers = [
            "业务类型", "任务标识", "来源ID", "来源版本", "来源行哈希", "流程版本",
            "姓名", "身份证号", "手机号", "原地址", "现住址", "社区", "核查人",
            "任务状态", "研判阶段", "本次研判结果", "研判意见", "历史研判意见", "已有核查反馈", "核查结果", "截止日期",
            "来源异常", "待同步",
        ]
    else:
        headers = [
            "姓名", "身份证号", "手机号", "原地址", "现住址", "社区", "核查人",
            "任务状态", "研判阶段", "研判意见", "复核反馈", "核查结果", "截止日期",
        ]
    workbook = build_xlsx(
        type_label,
        headers,
        export_rows,
        dropdown_column="P" if include_internal_columns else None,
        dropdown_values=("发现新线索", "未发现新线索"),
        hidden_columns=("A", "C", "D", "E", "F") if include_internal_columns else (),
    )
    return workbook, len(export_rows)


async def _mobile_export_workbook_from_read_model(
    *,
    data: TaskSearch | AnalysisTaskSearch,
    parser_type: str | None,
    user: dict,
    conn,
) -> tuple[BytesIO, int]:
    context = await _flow_context(conn, user)
    if isinstance(data, AnalysisTaskSearch):
        parser_types = list(dict.fromkeys(data.parser_types))
        if not parser_types or any(value not in UNVERIFIABLE_REVIEW_TYPES for value in parser_types):
            raise HTTPException(400, "存在尚未接入研判工作台的业务表")
        include_internal_columns = True
        type_label = "研判任务"
        records = []
        for current_type in parser_types:
            records.extend(await _task_read_model_records(
                conn, current_type, context, scope=data.scope, keyword=data.keyword,
                filters=_task_index_filters(current_type, inspectors=data.inspectors),
            ))
    else:
        if not parser_type or parser_type not in TASK_WORKFLOWS:
            raise HTTPException(400, "该业务尚未接入手机任务工作台")
        parser_types = [parser_type]
        include_internal_columns = False
        type_label = parser_type
        records = await _task_read_model_records(
            conn, parser_type, context, scope=data.scope, keyword=data.keyword,
            filters=_task_index_filters(
                parser_type, communities=data.communities,
                inspectors=data.inspectors, results=data.results,
            ),
        )

    states_by_task = await _read_task_view_states_for_records(records)

    export_rows = []
    for record in records:
        current_type = str(record["parser_type"])
        values = record["values"]
        workflow = TASK_WORKFLOWS[current_type]
        domains = states_by_task[str(record["task_id"])]
        review_flow = domains["review"]["state"]
        address_state = domains["address_match"]["state"]
        candidate = address_state.get("candidate") or {}
        address_match = {
            **address_state,
            "small_community_name": candidate.get("name") or candidate.get("community_name", ""),
        }
        watch_state = domains["watch"]["state"]
        qmf_state = domains["qmf"]["state"]
        registration_state = domains["registration"]["state"]
        task_state = workflow.state(values)
        if isinstance(data, AnalysisTaskSearch):
            if data.communities and workflow.first_value(values, ("社区", "下发社区")) not in set(data.communities):
                continue
            if data.inspectors and str(values.get("核查人") or "").strip() not in set(data.inspectors):
                continue
            if data.watch_categories:
                category_ids = {
                    int(mark.get("category_id")) for mark in watch_state.get("watch_marks", [])
                    if isinstance(mark, dict) and str(mark.get("category_id", "")).isdigit()
                }
                if not category_ids.intersection(set(data.watch_categories)):
                    continue
            if data.small_communities and str(address_match.get("small_community_name") or "") not in set(data.small_communities):
                continue
            if data.match_status and str(address_match.get("status") or "unmatched") not in set(data.match_status):
                continue
            stage = str((review_flow or {}).get("state") or workflow.review_stage(values))
            if data.review_stage != "all" and stage != data.review_stage:
                continue
        else:
            if data.communities and workflow.first_value(values, ("社区", "下发社区")) not in set(data.communities):
                continue
            if data.inspectors and str(values.get("核查人") or "").strip() not in set(data.inspectors):
                continue
            if data.small_communities and str(address_match.get("small_community_name") or "") not in set(data.small_communities):
                continue
            if data.match_status and str(address_match.get("status") or "unmatched") not in set(data.match_status):
                continue
            if data.results and str(values.get(workflow.result_field) or "").strip() not in set(data.results):
                continue
            if data.status == "pending" and task_state == "completed":
                continue
            if data.status in {"unchecked", "checked", "completed"} and task_state != data.status:
                continue
            if data.status == "review" and not workflow.needs_review(
                values, source_count=record["source_count"], conflict=record["conflict"],
            ):
                continue
            if data.status == "registration_review":
                if str(registration_state.get("status") or "") not in {
                    "review_required", "ambiguous", "conflict", "pending_review",
                }:
                    continue
            stage = str((review_flow or {}).get("state") or workflow.review_stage(values))
            if data.review_stage != "all" and stage != data.review_stage:
                continue
            if data.priority != "all" and _priority_bucket(
                current_type, values, record["source_count"], record["conflict"], False, task_state,
            ) != data.priority:
                continue
            if data.watch_categories:
                category_ids = {
                    int(mark.get("category_id")) for mark in watch_state.get("watch_marks", [])
                    if isinstance(mark, dict) and str(mark.get("category_id", "")).isdigit()
                }
                if not category_ids.intersection(set(data.watch_categories)):
                    continue
            if data.qmf_feedback_states and str(qmf_state.get("status") or "") not in set(data.qmf_feedback_states):
                continue
        if data.keyword.strip() and data.keyword.strip().casefold() not in " ".join(
            str(value) for value in values.values()
        ).casefold():
            continue
        sources = sorted(record["sources"], key=lambda source: int(source["source_id"]))
        source = sources[0] if sources else record
        export_rows.append(_mobile_export_row(
            current_type,
            str(record["row_key"]),
            values,
            int(source["source_id"]) if source.get("source_id") is not None else None,
            int(record["source_count"]),
            bool(record["conflict"]),
            False,
            task_state,
            int(source["revision"]) if source.get("revision") is not None else None,
            str(source.get("source_row_hash") or ""),
            review_flow,
            include_internal=include_internal_columns,
        ))
    if include_internal_columns:
        headers = [
            "业务类型", "任务标识", "来源ID", "来源版本", "来源行哈希", "流程版本",
            "姓名", "身份证号", "手机号", "原地址", "现住址", "社区", "核查人",
            "任务状态", "研判阶段", "本次研判结果", "研判意见", "历史研判意见", "已有核查反馈", "核查结果", "截止日期",
            "来源异常", "待同步",
        ]
    else:
        headers = [
            "姓名", "身份证号", "手机号", "原地址", "现住址", "社区", "核查人",
            "任务状态", "研判阶段", "研判意见", "复核反馈", "核查结果", "截止日期",
        ]
    workbook = build_xlsx(
        type_label,
        headers,
        export_rows,
        dropdown_column="P" if include_internal_columns else None,
        dropdown_values=("发现新线索", "未发现新线索"),
        hidden_columns=("A", "C", "D", "E", "F") if include_internal_columns else (),
    )
    return workbook, len(export_rows)


def _source_in_community(
    parser,
    values: dict,
    community_values: list[str] | None,
) -> bool:
    if community_values is None:
        return True
    source_community = parser.community_value(values).strip()
    return bool(source_community and source_community in set(community_values))


@router.get("/home")
async def get_mobile_task_home(
    scope: FlowScope = Query("mine"),
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    if settings.TASK_READ_MODEL_ENABLED:
        return await _get_mobile_task_home_from_read_model(scope, user, conn)
    _require_legacy_read_model_disabled()
    context = await _flow_context(conn, user)
    async with conn.cursor() as cur:
        business_date = await get_business_date(cur)
        readiness = await _source_readiness(cur)
        selected = await _aggregate_live(cur, context, scope, MOBILE_TASK_TYPES)
        personal_scope: FlowScope = "all" if context["admin_mode"] else "mine"
        community_scope: FlowScope = "all" if context["admin_mode"] else "community"
        personal = await _aggregate_live(cur, context, personal_scope, SUMMARY_TASK_TYPES)
        community = await _aggregate_live(cur, context, community_scope, SUMMARY_TASK_TYPES)
        await cur.execute(
            "SELECT MAX(finished_at) FROM _sync_log "
            "WHERE status IN ('success', 'completed')"
        )
        sync_row = await cur.fetchone()

    date_text = business_date.isoformat()
    try:
        today = await get_online_overview(
            date_text,
            date_text,
            SUMMARY_TYPE,
            context["community_values"],
            inspector=None if context["admin_mode"] else context["name"],
            parser_types_override=list(SUMMARY_TASK_TYPES),
        )
    except (RuntimeError, ValueError):
        today = {"exists": False}

    personal_totals = list(personal.values())
    community_totals = list(community.values())
    businesses = sorted(
        selected.values(),
        key=lambda item: (item["pending"] == 0, -item["pending"], item["label"]),
    )
    for item in businesses:
        item["source_ready"] = bool(readiness.get(item["parser_type"]))
    return {
        "business_date": date_text,
        "last_success_at": _iso_utc(sync_row[0]) if sync_row else None,
        "scope": scope,
        "admin_mode": context["admin_mode"],
        "person": {
            "name": context["name"],
            "position": context["position"],
            "community": context["community"],
        },
        "personal": {
            "pending": sum(item["pending"] for item in personal_totals),
            "new_today": int(today.get("new_tasks") or 0) if today.get("exists") else None,
            "carryover_today": int(today.get("carryover_tasks") or 0) if today.get("exists") else None,
            "completed_today": int(today.get("completed_tasks") or 0) if today.get("exists") else None,
        },
        "community": {
            "pending": sum(item["pending"] for item in community_totals),
        },
        "daily_snapshot_available": bool(today.get("exists")),
        "businesses": businesses,
    }


async def _get_mobile_task_home_from_read_model(scope: FlowScope, user: dict, conn) -> dict:
    if scope == "all" and not is_flow_task_elevated(user):
        raise HTTPException(403, "组员和组长只能查看本人或本社区任务")
    context = await _flow_context(conn, user)
    business_date = None
    sync_row = None
    async with conn.cursor() as cur:
        business_date = await get_business_date(cur)
        await cur.execute(
            "SELECT MAX(finished_at) FROM _sync_log "
            "WHERE status IN ('success', 'completed')"
        )
        sync_row = await cur.fetchone()
    personal_scope: FlowScope = "all" if context["admin_mode"] else "mine"
    community_scope: FlowScope = "all" if context["admin_mode"] else "community"

    async def count_scope(current_scope: FlowScope) -> dict[str, dict[str, int]]:
        if current_scope == "all" and not context["admin_mode"]:
            raise HTTPException(403, "组员和组长只能查看本人或本社区任务")
        allowed_communities = context.get("community_values")
        if allowed_communities is not None and not allowed_communities:
            return {
                parser_type: {
                    "unchecked": 0, "checked": 0, "completed": 0,
                    "review": 0, "pending": 0,
                }
                for parser_type in set(MOBILE_TASK_TYPES) | set(SUMMARY_TASK_TYPES)
            }
        run_id = str(settings.TASK_READ_MODEL_RUN_ID or "").strip()
        secret = str(settings.TASK_EVENT_ENCRYPTION_KEY or "")
        allowed_set = None if allowed_communities is None else set(allowed_communities)
        if not run_id or len(secret.encode("utf-8")) < 32:
            raise HTTPException(503, {"error": "projection_pending"})
        try:
            import redis.asyncio as redis
            from services.task_read_model import (
                query_task_read_model_group_ids,
                read_task_read_model_group_records,
                require_task_read_model_ready,
            )

            client = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=5,
            )
            try:
                await client.ping()
                await require_task_read_model_ready(
                    client, settings.APP_ENVIRONMENT, run_id,
                )
                counts = {
                    parser_type: {
                        "unchecked": 0, "checked": 0, "completed": 0,
                        "review": 0, "pending": 0,
                    }
                    for parser_type in set(MOBILE_TASK_TYPES) | set(SUMMARY_TASK_TYPES)
                }
                for parser_type in counts:
                    parser = get_parser(parser_type)
                    offset = 0
                    total = None
                    while total is None or offset < total:
                        page = await query_task_read_model_group_ids(
                            client, settings.APP_ENVIRONMENT, run_id,
                            parser.table_name, {}, allowed_communities, None,
                            offset, 400,
                        )
                        page_total = page.get("total")
                        tokens = page.get("group_tokens")
                        if (type(page_total) is not int or page_total < 0
                                or not isinstance(tokens, list)
                                or len(tokens) != min(400, max(0, page_total - offset))):
                            raise ValueError("task read-model group page contract invalid")
                        if total is None:
                            total = page_total
                        elif total != page_total:
                            raise ValueError("task read-model group total changed")
                        if not tokens:
                            break
                        groups = await read_task_read_model_group_records(
                            client, settings.APP_ENVIRONMENT, run_id,
                            parser.table_name, tokens,
                        )
                        if not isinstance(groups, list) or len(groups) != len(tokens):
                            raise ValueError("task read-model group page mismatch")
                        sources_by_id: dict[int, tuple[int, str, int, str]] = {}
                        expected_sources: dict[int, tuple[str, int, str]] = {}
                        for group in groups:
                            sources = group.get("sources")
                            values = group.get("values")
                            row_key = group.get("row_key")
                            if (not isinstance(sources, list) or not sources
                                    or not isinstance(values, dict)
                                    or not isinstance(row_key, str)
                                    or parser.make_row_key(values) != row_key
                                    or values != sources[0].get("values")
                                    or type(group.get("source_count")) is not int
                                    or group["source_count"] != len(sources)):
                                raise ValueError("task read-model group identity mismatch")
                            if (type(group.get("conflict")) is not bool
                                    or group["conflict"] != any(
                                        stable_json(source.get("values") or {})
                                        != stable_json(values)
                                        for source in sources[1:]
                                    )):
                                raise ValueError("task read-model group conflict mismatch")
                            ordered = sorted(
                                sources,
                                key=lambda item: (int(item.get("source_id") or 0), str(item.get("task_id") or "")),
                            )
                            if sources != ordered:
                                raise ValueError("task read-model group source order invalid")
                            communities = [
                                parser.community_value(source.get("values") or {}).strip()
                                for source in sources
                            ]
                            if (allowed_set is not None
                                    and any(value not in allowed_set for value in communities)):
                                raise ValueError("task read-model group scope mismatch")
                            if current_scope == "mine" and any(
                                str(source["values"].get("核查人") or "").strip().casefold()
                                != str(context["name"]).strip().casefold()
                                for source in sources
                            ):
                                continue
                            for source in sources:
                                source_id = source.get("source_id")
                                revision = source.get("revision")
                                task_id = source.get("task_id")
                                if (type(source_id) is not int or source_id < 1
                                        or type(revision) is not int or revision < 1
                                        or not isinstance(task_id, str)):
                                    raise ValueError("task read-model group source invalid")
                                table, separator, physical_text = task_id.rpartition(":")
                                if (not separator or table != parser.table_name
                                        or not physical_text.isdigit() or int(physical_text) < 1):
                                    raise ValueError("task read-model group source identity invalid")
                                if source_id in expected_sources:
                                    raise ValueError("task read-model group source duplicated")
                                expected_sources[source_id] = (task_id, revision, row_key)
                        if expected_sources:
                            source_ids = sorted(expected_sources)
                            placeholders = ",".join(["%s"] * len(source_ids))
                            row_keys = sorted({item[2] for item in expected_sources.values()})
                            row_key_placeholders = ",".join(["%s"] * len(row_keys))
                            async with conn.cursor() as cur:
                                await cur.execute(
                                    "SELECT source.id,source.physical_row,source.row_key,source.revision "
                                    "FROM _online_source_rows AS source WHERE source.parser_type=%s "
                                    f"AND source.id IN ({placeholders}) AND source.archived_at IS NULL "
                                    + active_source_sql_filter(parser_type, "source"),
                                    [parser_type, *source_ids],
                                )
                                for source_id, physical_row, row_key, revision in await cur.fetchall():
                                    task_id, expected_revision, expected_row_key = expected_sources[int(source_id)]
                                    if (int(revision or 0) != expected_revision
                                            or int(physical_row) != int(task_id.rsplit(":", 1)[1])
                                            or str(row_key) != expected_row_key):
                                        raise ValueError("task read-model source locator mismatch")
                                    sources_by_id[int(source_id)] = (
                                        int(physical_row), str(row_key), int(revision), task_id,
                                    )
                                await cur.execute(
                                    "SELECT source.row_key,COUNT(*) FROM _online_source_rows AS source "
                                    "WHERE source.parser_type=%s AND source.row_key IN ("
                                    + row_key_placeholders + ") AND source.archived_at IS NULL "
                                    + active_source_sql_filter(parser_type, "source")
                                    + " GROUP BY source.row_key",
                                    [parser_type, *row_keys],
                                )
                                source_counts = {
                                    str(row_key): int(count or 0)
                                    for row_key, count in await cur.fetchall()
                                }
                            if set(sources_by_id) != set(expected_sources):
                                raise ValueError("task read-model source locator missing")
                            if any(
                                source_counts.get(row_key, 0)
                                != sum(1 for source in expected_sources.values() if source[2] == row_key)
                                for row_key in row_keys
                            ):
                                raise ValueError("task read-model group source count mismatch")
                        for group in groups:
                            values = group["values"]
                            row_key = group["row_key"]
                            sources = group["sources"]
                            if current_scope == "mine" and any(
                                str(source["values"].get("核查人") or "").strip().casefold()
                                != str(context["name"]).strip().casefold()
                                for source in sources
                            ):
                                continue
                            if not all(
                                (int(source["source_id"]) in sources_by_id
                                 and sources_by_id[int(source["source_id"])][1] == row_key)
                                for source in sources
                            ):
                                continue
                            state = TASK_WORKFLOWS[parser_type].state(values)
                            if state in counts[parser_type]:
                                counts[parser_type][state] += 1
                            if TASK_WORKFLOWS[parser_type].needs_review(
                                values, source_count=group["source_count"],
                                conflict=group["conflict"],
                            ):
                                counts[parser_type]["review"] += 1
                            if state != "completed":
                                counts[parser_type]["pending"] += 1
                        offset += len(tokens)
                return counts
            finally:
                await client.aclose()
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, {"error": "projection_pending"}) from None

    scope_results = {}
    for current_scope in {scope, personal_scope, community_scope}:
        scope_results[current_scope] = await count_scope(current_scope)

    selected_counts = scope_results[scope]
    selected: dict[str, dict] = {}
    for parser_type in MOBILE_TASK_TYPES:
        counts = selected_counts[parser_type]
        selected[parser_type] = {
            "parser_type": parser_type,
            "label": TASK_WORKFLOWS[parser_type].label,
            **counts,
            "source_ready": True,
        }
    personal_pending = sum(
        scope_results[personal_scope][parser_type]["pending"]
        for parser_type in SUMMARY_TASK_TYPES
    )
    community_pending = sum(
        scope_results[community_scope][parser_type]["pending"]
        for parser_type in SUMMARY_TASK_TYPES
    )

    date_text = business_date.isoformat()
    try:
        today = await get_online_overview(
            date_text, date_text, SUMMARY_TYPE, context["community_values"],
            inspector=None if context["admin_mode"] else context["name"],
            parser_types_override=list(SUMMARY_TASK_TYPES),
        )
    except (RuntimeError, ValueError):
        today = {"exists": False}
    businesses = sorted(
        selected.values(), key=lambda item: (item["pending"] == 0, -item["pending"], item["label"]),
    )
    return {
        "business_date": date_text,
        "last_success_at": _iso_utc(sync_row[0]) if sync_row else None,
        "scope": scope,
        "admin_mode": context["admin_mode"],
        "person": {
            "name": context["name"], "position": context["position"],
            "community": context["community"],
        },
        "personal": {
            "pending": personal_pending,
            "new_today": int(today.get("new_tasks") or 0) if today.get("exists") else None,
            "carryover_today": int(today.get("carryover_tasks") or 0) if today.get("exists") else None,
            "completed_today": int(today.get("completed_tasks") or 0) if today.get("exists") else None,
        },
        "community": {"pending": community_pending},
        "daily_snapshot_available": bool(today.get("exists")),
        "businesses": businesses,
    }


def _empty_facets() -> dict:
    return {
        "total": 0,
        "priority_counts": {key: 0 for key in PRIORITY_KEYS},
        "review_stage_counts": {
            key: 0 for key in (
                "initial_pending", "initial_extension", "deep_pending", "deep_extension",
            )
        },
        "status_counts": {key: 0 for key in ("unchecked", "checked", "completed")},
        "registration_review_count": 0,
        "qmf_feedback_counts": {
            key: 0 for key in (
                "not_scanned", "stale", "pending", "completed_match",
                "completed_mismatch", "not_found", "non_jurisdiction", "error",
            )
        },
    }


async def _task_facets(cur, parser_type: str, where_sql: str, params: list) -> dict:
    facets = _empty_facets()
    bucket_sql = _priority_case(parser_type)
    await cur.execute(
        f"""
        SELECT bucket, COUNT(*)
        FROM (
            SELECT {bucket_sql} AS bucket
            FROM _online_source_projection AS projection
            WHERE {where_sql}
        ) AS priority_rows
        GROUP BY bucket
        """,
        params,
    )
    for bucket, count in await cur.fetchall():
        if str(bucket) in facets["priority_counts"]:
            facets["priority_counts"][str(bucket)] = int(count or 0)
    await cur.execute(
        f"""
        SELECT projection.task_state, COUNT(*)
        FROM _online_source_projection AS projection
        WHERE {where_sql}
        GROUP BY projection.task_state
        """,
        params,
    )
    for state, count in await cur.fetchall():
        if str(state) in facets["status_counts"]:
            facets["status_counts"][str(state)] = int(count or 0)
    if is_registration_task(parser_type):
        await cur.execute(
            f"SELECT COUNT(*) FROM _online_source_projection AS projection "
            f"WHERE {where_sql} AND {_registration_review_condition(parser_type)}",
            params,
        )
        facets["registration_review_count"] = int((await cur.fetchone())[0] or 0)
    if parser_type == MODEL_THREE_PARSER:
        await cur.execute(
            f"""
            SELECT qmf_state,COUNT(*)
            FROM (
                SELECT {_qmf_feedback_state_sql()} AS qmf_state
                FROM _online_source_projection AS projection
                WHERE {where_sql} AND projection.task_state='completed'
            ) AS qmf_rows
            GROUP BY qmf_state
            """,
            params,
        )
        for state, count in await cur.fetchall():
            normalized = str(state or "error")
            if normalized in facets["qmf_feedback_counts"]:
                facets["qmf_feedback_counts"][normalized] = int(count or 0)
    facets["total"] = sum(facets["priority_counts"].values())
    return facets


async def _task_filter_options(
    cur,
    parser_type: str,
    context: dict,
    scope: FlowScope,
    user: dict,
    communities: list[str] | None = None,
    small_communities: list[str] | None = None,
    match_status: list[str] | None = None,
    results: list[str] | None = None,
    review_stage: ReviewStage = "all",
) -> dict:
    _require_legacy_read_model_disabled()
    capability_user = _task_capability_user(user)
    scope_where, scope_params = _scope_where(context, scope)
    where_sql = f"projection.parser_type=%s AND {scope_where}"
    params = [parser_type, *scope_params]
    if review_stage != "all":
        where_sql = f"{where_sql} AND {_review_stage_condition(parser_type, review_stage)}"
    inspector_where = where_sql
    inspector_params = list(params)
    community_condition, community_params = _multi_filter_condition(
        "community", communities or []
    )
    small_community_condition, small_community_params = _multi_filter_condition(
        "small_community_name", small_communities or []
    )
    match_status_condition, match_status_params = _multi_filter_condition(
        "address_match_status", match_status or []
    )
    result_condition, result_params = _multi_filter_condition(
        _json_field(TASK_WORKFLOWS[parser_type].result_field), results or []
    )
    if community_condition != "1=1":
        inspector_where = f"{inspector_where} AND {community_condition}"
        inspector_params.extend(community_params)
    if small_community_condition != "1=1":
        inspector_where = f"{inspector_where} AND {small_community_condition}"
        inspector_params.extend(small_community_params)
    if match_status_condition != "1=1":
        inspector_where = f"{inspector_where} AND {match_status_condition}"
        inspector_params.extend(match_status_params)
    if result_condition != "1=1":
        inspector_where = f"{inspector_where} AND {result_condition}"
        inspector_params.extend(result_params)
    result = {
        "communities": [], "small_communities": [],
        "match_statuses": [], "results": [], "inspectors": [], "watch_categories": [],
    }
    for column, key, empty_label in (
        ("community", "communities", "社区未填写"),
        ("small_community_name", "small_communities", "小区未关联"),
        ("address_match_status", "match_statuses", "未匹配"),
        ("inspector", "inspectors", "未分配核查人"),
    ):
        option_where = inspector_where if column == "inspector" else where_sql
        option_params = inspector_params if column == "inspector" else params
        if column == "small_community_name" and communities:
            option_where = f"{option_where} AND {community_condition}"
            option_params = [*option_params, *community_params]
        if column == "address_match_status" and communities:
            option_where = f"{option_where} AND {community_condition}"
            option_params = [*option_params, *community_params]
        await cur.execute(
            f"""
            SELECT projection.{column}, COUNT(*)
            FROM _online_source_projection AS projection
            WHERE {option_where}
            GROUP BY projection.{column}
            ORDER BY CASE WHEN TRIM(COALESCE(projection.{column}, ''))='' THEN 1 ELSE 0 END,
                     projection.{column}
            """,
            option_params,
        )
        for value, count in await cur.fetchall():
            normalized = str(value or "").strip()
            result[key].append({
                "value": normalized or EMPTY_FILTER_VALUE,
                "label": normalized or empty_label,
                "count": int(count or 0),
            })
    result_field = _json_field(TASK_WORKFLOWS[parser_type].result_field)
    result_where = " AND ".join([
        where_sql, community_condition, small_community_condition, match_status_condition,
    ])
    await cur.execute(
        f"SELECT {result_field}, COUNT(*) FROM _online_source_projection AS projection "
        f"WHERE {result_where} GROUP BY {result_field}",
        [*params, *community_params, *small_community_params, *match_status_params],
    )
    result_counts = {str(value or "").strip(): int(count or 0) for value, count in await cur.fetchall()}
    result["results"] = [
        {"value": value or EMPTY_FILTER_VALUE, "label": value or "未填写",
         "count": result_counts.get(value, 0)}
        for value in ["", *TASK_WORKFLOWS[parser_type].result_options]
    ]
    if settings.REGISTRY_FEATURE_ENABLED:
        registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
        await cur.execute(
            f"""
            SELECT category.id, category.name, category.color, category.alert_level,
                   COUNT(DISTINCT projection.row_key)
            FROM _online_source_projection projection
            JOIN `{registry}`.online_task_watch_snapshots snapshot
              ON snapshot.parser_type=projection.parser_type
             AND snapshot.row_key=projection.row_key
            JOIN `{registry}`.watch_assignments assignment
              ON assignment.id=snapshot.assignment_id
            JOIN `{registry}`.watch_categories category
              ON category.id=assignment.category_id
            WHERE {where_sql}
            GROUP BY category.id, category.name, category.color, category.alert_level
            ORDER BY category.sort_order, category.id
            """,
            params,
        )
        result["watch_categories"] = [
            {"value": int(row[0]), "label": str(row[1]), "color": str(row[2]),
             "alert_level": str(row[3]), "count": int(row[4] or 0)}
            for row in await cur.fetchall()
        ]
    assignment_enabled = (
        _can_assign_tasks(context)
        and (
            has_permission(user, ONLINE_RAW_EDIT)
            or has_permission(user, ONLINE_TASK_MANAGE)
        )
    )
    assignment_context = (
        await inspector_option_context(cur, capability_user, assignment_only=True)
        if assignment_enabled
        else {
            "community_aliases": {},
            "inspectors_by_community": {},
        }
    )
    result["assignment"] = {
        "enabled": assignment_enabled,
        "community_aliases": assignment_context["community_aliases"],
        "inspectors_by_community": assignment_context["inspectors_by_community"],
    }
    return result


async def _list_mobile_tasks_data(
    parser_type: str,
    data: TaskSearch,
    user: dict,
    conn,
) -> dict:
    if settings.TASK_READ_MODEL_ENABLED:
        return await _list_mobile_tasks_data_from_read_model(
            parser_type, data, user, conn,
        )
    _require_legacy_read_model_disabled()
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    context = await _flow_context(conn, user)
    where_sql, query_params = _task_where(context, parser_type, data)
    facet_data = data.model_copy(update={
        "status": "all",
        "review_stage": "all",
        "priority": "all",
        "qmf_feedback_states": [],
    })
    base_where, base_params = _task_where(
        context,
        parser_type,
        facet_data,
        include_priority=False,
    )
    async with conn.cursor() as cur:
        ready = await _task_source_ready(cur, parser_type)
        if not ready:
            return {
                "data": [],
                "total": 0,
                "page": data.page,
                "page_size": data.page_size,
                "source_ready": False,
                "message": (
                    "本地任务来源尚未建立"
                    if local_data_source_enabled()
                    else "来源定位尚未建立，请等待一次正常同步"
                ),
                "facets": _empty_facets(),
                "priority_labels": PRIORITY_LABELS,
                "filters": {
                    "scope": data.scope,
                    "status": data.status,
                    "review_stage": data.review_stage,
                    "communities": data.communities,
                    "inspectors": data.inspectors,
                    "watch_categories": data.watch_categories,
                    "qmf_feedback_states": data.qmf_feedback_states,
                    "priority": data.priority,
                    "sort": data.sort,
                    "keyword_present": bool(data.keyword.strip()),
                },
            }
        await cur.execute(
            f"SELECT COUNT(*) FROM _online_source_projection AS projection "
            f"WHERE {where_sql}",
            query_params,
        )
        total = int((await cur.fetchone())[0] or 0)
        facets = await _task_facets(cur, parser_type, base_where, base_params)
        order_sql = _task_order(parser_type, data.sort)
        await cur.execute(
            f"""
            SELECT projection.row_key, projection.values_json,
                   projection.source_count, projection.conflict,
                   projection.pending_state, projection.task_state
            FROM _online_source_projection AS projection
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT %s OFFSET %s
            """,
            [*query_params, data.page_size, (data.page - 1) * data.page_size],
        )
        rows = await cur.fetchall()
        address_matches = await _address_matches_by_rows(
            cur, parser_type, [str(row[0]) for row in rows]
        )
        active_counts = await _active_source_counts(
            cur,
            parser_type,
            [str(row[0]) for row in rows],
        )
        qmf_by_row = await _qmf_status_by_rows(cur, parser_type, rows)
        residence_by_row = await residence_status_by_rows(cur, parser_type, rows)
        registration_by_row = await registration_links_by_rows(
            cur,
            parser_type,
            [str(row[0]) for row in rows],
        )
        review_by_row = await review_flows_by_rows(
            cur, [(parser_type, str(row[0])) for row in rows]
        )
        watch_by_row = await task_watch_payload(
            cur,
            parser_type,
            [str(row[0]) for row in rows],
        ) if settings.REGISTRY_FEATURE_ENABLED else {}
    return {
        "data": [
            _task_record(
                parser_type,
                str(row[0]),
                json_value(row[1], {}),
                active_counts.get(str(row[0]), 0)
                if local_data_source_enabled()
                else int(row[2] or 0),
                active_counts.get(str(row[0]), 0) > 1
                if local_data_source_enabled()
                else bool(row[3]),
                bool(str(row[4] or "")),
                str(row[5] or ""),
                watch_by_row.get(str(row[0])),
                str(row[4] or ""),
                qmf_status=(
                    qmf_by_row.get(str(row[0]))
                    if str(row[5] or "") == "completed"
                    else None
                ),
                residence_status=residence_by_row.get(str(row[0])),
                registration_link=registration_by_row.get(str(row[0])),
                review_flow=review_by_row.get((parser_type, str(row[0]))),
                address_match=address_matches.get(str(row[0])),
            )
            for row in rows
        ],
        "total": total,
        "page": data.page,
        "page_size": data.page_size,
        "source_ready": True,
        "message": "",
        "facets": facets,
        "priority_labels": PRIORITY_LABELS,
        "filters": {
            "scope": data.scope,
            "status": data.status,
            "review_stage": data.review_stage,
            "communities": data.communities,
            "inspectors": data.inspectors,
            "watch_categories": data.watch_categories,
            "qmf_feedback_states": data.qmf_feedback_states,
            "priority": data.priority,
            "sort": "stable_group_token",
            "keyword_present": bool(data.keyword.strip()),
        },
    }


async def _list_mobile_tasks_data_from_read_model(
    parser_type: str,
    data: TaskSearch,
    user: dict,
    conn,
) -> dict:
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    context = await _flow_context(conn, user)
    parser = get_parser(parser_type)
    index_filters = _task_index_filters(
        parser_type, communities=data.communities or None,
        inspectors=data.inspectors or None, results=data.results or None,
    )
    workflow = TASK_WORKFLOWS[parser_type]
    state_filters = {
        **({"address_match.small_community_name": data.small_communities}
           if data.small_communities else {}),
        **({"address_match.status": data.match_status}
           if data.match_status else {}),
        **({"watch.category_id": [str(value) for value in data.watch_categories]}
           if data.watch_categories else {}),
        **({"qmf.state": [str(value) for value in data.qmf_feedback_states]}
           if data.qmf_feedback_states else {}),
    }
    if data.review_stage != "all" and supports_unverifiable_review(parser_type):
        state_filters["review.state"] = [data.review_stage]
    if data.status == "registration_review":
        state_filters["registration.status"] = ["review_required"]
    facets = _empty_facets()
    total = 0
    page_offset = (data.page - 1) * data.page_size
    output: list[dict] = []
    async for groups in _iter_task_read_model_group_pages(
        conn, parser=parser, context=context, scope=data.scope,
        filters=index_filters, keyword=data.keyword,
    ):
        records = [{**group, "parser_type": parser_type} for group in groups]
        states_by_task = await _read_task_view_states_for_records(records)
        for record in records:
            values = record["values"]
            row_key = str(record["row_key"])
            domains = states_by_task.get(str(record["task_id"]), {})
            states = {domain: (value or {}).get("state", {}) for domain, value in domains.items()}
            review_flow = states.get("review") if supports_unverifiable_review(parser_type) else None
            qmf_status = states.get("qmf")
            residence_status = states.get("residence")
            registration_link = states.get("registration")
            watch_status = states.get("watch") or {}
            address_match = states.get("address_match") or {}
            candidate = address_match.get("candidate") or {}
            address_match = {
                **address_match,
                "small_community_id": candidate.get("entry_id"),
                "small_community_name": candidate.get("name") or candidate.get("community_name", ""),
            }
            state = workflow.state(values)
            item = _task_record(
                parser_type, row_key, values,
                int(record["source_count"]), bool(record["conflict"]), False, state,
                watch_status, review_flow=review_flow, qmf_status=qmf_status,
                residence_status=residence_status,
                registration_link=registration_link, address_match=address_match,
            )
            facets["total"] += 1
            if item["priority"] in facets["priority_counts"]:
                facets["priority_counts"][item["priority"]] += 1
            if state in facets["status_counts"]:
                facets["status_counts"][state] += 1
            stage = str((review_flow or {}).get("state") or item["review_stage"])
            if stage in facets["review_stage_counts"]:
                facets["review_stage_counts"][stage] += 1
            registration_state = str((registration_link or {}).get("status") or "")
            if registration_state in {"review_required", "ambiguous", "conflict", "pending_review"}:
                facets["registration_review_count"] += 1
            qmf_state = str((qmf_status or {}).get("state") or "")
            if qmf_state in facets["qmf_feedback_counts"]:
                facets["qmf_feedback_counts"][qmf_state] += 1
            if not state_filters:
                if data.status == "pending" and state == "completed":
                    continue
                if data.status in {"unchecked", "checked", "completed"} and state != data.status:
                    continue
                if data.status == "review" and not item["needs_review"]:
                    continue
                if data.status == "registration_review" and registration_state != "review_required":
                    continue
                if data.review_stage != "all" and item["review_stage"] != data.review_stage:
                    continue
                if data.priority != "all" and item["priority"] != data.priority:
                    continue
                if data.qmf_feedback_states and qmf_state not in set(data.qmf_feedback_states):
                    continue
                if total >= page_offset and len(output) < data.page_size:
                    output.append(item)
                total += 1

    if data.scope != "mine":
        counts = await _indexed_task_state_counts(
            parser, context, scope=data.scope,
            filters=index_filters, keyword=data.keyword,
            dimensions={
                **({"review.state": [
                    "initial_pending", "initial_extension", "deep_pending", "deep_extension",
                ]} if supports_unverifiable_review(parser_type) else {}),
                "qmf.state": list(facets["qmf_feedback_counts"]),
                "registration.status": ["review_required"],
            },
        )
        if counts is not None:
            facets["qmf_feedback_counts"] = counts["qmf.state"]
            facets["registration_review_count"] = counts["registration.status"]["review_required"]
            if "review.state" in counts:
                facets["review_stage_counts"] = counts["review.state"]

    if state_filters:
        async for groups in _iter_task_read_model_group_pages(
            conn, parser=parser, context=context, scope=data.scope,
            filters=index_filters, keyword=data.keyword,
            state_filters=state_filters,
        ):
            records = [{**group, "parser_type": parser_type} for group in groups]
            states_by_task = await _read_task_view_states_for_records(records)
            for record in records:
                values = record["values"]
                row_key = str(record["row_key"])
                states = {
                    domain: (value or {}).get("state", {})
                    for domain, value in states_by_task[str(record["task_id"])].items()
                }
                review_flow = states.get("review") if supports_unverifiable_review(parser_type) else None
                qmf_status = states.get("qmf")
                residence_status = states.get("residence")
                registration_link = states.get("registration")
                watch_status = states.get("watch") or {}
                address_match = states.get("address_match") or {}
                candidate = address_match.get("candidate") or {}
                address_match = {
                    **address_match,
                    "small_community_id": candidate.get("entry_id"),
                    "small_community_name": candidate.get("name") or candidate.get("community_name", ""),
                }
                state = workflow.state(values)
                item = _task_record(
                    parser_type, row_key, values,
                    int(record["source_count"]), bool(record["conflict"]), False, state,
                    watch_status, review_flow=review_flow, qmf_status=qmf_status,
                    residence_status=residence_status, registration_link=registration_link,
                    address_match=address_match,
                )
                registration_state = str((registration_link or {}).get("status") or "")
                qmf_state = str((qmf_status or {}).get("state") or "")
                if data.status == "pending" and state == "completed":
                    continue
                if data.status in {"unchecked", "checked", "completed"} and state != data.status:
                    continue
                if data.status == "review" and not item["needs_review"]:
                    continue
                if data.status == "registration_review" and registration_state != "review_required":
                    continue
                if data.review_stage != "all" and item["review_stage"] != data.review_stage:
                    continue
                if data.priority != "all" and item["priority"] != data.priority:
                    continue
                if data.qmf_feedback_states and qmf_state not in set(data.qmf_feedback_states):
                    continue
                if total >= page_offset and len(output) < data.page_size:
                    output.append(_task_record(
                        parser_type, row_key, values,
                        int(record["source_count"]), bool(record["conflict"]), False, state,
                        watch_status, "", qmf_status=qmf_status if state == "completed" else None,
                        residence_status=residence_status, registration_link=registration_link,
                        review_flow=review_flow, address_match=address_match,
                    ))
                total += 1
    return {
        "data": output,
        "total": total,
        "page": data.page,
        "page_size": data.page_size,
        "source_ready": True,
        "message": "",
        "facets": facets,
        "priority_labels": PRIORITY_LABELS,
        "filters": {
            "scope": data.scope,
            "status": data.status,
            "review_stage": data.review_stage,
            "communities": data.communities,
            "inspectors": data.inspectors,
            "watch_categories": data.watch_categories,
            "qmf_feedback_states": data.qmf_feedback_states,
            "priority": data.priority,
            "sort": "stable_group_token",
            "keyword_present": bool(data.keyword.strip()),
        },
    }


@router.get("/analysis/filter-options")
async def get_mobile_task_analysis_filter_options(
    parser_type: list[str] = Query(default=[]),
    community: list[str] = Query(default=[]),
    review_stage: ReviewStage = Query("all"),
    user: dict = Depends(require_permission(ONLINE_TASK_MANAGE)),
    conn=Depends(get_db),
):
    parser_types = list(dict.fromkeys(parser_type or list(UNVERIFIABLE_REVIEW_TYPES)))
    if any(value not in UNVERIFIABLE_REVIEW_TYPES for value in parser_types):
        raise HTTPException(400, "存在尚未接入研判工作台的业务表")
    context = await _flow_context(conn, user)
    data = AnalysisTaskSearch(
        parser_types=parser_types,
        review_stage=review_stage,
        communities=community,
    )
    if settings.TASK_READ_MODEL_ENABLED:
        if review_stage not in {"all", "initial_pending", "initial_extension", "deep_pending", "deep_extension"}:
            raise HTTPException(400, "研判阶段无效")
        merged = {
            "communities": [], "inspectors": [], "watch_categories": [],
            "small_communities": [], "match_statuses": [], "results": [],
            "assignment": {"enabled": False, "community_aliases": {},
                           "inspectors_by_community": {}},
        }
        for current_type in parser_types:
            parser = get_parser(current_type)
            async for groups in _iter_task_read_model_group_pages(
                conn, parser=parser, context=context, scope="all",
                filters={}, keyword="",
                state_filters=(
                    {"review.state": [review_stage]}
                    if review_stage != "all" else None
                ),
            ):
                records = [{**group, "parser_type": current_type} for group in groups]
                batch = await _mobile_filter_options_from_read_model(
                    conn, current_type, records, context, user,
                    communities=community,
                    review_stage=review_stage,
                    include_assignment_metadata=False,
                )
                _merge_mobile_filter_option_batches(merged, batch)
        metadata = await _mobile_filter_options_from_read_model(
            conn, parser_types[0], [], context, user,
        )
        merged["assignment"] = metadata["assignment"]
        return {
            "source_ready": True,
            **merged,
        }
    async with conn.cursor() as cur:
        ready = all([
            await _task_source_ready(cur, value)
            for value in parser_types
        ])
        if not ready:
            return {
                "source_ready": False,
                "communities": [],
                "inspectors": [],
                "watch_categories": [],
                "assignment": {
                    "enabled": False,
                    "community_aliases": {},
                    "inspectors_by_community": {},
                },
            }
        options = await _analysis_filter_options(cur, context, user, data)
    return {"source_ready": True, **options}


@router.post("/analysis/search")
async def search_mobile_task_analysis(
    data: AnalysisTaskSearch,
    user: dict = Depends(require_permission(ONLINE_TASK_MANAGE)),
    conn=Depends(get_db),
):
    return await _list_analysis_tasks_data(data, user, conn)


@router.post("/analysis/export")
async def export_mobile_task_analysis(
    data: AnalysisTaskSearch,
    request: Request,
    user: dict = Depends(require_permission(ONLINE_TASK_MANAGE)),
    conn=Depends(get_db),
):
    workbook, count = await _mobile_export_workbook(
        data=data, parser_type=None, user=user, conn=conn,
    )
    await record_admin_audit(
        user,
        "mobile_tasks.analysis_export",
        target_type="mobile_task_analysis",
        target_name="研判任务",
        detail={
            "file_format": "XLSX",
            "parser_types": list(dict.fromkeys(data.parser_types)),
            "review_stage": data.review_stage,
            "communities_count": len(data.communities),
            "inspectors_count": len(data.inspectors),
            "sort": data.sort,
            "keyword_present": bool(data.keyword.strip()),
            "rows": count,
        },
        **request_audit_fields(request),
    )
    filename = f"研判任务-{datetime.now():%Y%m%d%H%M%S}.xlsx"
    return StreamingResponse(
        workbook,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.post("/analysis/import")
async def import_mobile_task_analysis(
    request: Request,
    file: UploadFile = File(...),
    user: dict = Depends(require_permission(ONLINE_TASK_MANAGE)),
    conn=Depends(get_db),
):
    """Import a previously exported analysis workbook without creating tasks."""
    content = await file.read()
    try:
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
        sheet = workbook.active
        values = list(sheet.values)
    except Exception as exc:
        raise HTTPException(400, "研判文件无法读取，请上传平台导出的 XLSX 文件") from exc
    if not values:
        raise HTTPException(400, "研判文件为空")
    headers = [str(value or "").strip() for value in values[0]]
    required = ["业务类型", "任务标识", "来源ID", "来源版本", "来源行哈希", "流程版本", "研判阶段", "本次研判结果", "研判意见"]
    legacy_header = "本次研判决定" in headers and "本次研判结果" not in headers
    if legacy_header:
        required[-2] = "本次研判决定"
    missing = [header for header in required if header not in headers]
    if missing:
        raise HTTPException(400, f"研判文件缺少字段：{'、'.join(missing)}")
    index = {header: headers.index(header) for header in required}
    succeeded: list[dict] = []; failed: list[dict] = []; skipped: list[dict] = []; legacy_count = 0
    for row_number, row in enumerate(values[1:], start=2):
        cell = lambda key: str(row[index[key]] or "").strip() if index[key] < len(row) else ""
        outcome = cell(required[-2]).lower()
        if not outcome:
            skipped.append({"row": row_number, "reason": "本次研判结果为空"}); continue
        parser_type = cell("业务类型")
        try:
            source_id = int(cell("来源ID")); expected_revision = int(cell("来源版本")); flow_version = int(cell("流程版本"))
        except ValueError:
            failed.append({"row": row_number, "reason": "来源ID、来源版本和流程版本必须是数字"}); continue
        mapping = {"发现新线索": NEW_CLUE, "未发现新线索": NO_NEW_CLUE, "成功": NEW_CLUE, "研判成功": NEW_CLUE, "success": NEW_CLUE, "失败": NO_NEW_CLUE, "研判失败": NO_NEW_CLUE, "failure": NO_NEW_CLUE}
        normalized_outcome = mapping.get(outcome)
        if normalized_outcome is None:
            failed.append({"row": row_number, "reason": "本次研判结果只能填写发现新线索或未发现新线索"}); continue
        if outcome in {"成功", "研判成功", "success", "失败", "研判失败", "failure"} or legacy_header: legacy_count += 1
        stage = {INITIAL_PENDING: INITIAL_PENDING, UNVERIFIABLE_STATE_LABELS[INITIAL_PENDING]: INITIAL_PENDING, DEEP_PENDING: DEEP_PENDING, UNVERIFIABLE_STATE_LABELS[DEEP_PENDING]: DEEP_PENDING}.get(cell("研判阶段"))
        if stage is None:
            failed.append({"row": row_number, "reason": "研判阶段不是当前可提交的初步或深度待研判状态"}); continue
        if not parser_type or not supports_unverifiable_review(parser_type):
            failed.append({"row": row_number, "reason": "该业务不支持通过研判文件提交两级研判"}); continue
        if not cell("研判意见"):
            failed.append({"row": row_number, "reason": "请填写本次研判意见"}); continue
        try:
            decision = UnverifiableDecision(stage=stage, outcome=normalized_outcome, opinion=cell("研判意见"), flow_version=flow_version, expected_revision=expected_revision, expected_row_hash=cell("来源行哈希"))
            result = await decide_mobile_task_unverifiable_review(parser_type, source_id, decision, request, user, conn)
            succeeded.append({"row": row_number, "task": f"{parser_type}:{cell('任务标识')}", "state": result.get("review_flow", {}).get("state", ""), "next_step": result.get("review_flow", {}).get("state_label", "")})
        except (HTTPException, ValueError) as exc:
            failed.append({"row": row_number, "reason": exc.detail if isinstance(exc, HTTPException) else str(exc)})
        except Exception:
            # A database/connection failure must not turn a mixed workbook into
            # an opaque HTTP 500. Roll back the current transaction and return a
            # safe row-level reason; never expose SQL, credentials, or payloads.
            try:
                await conn.rollback()
            except Exception:
                pass
            failed.append({"row": row_number, "reason": "task_save_failed"})
    await record_admin_audit(user, "mobile_tasks.analysis_import", target_type="mobile_task_analysis", target_name=file.filename or "研判文件", detail={"file_format": "XLSX", "success_count": len(succeeded), "failed_count": len(failed), "skipped_count": len(skipped)}, **request_audit_fields(request))
    return {"success_count": len(succeeded), "failed_count": len(failed), "skipped_count": len(skipped), "legacy_count": legacy_count, "success": succeeded, "failed": failed, "skipped": skipped}


@router.get("/{parser_type}/filter-options")
async def get_mobile_task_filter_options(
    parser_type: str,
    scope: FlowScope = Query("mine"),
    community: list[str] = Query(default=[]),
    small_community: list[str] = Query(default=[]),
    match_status: list[str] = Query(default=[]),
    result: list[str] = Query(default=[]),
    review_stage: ReviewStage = Query("all"),
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    context = await _flow_context(conn, user)
    if settings.TASK_READ_MODEL_ENABLED:
        options = {
            "communities": [], "small_communities": [], "match_statuses": [],
            "results": [], "inspectors": [], "watch_categories": [],
            "assignment": {"enabled": False, "community_aliases": {},
                           "inspectors_by_community": {}},
        }
        parser = get_parser(parser_type)
        async for groups in _iter_task_read_model_group_pages(
            conn, parser=parser, context=context, scope=scope,
            filters=_task_index_filters(parser_type, communities=community),
            keyword="",
            state_filters=(
                {"review.state": [review_stage]}
                if review_stage != "all" and supports_unverifiable_review(parser_type)
                else None
            ),
        ):
            records = [{**group, "parser_type": parser_type} for group in groups]
            batch = await _mobile_filter_options_from_read_model(
                conn, parser_type, records, context, user,
                communities=community,
                small_communities=small_community,
                match_statuses=match_status,
                results=result,
                review_stage=review_stage,
                include_assignment_metadata=False,
            )
            _merge_mobile_filter_option_batches(options, batch)
        if small_community or match_status:
            scoped_options = {
                "communities": [], "small_communities": [], "match_statuses": [],
                "results": [], "inspectors": [], "watch_categories": [],
                "assignment": {"enabled": False, "community_aliases": {},
                               "inspectors_by_community": {}},
            }
            selected_state_filters = {
                **({"address_match.small_community_name": small_community}
                   if small_community else {}),
                **({"address_match.status": match_status}
                   if match_status else {}),
                **({"review.state": [review_stage]}
                   if review_stage != "all" and supports_unverifiable_review(parser_type)
                   else {}),
            }
            async for groups in _iter_task_read_model_group_pages(
                conn, parser=parser, context=context, scope=scope,
                filters=_task_index_filters(parser_type, communities=community),
                keyword="", state_filters=selected_state_filters,
            ):
                records = [{**group, "parser_type": parser_type} for group in groups]
                batch = await _mobile_filter_options_from_read_model(
                    conn, parser_type, records, context, user,
                    communities=community,
                    small_communities=small_community,
                    match_statuses=match_status,
                    results=result,
                    review_stage=review_stage,
                    include_assignment_metadata=False,
                )
                _merge_mobile_filter_option_batches(scoped_options, batch)
            options["results"] = scoped_options["results"]
            options["inspectors"] = scoped_options["inspectors"]
        metadata = await _mobile_filter_options_from_read_model(
            conn, parser_type, [], context, user,
        )
        options["assignment"] = metadata["assignment"]
        return {"source_ready": True, **options}
    async with conn.cursor() as cur:
        if not await _task_source_ready(cur, parser_type):
            return {
                "source_ready": False,
                "communities": [],
                "results": [],
                "inspectors": [],
                "watch_categories": [],
                "assignment": {
                    "enabled": False,
                    "community_aliases": {},
                    "inspectors_by_community": {},
                },
            }
        options = await _task_filter_options(
            cur,
            parser_type,
            context,
            scope,
            user,
            communities=community,
            small_communities=small_community,
            match_status=match_status,
            results=result,
            review_stage=review_stage,
        )
    return {"source_ready": True, **options}


@router.post("/{parser_type}/search")
async def search_mobile_tasks(
    parser_type: str,
    data: TaskSearch,
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    return await _list_mobile_tasks_data(parser_type, data, user, conn)


@router.post("/{parser_type}/export")
async def export_mobile_tasks(
    parser_type: str,
    data: TaskSearch,
    request: Request,
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    workbook, count = await _mobile_export_workbook(
        data=data, parser_type=parser_type, user=user, conn=conn,
    )
    await record_admin_audit(
        user,
        "mobile_tasks.export",
        target_type="mobile_task",
        target_name=parser_type,
        detail={
            "file_format": "XLSX",
            "status": data.status,
            "review_stage": data.review_stage,
            "communities_count": len(data.communities),
            "inspectors_count": len(data.inspectors),
            "sort": data.sort,
            "keyword_present": bool(data.keyword.strip()),
            "rows": count,
        },
        **request_audit_fields(request),
    )
    filename = f"{parser_type}-任务-{datetime.now():%Y%m%d%H%M%S}.xlsx"
    return StreamingResponse(
        workbook,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.post("/{parser_type}/assignment-selection")
async def select_mobile_tasks_for_assignment(
    parser_type: str,
    data: TaskSearch,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    if settings.TASK_READ_MODEL_ENABLED:
        return await _select_mobile_tasks_for_assignment_from_read_model(
            parser_type, data, user, conn,
        )
    """Resolve every currently eligible task for the complete filter result."""
    _require_legacy_read_model_disabled()
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    user = _require_task_edit_user(user)
    context = await _flow_context(conn, user)
    if not _can_assign_tasks(context):
        raise HTTPException(403, "只有组长及有权管理任务的上级岗位可以批量分配核查人")
    if data.scope == "all" and not context.get("admin_mode"):
        raise HTTPException(403, "组员和组长只能查看本人或本社区任务")

    where_sql, query_params = _task_where(context, parser_type, data)
    assignment_source_condition = "TRIM(COALESCE(projection.community,''))<>''"
    if local_data_source_enabled():
        assignment_source_condition = (
            f"{_active_source_count_sql(parser_type)} <= 1 "
            "AND TRIM(COALESCE(projection.community,''))<>''"
        )
    async with conn.cursor() as cur:
        if not local_data_source_enabled() and not await _writeback_enabled(cur):
            raise HTTPException(503, "在线回写已由超级管理员暂停")
        await cur.execute(
            f"""
            SELECT projection.row_key, projection.community, projection.small_community_name,
                   projection.address_match_status
            FROM _online_source_projection AS projection
            WHERE {where_sql}
              AND TRIM(COALESCE(projection.inspector, ''))=''
              AND projection.task_state<>'completed'
              AND {assignment_source_condition}
              AND EXISTS (
                  SELECT 1 FROM _online_source_rows AS source_row
                  WHERE source_row.parser_type=projection.parser_type
                    AND source_row.row_key=projection.row_key
                    AND source_row.archived_at IS NULL
                    {active_source_sql_filter(parser_type, 'source_row')}
              )
            ORDER BY {_address_order(parser_type)}, projection.row_key
            LIMIT %s
            """,
            [*query_params, MAX_BULK_ASSIGNMENT_TASKS + 1],
        )
        rows = await cur.fetchall()
        if len(rows) > MAX_BULK_ASSIGNMENT_TASKS:
            raise HTTPException(
                409,
                f"当前筛选可分配任务超过 {MAX_BULK_ASSIGNMENT_TASKS} 条，请继续缩小筛选范围",
            )
        assignment_context = await inspector_option_context(
            cur,
            user,
            assignment_only=True,
        )

    aliases = assignment_context["community_aliases"]
    formal_communities = {
        aliases.get(str(community or "").strip(), "")
        for _, community, _, _ in rows
    }
    if "" in formal_communities:
        raise HTTPException(403, "部分任务社区不在当前账号可分配范围内")
    if len(formal_communities) > 1:
        raise HTTPException(400, "请先筛选到一个社区，再全选当前筛选结果")
    formal_community = next(iter(formal_communities), "")
    if formal_community and not assignment_context["inspectors_by_community"].get(
        formal_community
    ):
        raise HTTPException(400, "该社区当前没有在岗组员可分配")
    return {
        "row_keys": [str(row[0]) for row in rows],
        "total": len(rows),
        "community": formal_community,
    }


async def _iter_assignment_read_model_groups(
    parser_type: str,
    conn,
    context: dict,
    *,
    scope: FlowScope,
    filters: dict[str, list[str]] | None = None,
    state_filters: dict[str, list[str]] | None = None,
    keyword: str = "",
):
    """Yield verified canonical task groups in bounded Redis pages."""
    parser = get_parser(parser_type)
    communities = context.get("community_values")
    if communities is not None and not communities:
        return
    query_filters = {field: list(values) for field, values in (filters or {}).items() if values}
    inspector = str(context.get("name") or "").strip()
    if scope == "mine":
        inspectors = query_filters.get("核查人")
        if inspectors:
            query_filters["核查人"] = [
                value for value in inspectors
                if value.strip().casefold() == inspector.casefold()
            ]
            if not query_filters["核查人"]:
                return
        elif inspector:
            query_filters["核查人"] = [inspector]
        else:
            return

    run_id = str(settings.TASK_READ_MODEL_RUN_ID or "").strip()
    secret = str(settings.TASK_EVENT_ENCRYPTION_KEY or "")
    if not run_id or len(secret.encode("utf-8")) < 32:
        raise HTTPException(503, {"error": "projection_pending"})
    try:
        import redis.asyncio as redis
        from services.task_read_model import (
            query_task_read_model_group_ids,
            read_task_read_model_group_records,
            require_task_read_model_ready,
        )

        client = redis.from_url(
            settings.REDIS_URL, decode_responses=True,
            socket_connect_timeout=3, socket_timeout=5,
        )
        try:
            await client.ping()
            await require_task_read_model_ready(client, settings.APP_ENVIRONMENT, run_id)
            total = None
            offset = 0
            page_size = 400
            allowed_set = None if communities is None else set(communities)
            needle = str(keyword or "").strip().casefold()
            while total is None or offset < total:
                page = await query_task_read_model_group_ids(
                    client, settings.APP_ENVIRONMENT, run_id, parser.table_name,
                    query_filters, communities,
                    keyword if len(needle) >= 2 else None,
                    offset, page_size, state_filters=state_filters,
                )
                page_total = page.get("total")
                tokens = page.get("group_tokens")
                if (type(page_total) is not int or page_total < 0
                        or not isinstance(tokens, list)
                        or len(tokens) != min(page_size, max(0, page_total - offset))):
                    raise ValueError("assignment group page contract invalid")
                if total is None:
                    total = page_total
                elif total != page_total:
                    raise ValueError("assignment group total changed")
                if not tokens:
                    break
                groups = await read_task_read_model_group_records(
                    client, settings.APP_ENVIRONMENT, run_id, parser.table_name, tokens,
                )
                if not isinstance(groups, list) or len(groups) != len(tokens):
                    raise ValueError("assignment group page mismatch")
                source_entries = []
                candidate_groups = []
                for group in groups:
                    sources = group.get("sources")
                    canonical = group.get("values")
                    row_key = group.get("row_key")
                    if (not isinstance(sources, list) or not sources
                            or not isinstance(canonical, dict)
                            or not isinstance(row_key, str)
                            or parser.make_row_key(canonical) != row_key
                            or canonical != sources[0].get("values")
                            or group.get("source_count") != len(sources)
                            or type(group.get("conflict")) is not bool):
                        raise ValueError("assignment group identity mismatch")
                    ordered = sorted(
                        sources, key=lambda source: (int(source.get("source_id") or 0),
                                                     str(source.get("task_id") or "")),
                    )
                    if sources != ordered:
                        raise ValueError("assignment group canonical order mismatch")
                    if allowed_set is not None and any(
                        parser.community_value(source.get("values") or {}).strip() not in allowed_set
                        for source in sources
                    ):
                        raise ValueError("assignment group scope mismatch")
                    if scope == "mine" and any(
                        str(source.get("values", {}).get("核查人") or "").strip().casefold()
                        != inspector.casefold() for source in sources
                    ):
                        continue
                    if any(
                        str(canonical.get(field, "") or "") not in accepted
                        for field, accepted in query_filters.items()
                    ):
                        continue
                    if needle and not any(
                        needle in str(value or "").casefold() for value in canonical.values()
                    ):
                        continue
                    candidate_groups.append(group)
                    for source in sources:
                        task_id = str(source.get("task_id") or "")
                        table_name, separator, raw_physical = task_id.rpartition(":")
                        source_id = source.get("source_id")
                        revision = source.get("revision")
                        if (not separator or table_name != parser.table_name
                                or not raw_physical.isdigit() or int(raw_physical) < 1
                                or type(source_id) is not int or source_id < 1
                                or type(revision) is not int or revision < 1):
                            raise ValueError("assignment group source invalid")
                        source_entries.append((source_id, int(raw_physical), revision,
                                               row_key, task_id, source))

                locators: dict[int, tuple[int, int, str, str]] = {}
                source_ids = [entry[0] for entry in source_entries]
                for batch_start in range(0, len(source_ids), 400):
                    batch = source_ids[batch_start:batch_start + 400]
                    marks = ",".join(["%s"] * len(batch))
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT id,revision,physical_row,row_key,row_hash "
                            "FROM _online_source_rows AS source WHERE parser_type=%s "
                            "AND source.archived_at IS NULL "
                            f"AND id IN ({marks}) "
                            f"{active_source_sql_filter(parser_type)}",
                            [parser_type, *batch],
                        )
                        for source_id, revision, physical_row, row_key, row_hash in await cur.fetchall():
                            source_id = int(source_id)
                            if source_id in locators:
                                raise ValueError("assignment source locator is ambiguous")
                            locators[source_id] = (
                                int(revision or 0), int(physical_row), str(row_key), str(row_hash or ""),
                            )
                if len(locators) != len(source_entries):
                    raise ValueError("assignment source locator is missing")
                verified_groups = []
                for group in candidate_groups:
                    row_key = group["row_key"]
                    verified_sources = []
                    for source in group["sources"]:
                        source_id = int(source["source_id"])
                        expected_task_id = str(source["task_id"])
                        table_name, _separator, raw_physical = expected_task_id.rpartition(":")
                        locator = locators.get(source_id)
                        if (locator is None or locator[0] != int(source["revision"])
                                or locator[1] != int(raw_physical)
                                or locator[2] != row_key):
                            raise ValueError("assignment source locator mismatch")
                        verified_sources.append({**source, "source_row_hash": locator[3]})
                    verified_groups.append({
                        **group, "sources": verified_sources,
                        "canonical_source": verified_sources[0],
                    })
                offset += len(tokens)
                yield verified_groups
        finally:
            await client.aclose()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, {"error": "projection_pending"}) from None


async def _select_mobile_tasks_for_assignment_from_read_model(
    parser_type: str,
    data: TaskSearch,
    user: dict,
    conn,
) -> dict:
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    user = _require_task_edit_user(user)
    context = await _flow_context(conn, user)
    if not _can_assign_tasks(context):
        raise HTTPException(403, "只有组长及有权管理任务的上级岗位可以批量分配核查人")
    if data.scope == "all" and not context.get("admin_mode"):
        raise HTTPException(403, "组员和组长只能查看本人或本社区任务")
    workflow = TASK_WORKFLOWS[parser_type]
    parser = get_parser(parser_type)
    requested_communities = set(data.communities)
    requested_inspectors = set(data.inspectors)
    selected: list[dict] = []
    filters = _task_index_filters(
        parser_type, communities=data.communities,
        inspectors=data.inspectors, results=data.results,
    )
    async for group_page in _iter_assignment_read_model_groups(
        parser_type, conn, context, scope=data.scope,
        filters=filters,
        state_filters={
            **({"address_match.small_community_name": data.small_communities} if data.small_communities else {}),
            **({"address_match.status": data.match_status} if data.match_status else {}),
            **({"watch.category_id": [str(value) for value in data.watch_categories]} if data.watch_categories else {}),
            **({"qmf.state": data.qmf_feedback_states} if data.qmf_feedback_states else {}),
        },
        keyword=data.keyword or "",
    ):
        candidates = []
        for group in group_page:
            record = {
                **group["canonical_source"],
                "values": group["values"], "row_key": group["row_key"],
                "source_count": group["source_count"], "conflict": group["conflict"],
                "sources": group["sources"],
            }
            values = record["values"]
            community = parser.community_value(values).strip()
            inspector = str(values.get("核查人") or "").strip()
            state = workflow.state(values)
            if requested_communities and community not in requested_communities:
                continue
            if requested_inspectors and inspector not in requested_inspectors:
                continue
            if data.results and str(values.get(workflow.result_field) or "").strip() not in set(data.results):
                continue
            if data.status == "pending" and state == "completed":
                continue
            if data.status in {"unchecked", "checked", "completed"} and state != data.status:
                continue
            if data.status == "review" and not workflow.needs_review(
                values, source_count=record["source_count"], conflict=record["conflict"],
            ):
                continue
            if data.review_stage != "all" and workflow.review_stage(values) != data.review_stage:
                continue
            if data.priority != "all" and _priority_bucket(
                parser_type, values, record["source_count"], record["conflict"], False, state,
            ) != data.priority:
                continue
            if inspector or not community or record["source_count"] != 1 or record["conflict"]:
                continue
            candidates.append(record)
        states_by_task = await _read_task_view_states_for_records(candidates)
        for record in candidates:
            values = record["values"]
            domains = states_by_task[str(record["task_id"])]
            address_state = domains["address_match"]["state"]
            candidate = address_state.get("candidate") or {}
            address_match = {
                **address_state,
                "small_community_name": candidate.get("name") or candidate.get("community_name", ""),
            }
            if (data.small_communities and str(address_match.get("small_community_name") or "")
                    not in set(data.small_communities)):
                continue
            if (data.match_status and str(address_match.get("status") or "unmatched")
                    not in set(data.match_status)):
                continue
            watch_state = domains["watch"]["state"]
            if data.watch_categories and not {
                int(mark.get("category_id")) for mark in watch_state.get("watch_marks", [])
                if isinstance(mark, dict) and str(mark.get("category_id", "")).isdigit()
            }.intersection(set(data.watch_categories)):
                continue
            qmf_state = domains["qmf"]["state"]
            if (data.qmf_feedback_states
                    and str(qmf_state.get("status") or "") not in set(data.qmf_feedback_states)):
                continue
            selected.append(record)
            if len(selected) > MAX_BULK_ASSIGNMENT_TASKS:
                raise HTTPException(
                    409,
                    f"当前筛选可分配任务超过 {MAX_BULK_ASSIGNMENT_TASKS} 条，请继续缩小筛选范围",
                )

    async with conn.cursor() as cur:
        assignment_context = await inspector_option_context(cur, user, assignment_only=True)
    aliases = assignment_context["community_aliases"]
    formal_communities = {aliases.get(
        get_parser(parser_type).community_value(record["values"]).strip(), ""
    ) for record in selected}
    if "" in formal_communities:
        raise HTTPException(403, "部分任务社区不在当前账号可分配范围内")
    if len(formal_communities) > 1:
        raise HTTPException(400, "请先筛选到一个社区，再全选当前筛选结果")
    formal_community = next(iter(formal_communities), "")
    if formal_community and not assignment_context["inspectors_by_community"].get(formal_community):
        raise HTTPException(400, "该社区当前没有在岗组员可分配")
    return {
        "row_keys": [str(record["row_key"]) for record in selected],
        "total": len(selected),
        "community": formal_community,
    }


ADDRESS_MANUAL_UNMATCHED_LABELS = MANUAL_UNMATCHED_REASON_LABELS


@router.get("/{parser_type}/{row_key}/address-match/options")
async def get_mobile_task_address_match_options(
    parser_type: str,
    row_key: str,
    keyword: str = Query(default="", max_length=100),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=30, ge=1, le=100),
    selected_id: int | None = Query(default=None, gt=0),
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    """Return the task fence and searchable same-community address entries.

    The endpoint deliberately exposes only enabled entries belonging to the
    task's formal community.  It can read automatically matched and already
    confirmed tasks so a deep link from a task label remains useful.
    """
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    context = await _flow_context(conn, user)
    task = None
    if settings.TASK_READ_MODEL_ENABLED:
        records = await _task_read_model_records(
            conn, parser_type, context,
            scope="all" if context.get("admin_mode") else "community",
        )
        record = next((item for item in records if item["row_key"] == row_key), None)
        if not record:
            raise HTTPException(404, "任务不存在或不在当前账号可查看范围内")
        sources = record.get("sources") or []
        if not sources:
            raise HTTPException(503, {"error": "projection_pending"})
        source = sources[0]
        values = record["values"]
        parser = get_parser(parser_type)
        task_community_raw = parser.community_value(values).strip()
        source_id = int(source["source_id"])
        revision = int(source["revision"])
        row_hash = str(source.get("source_row_hash") or "")
        domains = await _read_task_view_states_for_records([record])
        address_state = domains[str(record["task_id"])]["address_match"]["state"]
        candidate = address_state.get("candidate") or {}
        original_address = str(values.get("現住址") or values.get("地址") or "")
        task = (
            task_community_raw,
            int(record["source_count"]),
            int(bool(record["conflict"])),
            original_address, source_id, revision, row_hash,
            candidate.get("entry_id"), candidate.get("community_name", ""),
            address_state.get("status", "unmatched"),
            address_state.get("reason", ""), address_state.get("candidates", []),
            revision, values, address_state.get("manual_unmatched_reason", ""),
        )
    else:
        _require_legacy_read_model_disabled()
        scope_where, scope_params = _scope_where(
            context,
            "all" if context.get("admin_mode") else "community",
        )
    async with conn.cursor() as cur:
        if not settings.TASK_READ_MODEL_ENABLED:
            await cur.execute(
                f"""
                SELECT projection.community, projection.source_count,
                       projection.conflict, address_match.original_address,
                       source.id, source.revision, source.row_hash,
                       projection.small_community_id, projection.small_community_name,
                       projection.address_match_status, projection.address_match_reason,
                       projection.address_match_candidates, projection.source_revision,
                       projection.values_json, address_match.manual_unmatched_reason
                FROM _online_source_projection AS projection
                LEFT JOIN _online_task_address_matches AS address_match
                  ON address_match.parser_type=projection.parser_type
                 AND address_match.row_key=projection.row_key
                JOIN _online_source_rows AS source
                  ON source.parser_type=projection.parser_type
                 AND source.row_key=projection.row_key
                 AND source.archived_at IS NULL
                WHERE projection.parser_type=%s AND projection.row_key=%s
                  AND source.spreadsheet_id=0
                  AND source.source_kind IN ('local_table','local_dispatch','one_time_continuation_import')
                  AND {scope_where}
                ORDER BY source.id LIMIT 1
                """,
                (parser_type, row_key, *scope_params),
            )
            task = await cur.fetchone()
        if not task:
            raise HTTPException(404, "任务不存在或不在当前账号可查看范围内")

        task_community_raw = str(task[0] or "").strip()
        # Address options are a read operation.  Resolve aliases from the
        # registry directly instead of using ``inspector_option_context``:
        # that helper intentionally limits its result to users with
        # ``online.raw.edit`` and would make a read-only task viewer see an
        # empty option list (or a non-formal community).  The write endpoints
        # still perform their own permission and community checks.
        task_community = await formal_community(cur, task_community_raw)
        task_community = task_community or task_community_raw
        normalized_keyword = keyword.strip()
        entry_where = (
            "community.name=%s AND entry.enabled=1 AND community.is_active=1"
        )
        entry_params: list[object] = [task_community]
        if normalized_keyword:
            entry_where += " AND (entry.name LIKE %s OR entry.normalized_name LIKE %s)"
            entry_params.extend([
                f"%{normalized_keyword}%", f"%{normalized_keyword}%",
            ])
        await cur.execute(
            f"""
            SELECT entry.id, entry.name, entry.community_id, community.name,
                   entry.detail_address
            FROM _police_address_entries AS entry
            JOIN _communities AS community ON community.id=entry.community_id
            WHERE {entry_where}
            ORDER BY entry.name, entry.id
            LIMIT %s OFFSET %s
            """,
            (*entry_params, page_size, (page - 1) * page_size),
        )
        entries = await cur.fetchall()
        await cur.execute(
            f"""
            SELECT COUNT(*)
            FROM _police_address_entries AS entry
            JOIN _communities AS community ON community.id=entry.community_id
            WHERE {entry_where}
            """,
            entry_params,
        )
        total = int((await cur.fetchone() or (0,))[0] or 0)

        # Keep a selected entry visible when the current search term or page
        # would otherwise hide it.  The same-community predicate is reused.
        selected = None
        if selected_id:
            await cur.execute(
                """
                SELECT entry.id, entry.name, entry.community_id, community.name,
                       entry.detail_address
                FROM _police_address_entries AS entry
                JOIN _communities AS community ON community.id=entry.community_id
                WHERE entry.id=%s AND entry.enabled=1 AND community.is_active=1
                  AND community.name=%s
                """,
                (selected_id, task_community),
            )
            selected = await cur.fetchone()
        if selected and not any(int(row[0]) == int(selected[0]) for row in entries):
            entries = [selected, *entries]

        if settings.TASK_READ_MODEL_ENABLED:
            history = list(address_state.get("history") or [])
        else:
            await cur.execute(
                """
                SELECT reason_code, created_at, source_id, source_revision
                FROM _online_task_address_unmatched_events
                WHERE parser_type=%s AND row_key=%s
                ORDER BY id DESC LIMIT 20
                """,
                (parser_type, row_key),
            )
            history = [
                {
                    "reason_code": str(row[0] or ""),
                    "created_at": _iso_utc(row[1]),
                    "source_id": int(row[2]) if row[2] is not None else None,
                    "source_revision": int(row[3]) if row[3] is not None else None,
                }
                for row in await cur.fetchall()
            ]

        can_manage = _can_assign_tasks(context) and (
            has_permission(user, ONLINE_RAW_EDIT)
            or has_permission(user, ONLINE_TASK_MANAGE)
        )
    editable = can_manage and int(task[1] or 0) == 1 and not bool(task[2])
    status = str(task[9] or "unmatched")
    source_values = json_value(task[13], {})
    return {
        "task": {
            "parser_type": parser_type,
            "row_key": row_key,
            "source_id": int(task[4]),
            "revision": int(task[5]),
            "row_hash": str(task[6] or ""),
            "source_revision": int(task[12] or task[5] or 0),
            "community": task_community,
            "original_address": str(task[3] or ""),
            "current_address": str(source_values.get("现住址") or ""),
            "small_community_id": int(task[7]) if task[7] is not None else None,
            "small_community_name": str(task[8] or ""),
            "address_match_status": status,
            "address_match_reason": str(task[10] or ""),
            "address_match_candidates": json_value(task[11], []),
            "manual_unmatched_reason": str(task[14] or ""),
        },
        "community": task_community,
        "items": [
            {
                "id": int(row[0]),
                "name": str(row[1] or ""),
                "community_id": int(row[2]) if row[2] is not None else None,
                "community_name": str(row[3] or ""),
                "detail_address": str(row[4] or ""),
            }
            for row in entries
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "capabilities": {
            "confirm": editable,
            "manual_unmatched": editable,
            "resolve_conflict": int(task[1] or 0) == 1 and not bool(task[2])
            and can_confirm_registration(user) and (
                has_permission(user, ONLINE_RAW_EDIT)
                or has_permission(user, ONLINE_TASK_MANAGE)
            ),
        },
        "manual_unmatched_reasons": [
            {"code": code, "label": label}
            for code, label in ADDRESS_MANUAL_UNMATCHED_LABELS.items()
        ],
        "manual_unmatched_reason": str(task[14] or ""),
        "history": history,
    }


@router.post("/{parser_type}/{row_key}/address-match/manual-unmatched")
async def mark_mobile_task_address_manual_unmatched(
    parser_type: str,
    row_key: str,
    data: AddressMatchManualUnmatched,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Record an explicit human conclusion that no known community applies."""
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    user = _require_task_edit_user(user)
    context = await _flow_context(conn, user)
    if not _can_assign_tasks(context):
        raise HTTPException(403, "只有组长及有权管理任务的上级岗位可以标注小区归属")
    scope_where, scope_params = _scope_where(
        context,
        "all" if context.get("admin_mode") else "community",
    )
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT projection.community, projection.source_count,
                       projection.conflict, address_match.original_address,
                       source.id, source.revision, source.row_hash,
                       source.physical_row, projection.source_revision,
                       projection.values_json
                FROM _online_source_projection AS projection
                LEFT JOIN _online_task_address_matches AS address_match
                  ON address_match.parser_type=projection.parser_type
                 AND address_match.row_key=projection.row_key
                JOIN _online_source_rows AS source
                  ON source.parser_type=projection.parser_type
                 AND source.row_key=projection.row_key
                 AND source.archived_at IS NULL
                WHERE projection.parser_type=%s AND projection.row_key=%s
                  AND source.spreadsheet_id=0
              AND source.source_kind IN ('local_table','local_dispatch','one_time_continuation_import')
                  AND {scope_where}
                FOR UPDATE
                """,
                (parser_type, row_key, *scope_params),
            )
            projection = await cur.fetchone()
            if not projection:
                raise HTTPException(404, "任务不存在或不在当前账号可操作范围内")
            if bool(projection[2]) or int(projection[1] or 0) != 1:
                raise HTTPException(409, "任务存在重复或冲突来源，暂不能标注小区")
            if (
                int(projection[4] or 0) != int(data.source_id)
                or int(projection[5] or 0) != int(data.expected_revision)
                or str(projection[6] or "") != str(data.expected_row_hash)
            ):
                raise HTTPException(409, "任务已发生变化，请刷新后重新标注")
            assignment_context = await inspector_option_context(
                cur, user, assignment_only=True,
            )
            aliases = assignment_context.get("community_aliases") or {}
            community_name = aliases.get(
                str(projection[0] or "").strip(), str(projection[0] or "").strip()
            )
            address = str(projection[3] or "")
            source_values = json_value(projection[9], {})
            original_address, current_address = _task_annotation_addresses(
                parser_type, source_values
            )
            next_revision = int(projection[5]) + 1
            annotation_key = annotation_hmac(
                original_address, current_address, community_name
            )
            async def authorize(_command_cur, locked_source):
                if (
                    locked_source is None
                    or int(locked_source["revision"]) != int(projection[5])
                    or int(locked_source["physical_row"]) != int(projection[7])
                ):
                    raise HTTPException(409, "任务已发生变化，请刷新后重新标注")
                if not context.get("admin_mode"):
                    allowed = {
                        str(value).strip()
                        for value in context.get("community_values") or []
                        if str(value).strip()
                    }
                    if community_name not in allowed:
                        raise HTTPException(404, "任务不存在或不在当前账号可操作范围内")
                else:
                    allowed = {"*"}
                return TaskAuthorizationResult(
                    policy_key="online.address_match.manual_unmatched",
                    scope_values=tuple(sorted(allowed)),
                )

            async def mutate(command_cur, _locked_source):
                await record_feedback_unmatched(
                    command_cur,
                    parser_type=parser_type,
                    row_key=row_key,
                    address=original_address,
                    community_name=community_name,
                    current_address=current_address,
                    reason_code=data.reason_code,
                    recorded_by=int(user["id"]),
                    source_id=int(projection[4]),
                    source_revision=next_revision,
                )
                await command_cur.execute(
                    """
                    INSERT INTO _online_task_address_matches (
                        parser_type, row_key, original_address,
                        suggested_entry_id, suggested_community_id,
                        suggested_community_name, match_status, match_score,
                        match_method, match_reason, candidates_json,
                        matcher_version, confirmed_entry_id, confirmed_by,
                        confirmed_at, manual_unmatched_reason,
                        manual_unmatched_address_hmac, manual_unmatched_by,
                        manual_unmatched_at
                    ) VALUES (%s,%s,%s,NULL,NULL,'','manual_unmatched',0,
                              '人工标注',%s,JSON_ARRAY(),%s,NULL,%s,NULL,
                              %s,%s,%s,UTC_TIMESTAMP())
                    ON DUPLICATE KEY UPDATE
                        original_address=VALUES(original_address),
                        suggested_entry_id=NULL, suggested_community_id=NULL,
                        suggested_community_name='', match_status='manual_unmatched',
                        match_score=0, match_method='人工标注',
                        match_reason=VALUES(match_reason), candidates_json=JSON_ARRAY(),
                        matcher_version=VALUES(matcher_version), confirmed_entry_id=NULL,
                        confirmed_by=VALUES(confirmed_by), confirmed_at=NULL,
                        manual_unmatched_reason=VALUES(manual_unmatched_reason),
                        manual_unmatched_address_hmac=VALUES(manual_unmatched_address_hmac),
                        manual_unmatched_by=VALUES(manual_unmatched_by),
                        manual_unmatched_at=UTC_TIMESTAMP()
                    """,
                    (
                        parser_type, row_key, original_address or current_address,
                        ADDRESS_MANUAL_UNMATCHED_LABELS[data.reason_code], MATCHER_VERSION,
                        int(user["id"]), data.reason_code, annotation_key, int(user["id"]),
                    ),
                )
                await command_cur.execute(
                    "UPDATE _online_source_rows SET revision=%s WHERE id=%s AND revision=%s",
                    (next_revision, int(projection[4]), int(projection[5])),
                )
                if command_cur.rowcount != 1:
                    raise HTTPException(409, "任务已发生变化，请刷新后重新标注")
                await command_cur.execute(
                    "UPDATE _local_source_records SET revision=%s,updated_at=UTC_TIMESTAMP() "
                    "WHERE parser_type=%s AND local_task_id=%s AND status='active' AND revision=%s",
                    (next_revision, parser_type, int(projection[7]), int(projection[5])),
                )
                if command_cur.rowcount != 1:
                    raise HTTPException(409, "本地任务来源已变化，请刷新后重新标注")
                from services.task_view_state import (
                    enqueue_source_task_view_state, task_view_state_events_enabled,
                )
                if task_view_state_events_enabled():
                    await enqueue_source_task_view_state(
                        command_cur, source_id=int(projection[4]), domain="address_match",
                        state={"status": "manual_unmatched", "reason_code": data.reason_code,
                               "matcher_version": MATCHER_VERSION},
                        operation_id=f"address-manual-unmatched-{projection[4]}-{next_revision}",
                    )
                from services.business_time import get_business_date
                from services.online_summary_updates import enqueue_online_summary_update
                await enqueue_online_summary_update(
                    command_cur, task_id=int(projection[7]), parser_type=parser_type,
                    row_key=row_key, revision=next_revision,
                    business_date=await get_business_date(command_cur),
                    operation_id=f"address-manual-unmatched-{projection[4]}-{next_revision}",
                )
                await rebuild_projection_rows(command_cur, parser_type, [row_key])
                address_match = (await _address_matches_by_rows(
                    command_cur, parser_type, [row_key],
                )).get(row_key)
                return TaskMutationResult(
                    revision=next_revision,
                    snapshot={**source_values, "address_match": {
                        "status": "manual_unmatched", "reason_code": data.reason_code,
                    }},
                    changed_fields=("small_community",),
                    result=address_match,
                )

            command = await TaskCommandService.execute(
                cur,
                spec=TaskCommandSpec(
                    actor_id=int(user["id"]), environment=settings.APP_ENVIRONMENT,
                    parser_type=parser_type,
                    task_id=stable_task_id(get_parser(parser_type).table_name, int(projection[7])),
                    source_id=int(projection[4]), action="address_conflict_resolve",
                    event_type="online.task.changed", expected_revision=int(projection[5]),
                    audiences=("authenticated",),
                ),
                authorize=authorize,
                mutate=mutate,
            )
            result = command.result
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "mobile_tasks.address_match.manual_unmatched",
        conn=conn,
        target_type="mobile_task_address_match",
        target_name=f"{parser_type}:{row_key}",
        detail={"reason_code": data.reason_code},
        **request_audit_fields(request),
    )
    return {"message": "已记录无匹配小区", "address_match": result}


@router.post("/{parser_type}/{row_key}/address-match/confirm")
async def confirm_mobile_task_address_match(
    parser_type: str,
    row_key: str,
    data: AddressMatchConfirm,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """人工确认任务的小区归属；确认结果不会被后续规则重跑覆盖。"""
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    user = _require_task_edit_user(user)
    context = await _flow_context(conn, user)
    if not _can_assign_tasks(context):
        raise HTTPException(403, "只有组长及有权管理任务的上级岗位可以确认小区归属")
    scope_where, scope_params = _scope_where(
        context,
        "all" if context.get("admin_mode") else "community",
    )
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT projection.community, projection.source_count,
                       projection.conflict, address_match.original_address,
                       source.id, source.revision, source.row_hash, source.physical_row,
                       source.values_json
                FROM _online_source_projection AS projection
                LEFT JOIN _online_task_address_matches AS address_match
                  ON address_match.parser_type=projection.parser_type
                 AND address_match.row_key=projection.row_key
                JOIN _online_source_rows AS source
                  ON source.parser_type=projection.parser_type
                 AND source.row_key=projection.row_key
                 AND source.archived_at IS NULL
                WHERE projection.parser_type=%s AND projection.row_key=%s
                  AND source.spreadsheet_id=0
              AND source.source_kind IN ('local_table','local_dispatch','one_time_continuation_import')
                  AND {scope_where}
                FOR UPDATE
                """,
                (parser_type, row_key, *scope_params),
            )
            projection = await cur.fetchone()
            if not projection:
                raise HTTPException(404, "任务不存在或不在当前账号可操作范围内")
            if bool(projection[2]) or int(projection[1] or 0) != 1:
                raise HTTPException(409, "任务存在重复或冲突来源，暂不能确认小区")
            if int(projection[4] or 0) != int(data.source_id) or (
                int(projection[5] or 0) != int(data.expected_revision)
                or str(projection[6] or "") != str(data.expected_row_hash)
            ):
                raise HTTPException(409, "任务已发生变化，请刷新后重新确认小区")
            await cur.execute(
                """
                SELECT entry.id, entry.name, entry.community_id, community.name
                FROM _police_address_entries AS entry
                JOIN _communities AS community ON community.id=entry.community_id
                WHERE entry.id=%s AND entry.enabled=1
                """,
                (data.small_community_id,),
            )
            entry = await cur.fetchone()
            if not entry:
                raise HTTPException(404, "小区不存在、已停用或尚未设置所属社区")
            assignment_context = await inspector_option_context(
                cur,
                user,
                assignment_only=True,
            )
            aliases = assignment_context["community_aliases"]
            task_community = aliases.get(str(projection[0] or "").strip(), "")
            entry_community = aliases.get(str(entry[3] or "").strip(), "")
            if not task_community or not entry_community or task_community != entry_community:
                raise HTTPException(409, "所选小区与任务正式社区不一致，请先处理地址冲突")
            next_revision = int(projection[5]) + 1

            async def authorize(_command_cur, locked_source):
                if (
                    locked_source is None
                    or int(locked_source["revision"]) != int(projection[5])
                    or int(locked_source["physical_row"]) != int(projection[7])
                ):
                    raise HTTPException(409, "任务已发生变化，请刷新后重新确认小区")
                if not context.get("admin_mode"):
                    allowed = {
                        str(value).strip()
                        for value in context.get("community_values") or []
                        if str(value).strip()
                    }
                    if task_community not in allowed:
                        raise HTTPException(404, "任务不存在或不在当前账号可操作范围内")
                else:
                    allowed = {"*"}
                return TaskAuthorizationResult(
                    policy_key="online.address_match.confirm",
                    scope_values=tuple(sorted(allowed)),
                )

            async def mutate(command_cur, _locked_source):
                await record_feedback_confirmation(
                    command_cur,
                    parser_type=parser_type,
                    row_key=row_key,
                    address=str(projection[3] or ""),
                    community_name=str(entry[3] or ""),
                    community_id=int(entry[2]),
                    confirmed_entry_id=int(entry[0]),
                    confirmed_by=int(user["id"]),
                )
                await command_cur.execute(
                    """
                    INSERT INTO _online_task_address_matches (
                        parser_type, row_key, original_address,
                        suggested_entry_id, suggested_community_id,
                        suggested_community_name, match_status, match_score,
                        match_method, match_reason, candidates_json,
                        matcher_version, confirmed_entry_id, confirmed_by,
                        confirmed_at
                    )
                    SELECT parser_type, row_key, '', %s, %s, %s,
                           'confirmed', 1, '人工确认', '管理员已确认小区归属',
                           JSON_ARRAY(), %s, %s, %s, UTC_TIMESTAMP()
                    FROM _online_source_projection
                    WHERE parser_type=%s AND row_key=%s
                    ON DUPLICATE KEY UPDATE
                        suggested_entry_id=VALUES(suggested_entry_id),
                        suggested_community_id=VALUES(suggested_community_id),
                        suggested_community_name=VALUES(suggested_community_name),
                        match_status='confirmed', match_score=1,
                        match_method='人工确认',
                        match_reason='管理员已确认小区归属',
                        matcher_version=VALUES(matcher_version),
                        confirmed_entry_id=VALUES(confirmed_entry_id),
                        confirmed_by=VALUES(confirmed_by),
                        confirmed_at=VALUES(confirmed_at),
                        manual_unmatched_reason=NULL,
                        manual_unmatched_address_hmac=NULL,
                        manual_unmatched_by=NULL,
                        manual_unmatched_at=NULL
                    """,
                    (
                        int(entry[0]), int(entry[2]), str(entry[3] or ""),
                        MATCHER_VERSION, int(entry[0]), int(user["id"]),
                        parser_type, row_key,
                    ),
                )
                await command_cur.execute(
                    "UPDATE _online_source_rows SET revision=%s WHERE id=%s AND revision=%s",
                    (next_revision, int(projection[4]), int(projection[5])),
                )
                if command_cur.rowcount != 1:
                    raise HTTPException(409, "任务已发生变化，请刷新后重新确认小区")
                await command_cur.execute(
                    "UPDATE _local_source_records SET revision=%s,updated_at=UTC_TIMESTAMP() "
                    "WHERE parser_type=%s AND local_task_id=%s AND status='active' AND revision=%s",
                    (next_revision, parser_type, int(projection[7]), int(projection[5])),
                )
                if command_cur.rowcount != 1:
                    raise HTTPException(409, "本地任务来源已变化，请刷新后重新确认小区")
                from services.task_view_state import (
                    enqueue_source_task_view_state, task_view_state_events_enabled,
                )
                if task_view_state_events_enabled():
                    await enqueue_source_task_view_state(
                        command_cur, source_id=int(projection[4]), domain="address_match",
                        state={"status": "confirmed", "small_community_id": int(entry[0]),
                               "small_community_name": str(entry[1] or ""),
                               "community_id": int(entry[2]), "matcher_version": MATCHER_VERSION},
                        operation_id=f"address-confirm-{projection[4]}-{next_revision}",
                    )
                from services.business_time import get_business_date
                from services.online_summary_updates import enqueue_online_summary_update
                await enqueue_online_summary_update(
                    command_cur, task_id=int(projection[7]), parser_type=parser_type,
                    row_key=row_key, revision=next_revision,
                    business_date=await get_business_date(command_cur),
                    operation_id=f"address-confirm-{projection[4]}-{next_revision}",
                )
                await rebuild_projection_rows(command_cur, parser_type, [row_key])
                address_match = (await _address_matches_by_rows(
                    command_cur, parser_type, [row_key],
                )).get(row_key)
                return TaskMutationResult(
                    revision=next_revision,
                    snapshot={**json_value(projection[8], {}), "address_match": {
                        "status": "confirmed", "small_community_id": int(entry[0]),
                        "community_id": int(entry[2]),
                    }},
                    changed_fields=("small_community",),
                    result=address_match,
                )

            command = await TaskCommandService.execute(
                cur,
                spec=TaskCommandSpec(
                    actor_id=int(user["id"]), environment=settings.APP_ENVIRONMENT,
                    parser_type=parser_type,
                    task_id=stable_task_id(get_parser(parser_type).table_name, int(projection[7])),
                    source_id=int(projection[4]), action="address_confirm",
                    event_type="online.task.changed", expected_revision=int(projection[5]),
                    audiences=("authenticated",),
                ),
                authorize=authorize,
                mutate=mutate,
            )
            result = command.result
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "mobile_tasks.address_match_confirm",
        conn=conn,
        target_type="mobile_task_address_match",
        target_name=f"{parser_type}:{row_key}",
        detail={"small_community_id": int(entry[0])},
        **request_audit_fields(request),
    )
    return {"message": "小区归属已确认", "address_match": result}


@router.post("/{parser_type}/{row_key}/address-match/resolve-conflict")
async def resolve_mobile_task_address_conflict(
    parser_type: str,
    row_key: str,
    data: AddressMatchConflictResolution,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """提供地址冲突的可执行处理路径，并在修正后重新生成匹配结果。"""
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    if not local_data_source_enabled():
        raise HTTPException(409, "本地业务数据源尚未启用，暂不能处理地址冲突")
    scoped_user = _require_unverifiable_reviewer(user)
    context = await _flow_context(conn, scoped_user)
    scope_where, scope_params = _scope_where(
        context,
        "all" if context.get("admin_mode") else "community",
    )
    parser = get_parser(parser_type)
    community_field = parser.COMMUNITY_COLUMN
    async with conn.cursor() as cur:
        await cur.execute(
            f"""
            SELECT source.id, source.row_key, source.revision, source.row_hash,
                   source.values_json, projection.address_match_status,
                   projection.address_match_candidates, projection.community,
                   projection.source_count, projection.conflict
            FROM _online_source_rows AS source
            JOIN _online_source_projection AS projection
              ON projection.parser_type=source.parser_type
             AND projection.row_key=source.row_key
            WHERE source.id=(
                SELECT candidate_source.id FROM _online_source_rows AS candidate_source
                WHERE candidate_source.parser_type=%s
                  AND candidate_source.row_key=%s
                  AND candidate_source.archived_at IS NULL
                {active_source_sql_filter(parser_type, 'candidate_source')}
                ORDER BY id LIMIT 1
            )
              AND source.parser_type=%s
              AND source.archived_at IS NULL
              AND {scope_where}
            """,
            (parser_type, row_key, parser_type, *scope_params),
        )
        source = await cur.fetchone()
        if not source:
            raise HTTPException(404, "任务不存在或不在当前账号可处理范围内")
        if str(source[1] or "") != row_key:
            raise HTTPException(409, "任务主键已变化，请刷新后重试")
        _validate_address_source_identity(source[0], data.source_id)
        if int(source[2] or 0) != data.expected_revision or str(source[3] or "") != data.expected_row_hash:
            raise HTTPException(409, "任务已被其他人更新，请刷新后重试")
        if int(source[8] or 0) != 1 or bool(source[9]):
            raise HTTPException(409, "任务存在重复或冲突来源，请先处理来源异常")
        if str(source[5] or "") != "conflict":
            raise HTTPException(409, "该任务当前已不处于地址冲突状态，请刷新后重试")
        candidates = json_value(source[6], [])
        selected = next(
            (item for item in candidates if int(item.get("entry_id") or 0) == data.small_community_id),
            None,
        )
        if not selected:
            raise HTTPException(400, "所选小区不是本次冲突匹配生成的候选，请刷新后重新选择")
        await cur.execute(
            """
            SELECT entry.id, entry.name, entry.community_id, community.name
            FROM _police_address_entries AS entry
            JOIN _communities AS community ON community.id=entry.community_id
            WHERE entry.id=%s AND entry.enabled=1 AND community.is_active=1
            """,
            (data.small_community_id,),
        )
        entry = await cur.fetchone()
        if not entry:
            raise HTTPException(404, "候选小区不存在、已停用或所属社区已停用")
        target_community = str(entry[3] or "").strip()
        if not target_community:
            raise HTTPException(409, "候选小区尚未设置有效所属社区，请先维护小区地址库")
        current_values = json_value(source[4], {})

    result = await queue_source_fields(
        parser_type=parser_type,
        source_id=int(source[0]),
        changes={community_field: target_community},
        base_values={community_field: str(current_values.get(community_field) or "")},
        expected_revision=data.expected_revision,
        request=request,
        user=scoped_user,
        conn=conn,
        explicit_text_edit=True,
        allowed_columns={community_field},
        audit_action="address_match.resolve_conflict",
        record_unverifiable_save=False,
    )
    await record_admin_audit(
        scoped_user,
        "mobile_tasks.address_match.resolve_conflict",
        target_type="mobile_task_address_match",
        target_name=f"{parser_type}:{row_key}",
        detail={
            "small_community_id": int(entry[0]),
            "target_community": target_community,
        },
        **request_audit_fields(request),
    )
    return {
        **result,
        "message": f"已将任务社区修正为{target_community}，正在重新匹配",
        **await _mobile_task_save_context(conn, parser_type, str(result["row_key"])),
    }


@router.get("/{parser_type}/assignment-workbench")
async def get_mobile_task_assignment_workbench(
    parser_type: str,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Return an indexed, address-sorted queue of unassigned tasks.

    The legacy expression ``ORDER BY {_address_order(parser_type)}`` is kept
    out of the SQL path; the projection now stores its normalized sort key.
    """
    started_at = time.monotonic()
    if settings.TASK_READ_MODEL_ENABLED:
        return await _get_mobile_task_assignment_workbench_from_read_model(
            parser_type, user, conn, started_at=started_at,
        )
    _require_legacy_read_model_disabled()
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    user = _require_task_edit_user(user)
    context = await _flow_context(conn, user)
    if not _can_assign_tasks(context):
        raise HTTPException(403, "只有组长及有权管理任务的上级岗位可以批量分配核查人")
    scope_where, scope_params = _scope_where(
        context,
        "all" if context.get("admin_mode") else "community",
    )
    assignment_source_condition = "projection.assignment_queue_ready=1"
    async with conn.cursor() as cur:
        if not local_data_source_enabled() and not await _writeback_enabled(cur):
            raise HTTPException(503, "在线回写已由超级管理员暂停")
        await cur.execute(
            f"""
            SELECT COUNT(*)
            FROM _online_source_projection AS projection
            WHERE projection.parser_type=%s
              AND {scope_where}
              AND {assignment_source_condition}
            """,
            [parser_type, *scope_params],
        )
        available_total = int((await cur.fetchone())[0] or 0)
        await cur.execute(
            f"""
            SELECT projection.row_key, projection.community,
                   projection.small_community_name, projection.address_match_status,
                   projection.assignment_source_label,
                   projection.assignment_address_display
            FROM _online_source_projection AS projection
            WHERE projection.parser_type=%s
              AND {scope_where}
              AND {assignment_source_condition}
            ORDER BY projection.assignment_address_sort_key, projection.row_key
            LIMIT %s
            """,
            [parser_type, *scope_params, MAX_BULK_ASSIGNMENT_TASKS],
        )
        rows = await cur.fetchall()
        assignment_context = await inspector_option_context(
            cur,
            user,
            assignment_only=True,
        )
        await cur.execute(
            f"""
            SELECT projection.community, projection.inspector, COUNT(*)
            FROM _online_source_projection AS projection
            WHERE projection.parser_type=%s
              AND {scope_where}
              AND TRIM(COALESCE(projection.inspector, ''))<>''
              AND projection.task_state<>'completed'
              AND projection.source_count=1
              AND projection.conflict=0
            GROUP BY projection.community, projection.inspector
            """,
            [parser_type, *scope_params],
        )
        assigned_count_rows = await cur.fetchall()

    aliases = assignment_context["community_aliases"]
    inspectors_by_community = assignment_context["inspectors_by_community"]
    items: list[dict[str, str]] = []
    for row_key, raw_community, raw_small_community, raw_match_status, raw_source, raw_address in rows:
        community = aliases.get(str(raw_community or "").strip(), "")
        if not community:
            community = str(raw_community or "").strip() or "社区未识别"
        items.append({
            "row_key": str(row_key),
            "community": community,
            "small_community": str(raw_small_community or ""),
            "match_status": str(raw_match_status or "unmatched"),
            "source": str(raw_source or TASK_WORKFLOWS[parser_type].label),
            "address": str(raw_address or "未填写地址"),
        })
    community_counts: dict[str, int] = {}
    for item in items:
        community_counts[item["community"]] = community_counts.get(item["community"], 0) + 1
    inspector_counts_by_community: dict[str, dict[str, int]] = {}
    for raw_community, raw_inspector, count in assigned_count_rows:
        formal_community = aliases.get(str(raw_community or "").strip(), "")
        if not formal_community:
            formal_community = str(raw_community or "").strip() or "社区未识别"
        inspector_name = str(raw_inspector or "").strip()
        if not inspector_name:
            continue
        inspector_counts_by_community.setdefault(formal_community, {})[inspector_name] = int(count or 0)
    assigned_totals_by_community: dict[str, int] = {}
    for formal_community, counts in inspector_counts_by_community.items():
        assigned_totals_by_community[formal_community] = sum(counts.values())
    all_communities = set(community_counts) | set(assigned_totals_by_community)
    return {
        "data": items,
        "total": available_total,
        "displayed_total": len(items),
        "limited": available_total > len(items),
        "limit": MAX_BULK_ASSIGNMENT_TASKS,
        "communities": [
            {"value": community, "label": community, "count": community_counts.get(community, 0),
             "assigned_count": assigned_totals_by_community.get(community, 0)}
            for community in sorted(all_communities)
        ],
        "inspectors_by_community": {
            community: list(inspectors_by_community.get(community) or [])
            for community in all_communities
        },
        "inspector_counts_by_community": {
            community: {
                inspector: inspector_counts_by_community.get(community, {}).get(inspector, 0)
                for inspector in inspectors_by_community.get(community) or []
            }
            for community in all_communities
        },
        "assigned_totals_by_community": assigned_totals_by_community,
        "duration_ms": max(0, int((time.monotonic() - started_at) * 1000)),
        "query_mode": "indexed_projection",
    }


async def _get_mobile_task_assignment_workbench_from_read_model(
    parser_type: str,
    user: dict,
    conn,
    *,
    started_at: float,
) -> dict:
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    user = _require_task_edit_user(user)
    context = await _flow_context(conn, user)
    if not _can_assign_tasks(context):
        raise HTTPException(403, "只有组长及有权管理任务的上级岗位可以批量分配核查人")
    scope: FlowScope = "all" if context.get("admin_mode") else "community"
    workflow = TASK_WORKFLOWS[parser_type]
    parser = get_parser(parser_type)
    async with conn.cursor() as cur:
        assignment_context = await inspector_option_context(
            cur, user, assignment_only=True,
        )
    aliases = assignment_context["community_aliases"]
    inspectors_by_community = assignment_context["inspectors_by_community"]
    items: list[dict[str, str]] = []
    community_counts: dict[str, int] = {}
    available_total = 0
    displayed: list[dict] = []
    assigned_count_rows: dict[str, dict[str, int]] = {}
    async for group_page in _iter_assignment_read_model_groups(
        parser_type, conn, context, scope=scope,
    ):
        for group in group_page:
            record = {
                **group["canonical_source"],
                "values": group["values"], "row_key": group["row_key"],
                "source_count": group["source_count"], "conflict": group["conflict"],
                "sources": group["sources"],
            }
            values = record["values"]
            community = parser.community_value(values).strip()
            if not community or record["source_count"] != 1 or record["conflict"]:
                continue
            assigned = str(values.get("核查人") or "").strip()
            state = workflow.state(values)
            if assigned:
                if state != "completed":
                    formal = aliases.get(community, "") or community
                    counts = assigned_count_rows.setdefault(formal, {})
                    counts[assigned] = counts.get(assigned, 0) + 1
                continue
            if state == "completed":
                continue
            available_total += 1
            if len(displayed) < MAX_BULK_ASSIGNMENT_TASKS:
                summary = workflow.summary(values)
                address = str(summary.get("address") or "未填写地址").strip()
                displayed.append({
                    **record,
                    "community_raw": community,
                    "address": address,
                    "source": str(summary.get("source") or workflow.label).strip(),
                })
    states_by_task = await _read_task_view_states_for_records(displayed)
    for record in displayed:
        raw_community = record["community_raw"]
        community = aliases.get(raw_community, "") or raw_community or "社区未识别"
        community_counts[community] = community_counts.get(community, 0) + 1
        address_state = states_by_task[str(record["task_id"])]["address_match"]["state"]
        candidate = address_state.get("candidate") or {}
        items.append({
            "row_key": str(record["row_key"]),
            "community": community,
            "small_community": str(candidate.get("name") or candidate.get("community_name") or ""),
            "match_status": str(address_state.get("status") or "unmatched"),
            "source": record["source"],
            "address": record["address"],
        })
    assigned_totals = {
        community: sum(counts.values())
        for community, counts in assigned_count_rows.items()
    }
    all_communities = set(community_counts) | set(assigned_totals)
    return {
        "data": items,
        "total": available_total,
        "displayed_total": len(items),
        "limited": available_total > len(items),
        "limit": MAX_BULK_ASSIGNMENT_TASKS,
        "communities": [
            {"value": community, "label": community,
             "count": community_counts.get(community, 0),
             "assigned_count": assigned_totals.get(community, 0)}
            for community in sorted(all_communities)
        ],
        "inspectors_by_community": {
            community: list(inspectors_by_community.get(community) or [])
            for community in all_communities
        },
        "inspector_counts_by_community": {
            community: {
                inspector: assigned_count_rows.get(community, {}).get(inspector, 0)
                for inspector in inspectors_by_community.get(community) or []
            }
            for community in all_communities
        },
        "assigned_totals_by_community": assigned_totals,
        "duration_ms": max(0, int((time.monotonic() - started_at) * 1000)),
        "query_mode": "redis_read_model",
    }


@router.get("/{parser_type}")
async def list_mobile_tasks(
    parser_type: str,
    scope: FlowScope = Query("mine"),
    status: TaskStatus = Query("pending"),
    review_stage: ReviewStage = Query("all"),
    community: list[str] = Query(default=[]),
    small_community: list[str] = Query(default=[]),
    match_status: list[str] = Query(default=[]),
    inspector: list[str] = Query(default=[]),
    watch_category: list[int] = Query(default=[]),
    priority: Priority = Query("all"),
    sort: SortMode = Query("priority"),
    keyword: str | None = Query(None, max_length=100),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=50),
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    return await _list_mobile_tasks_data(
        parser_type,
        TaskSearch(
            scope=scope,
            status=status,
            review_stage=review_stage,
            communities=community,
            small_communities=small_community,
            match_status=match_status,
            inspectors=inspector,
            watch_categories=watch_category,
            priority=priority,
            sort=sort,
            keyword=keyword or "",
            page=page,
            page_size=page_size,
        ),
        user,
        conn,
    )


async def _mobile_task_detail_data(
    parser_type: str,
    row_key: str,
    user: dict,
    conn,
    *,
    analysis_mode: bool = False,
) -> dict:
    if analysis_mode:
        user = (
            _require_unverifiable_reviewer(user)
            if supports_unverifiable_review(parser_type)
            else _require_analysis_user(user)
        )
    capability_user = _task_capability_user(user)
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    context = await _flow_context(conn, user)
    parser = get_parser(parser_type)
    dependency_blocked = False
    detail_scope: FlowScope = "all" if context["admin_mode"] else "community"
    scope_where, scope_params = _scope_where(context, detail_scope)
    async with conn.cursor() as cur:
        if settings.TASK_READ_MODEL_ENABLED:
            await cur.execute(
                f"""
                SELECT id, physical_row, revision, row_hash,
                       spreadsheet_id, sheet_id
                FROM _online_source_rows AS source
                WHERE source.parser_type=%s AND source.row_key=%s
                  AND source.archived_at IS NULL
                  {active_source_sql_filter(parser_type, 'source')}
                ORDER BY spreadsheet_id, physical_row
                """,
                (parser_type, row_key),
            )
            locator_rows = await cur.fetchall()
            if not locator_rows:
                raise HTTPException(404, "任务不存在或不属于当前社区")
            snapshot_rows = await _read_task_snapshots_for_source_rows(
                parser_type,
                [(int(row[0]), int(row[1]), int(row[2] or 0)) for row in locator_rows],
            )
            if len(snapshot_rows) != len(locator_rows):
                raise HTTPException(503, {"error": "projection_pending"})
            visible_sources = [
                row for row in locator_rows
                if _source_in_community(
                    parser, snapshot_rows[int(row[0])], context["community_values"],
                )
            ]
            if not visible_sources:
                raise HTTPException(404, "任务不存在或不属于当前社区")
            raw_sources = [
                (
                    int(row[0]), int(row[1]), None, None, int(row[2] or 0),
                    str(row[3] or ""), int(row[4] or 0), str(row[5] or ""),
                )
                for row in visible_sources
            ]
            read_model_values = {
                int(row[0]): snapshot_rows[int(row[0])] for row in visible_sources
            }
            visible_values = list(read_model_values.values())
            first_values = visible_values[0]
            conflict = len(locator_rows) != len(visible_values) or any(
                stable_json(values) != stable_json(first_values)
                for values in visible_values[1:]
            )
            parent_row = (
                None, len(locator_rows), int(conflict), "",
                TASK_WORKFLOWS[parser_type].state(first_values),
            )
            parent_values = dict(first_values)
        else:
            if not await _task_source_ready(cur, parser_type):
                raise HTTPException(
                    409,
                    "本地任务来源尚未建立"
                    if local_data_source_enabled()
                    else "来源定位尚未建立，请等待一次正常同步",
                )
            await cur.execute(
                f"""
                SELECT values_json, source_count, conflict, pending_state, task_state
                FROM _online_source_projection AS projection
                WHERE projection.parser_type=%s AND projection.row_key=%s
                  AND {scope_where}
                """,
                (parser_type, row_key, *scope_params),
            )
            parent_row = await cur.fetchone()
            if not parent_row:
                raise HTTPException(404, "任务不存在或不属于当前社区")
            parent_values = json_value(parent_row[0], {})
            await cur.execute(
                f"""
                SELECT id, physical_row, values_json, cell_meta_json,
                       revision, row_hash, spreadsheet_id, sheet_id
                FROM _online_source_rows AS source
                WHERE source.parser_type=%s AND source.row_key=%s
                  AND source.archived_at IS NULL
                  {active_source_sql_filter(parser_type, 'source')}
                ORDER BY spreadsheet_id, physical_row
                """,
                (parser_type, row_key),
            )
            raw_sources = await cur.fetchall()
            read_model_values: dict[int, dict] = {}
        domain_states: dict = {}
        if settings.TASK_READ_MODEL_ENABLED:
            state_tasks = [
                {"task_id": stable_task_id(parser.table_name, int(row[1])),
                 "revision": int(row[4] or 0)}
                for row in raw_sources
            ]
            all_states = await _read_task_view_states_for_records(state_tasks)
            if state_tasks:
                domain_states = all_states[state_tasks[0]["task_id"]]
            review_state = (domain_states.get("review") or {}).get("state", {})
            review_flow = (
                {**review_state, "id": int(review_state["flow_id"])}
                if review_state.get("flow_id") else None
            )
            if review_flow:
                review_flow["events"] = await review_events_for_flow(
                    cur, int(review_flow["id"]),
                )
        else:
            review_by_row = await review_flows_by_rows(cur, [(parser_type, row_key)])
            review_flow = review_by_row.get((parser_type, row_key))
            if review_flow:
                review_flow["events"] = await review_events_for_flow(cur, int(review_flow["id"]))
        if not analysis_mode:
            if settings.TASK_READ_MODEL_ENABLED:
                graph_state = (domain_states.get("task_graph") or {}).get("state")
                if not isinstance(graph_state, dict):
                    raise HTTPException(503, {"error": "projection_pending"})
                dependency_blocked = graph_state.get("blocked") is True
            else:
                dependency_blocked = await online_task_blocked(cur, parser_type, row_key)
        if analysis_mode and not (
            review_flow and review_flow.get("state") in {
                INITIAL_PENDING, INITIAL_EXTENSION, DEEP_PENDING, DEEP_EXTENSION,
            }
        ) and TASK_WORKFLOWS[parser_type].review_stage(parent_values) not in {
            "waiting_analysis", "analyzed"
        }:
            raise HTTPException(404, "该任务当前不属于网格核查研判范围")
        local_changes = (
            {}
            if settings.TASK_READ_MODEL_ENABLED
            else await load_local_changes(cur, [int(row[0]) for row in raw_sources])
        )
        enabled = local_data_source_enabled() or await _writeback_enabled(cur)
        assignment_context = (
            await inspector_option_context(cur, capability_user, assignment_only=True)
            if _can_assign_tasks(context) and (
                has_permission(user, ONLINE_RAW_EDIT)
                or has_permission(user, ONLINE_TASK_MANAGE)
            )
            else None
        )
        if settings.TASK_READ_MODEL_ENABLED:
            watch_by_row = {
                row_key: (domain_states.get("watch") or {}).get("state", {})
            }
            qmf_by_row = {
                row_key: (domain_states.get("qmf") or {}).get("state", {})
            }
            residence_by_row = {
                row_key: (domain_states.get("residence") or {}).get("state", {})
            }
            registration_by_row = {
                row_key: (domain_states.get("registration") or {}).get("state", {})
            }
            address_state = (domain_states.get("address_match") or {}).get("state", {})
            address_candidate = address_state.get("candidate") or {}
            address_match = {
                **address_state,
                "small_community_id": address_candidate.get("entry_id"),
                "small_community_name": address_candidate.get("name") or address_candidate.get("community_name", ""),
            }
        else:
            watch_by_row = await task_watch_payload(
                cur, parser_type, [row_key],
                task_values_by_row={row_key: parent_values},
            ) if settings.REGISTRY_FEATURE_ENABLED else {}
            qmf_by_row = await _qmf_status_by_rows(
                cur, parser_type, [(row_key, stable_json(parent_values))],
            )
            residence_by_row = await residence_status_by_rows(
                cur, parser_type, [(row_key, stable_json(parent_values))],
            )
            registration_by_row = await registration_links_by_rows(cur, parser_type, [row_key])
            address_match = (await _address_matches_by_rows(cur, parser_type, [row_key])).get(row_key)
        registration_link = registration_by_row.get(row_key)

        sources = []
        for (
            source_id,
            physical_row,
            raw_values,
            raw_meta,
            revision,
            row_hash,
            spreadsheet_id,
            sheet_id,
        ) in raw_sources:
            source_changes = local_changes.get(int(source_id), [])
            values = (
                read_model_values[int(source_id)]
                if settings.TASK_READ_MODEL_ENABLED
                else overlay_local_values(json_value(raw_values, {}), source_changes)
            )
            # 同一业务主键偶尔会跨社区重复。父投影可以用于定位任务，
            # 但详情绝不能因此暴露其他社区的腾讯原始行。
            if not _source_in_community(
                parser, values, context["community_values"]
            ):
                continue
            capabilities = await row_edit_capabilities(
                cur, capability_user, parser, values
            )
            metadata = await _managed_column_metadata(
                cur,
                parser,
                json_value(raw_meta, {}),
                spreadsheet_id=int(spreadsheet_id),
                sheet_id=str(sheet_id),
            )
            workflow = TASK_WORKFLOWS[parser_type]
            if is_registration_task(parser_type) and workflow.result_field in parser.COLUMNS:
                metadata[workflow.result_field] = {
                    "type": "select",
                    "multiple": False,
                    "options": [
                        {"id": option, "text": option}
                        for option in workflow.result_options
                        if option != "已登记"
                    ],
                }
            raw_community = parser.community_value(values)
            formal_community = (
                (assignment_context or {}).get("community_aliases", {})
                .get(str(raw_community or "").strip(), "")
            )
            assignee_options = list(
                (assignment_context or {}).get("inspectors_by_community", {})
                .get(formal_community, [])
            )
            if "核查人" in metadata and assignee_options:
                metadata["核查人"] = {
                    "type": "select",
                    "multiple": False,
                    "options": [
                        {"id": name, "text": name}
                        for name in assignee_options
                    ],
                }
            source_task = _task_record(
                parser_type,
                row_key,
                values,
                1,
                False,
                bool(str(parent_row[3] or "")),
                TASK_WORKFLOWS[parser_type].state(
                    values,
                    registration_status=str(
                        (registration_link or {}).get("status") or ""
                    ),
                ),
                review_flow=review_flow,
                address_match=address_match,
            )
            editable_fields = (
                list(capabilities["editable_fields"])
                if enabled else []
            )
            if context["position"] != "组长" and not context["admin_mode"]:
                editable_fields = [
                    field for field in editable_fields if field != "核查人"
                ]
            # 二次反馈是否真正允许保存，仍由批量校验根据提交后的主结果
            # 决定；这里先把字段交给手机表单，才能在同一次提交中展开。
            if enabled and capabilities["can_edit"]:
                editable_fields.extend(
                    field for field in TASK_WORKFLOWS[parser_type].secondary_fields
                    if field in parser.COLUMNS
                )
            analysis_fields = set(TASK_WORKFLOWS[parser_type].analysis_fields)
            if analysis_mode:
                if supports_unverifiable_review(parser_type):
                    # 两级研判只能通过带流程版本、来源版本和行哈希的
                    # 结构化决定接口提交，禁止旧客户端继续自由编辑“研判”列。
                    editable_fields = []
                else:
                    editable_fields = [
                        field for field in analysis_fields
                        if field in parser.COLUMNS
                    ] if source_task["review_stage"] in {
                        "waiting_analysis", "analyzed",
                    } else []
            else:
                editable_fields = [
                    field for field in editable_fields
                    if field not in analysis_fields
                ]
            sources.append({
                "id": int(source_id),
                "row_key": row_key,
                "physical_row": int(physical_row),
                "source_available": int(physical_row) > 0,
                "values": {
                    column: str(values.get(column, "") or "")
                    for column in parser.COLUMNS
                },
                "cell_meta": metadata,
                "revision": int(revision),
                "row_hash": str(row_hash),
                "editable_fields": list(dict.fromkeys(editable_fields)),
                "state": source_task["state"],
                "needs_review": source_task["needs_review"],
                "review_stage": source_task["review_stage"],
                "review_flow": review_flow,
                "sync_state": local_sync_state(source_changes),
                "sync_fields": [
                    {
                        "field": item["field_name"],
                        "platform_value": item["local_value"],
                        "tencent_value": item["remote_value"],
                        "status": item["status"],
                        "error_code": item["error_code"],
                    }
                    for item in source_changes
                ],
            })

        if not sources:
            raise HTTPException(404, "任务不存在或不属于当前社区")

    workflow = TASK_WORKFLOWS[parser_type]
    qmf_config = await load_qmf_config(conn)
    qmf_allowed = has_permission(user, QMF_REGISTRATION_EXECUTE)
    effective_source_count = int(parent_row[1] or 0)
    effective_conflict = bool(parent_row[2])
    qmf_preview = preview_capability(
        allowed=qmf_allowed,
        parser_type=parser_type,
        source_count=effective_source_count,
        conflict=effective_conflict,
        values=sources[0]["values"] if effective_source_count == 1 else None,
        config=qmf_config,
    )
    qmf_registration = registration_capability(
        allowed=qmf_allowed,
        parser_type=parser_type,
        source_count=effective_source_count,
        conflict=effective_conflict,
        values=sources[0]["values"] if effective_source_count == 1 else None,
        config=qmf_config,
    )
    latest_qmf_run, qmf_feedback = await _qmf_registration_state(
        conn,
        parser_type=parser_type,
        sources=sources,
        user=user,
    )
    qmf_registration["latest_run"] = latest_qmf_run
    return {
        "task": _task_record(
            parser_type,
            row_key,
            parent_values,
            effective_source_count,
            effective_conflict,
            bool(str(parent_row[3] or "")),
            str(parent_row[4] or ""),
            watch_by_row.get(row_key),
            str(parent_row[3] or ""),
            qmf_status=(
                qmf_by_row.get(row_key)
                if str(parent_row[4] or "") == "completed"
                else None
            ),
            residence_status=residence_by_row.get(row_key),
            registration_link=registration_link,
            review_flow=review_flow,
            address_match=address_match,
        ),
        "workflow": {
            "label": workflow.label,
            "result_field": workflow.result_field,
            # Keep the canonical field first while allowing historical source
            # aliases in the detail page to match the list summary.
            "phone_fields": list(dict.fromkeys((*workflow.phone_fields, *PHONE_FIELD_ALIASES))),
            "title_fields": list(workflow.title_fields),
            "address_fields": list(workflow.address_fields),
            "date_fields": list(workflow.date_fields),
            "identity_fields": list(workflow.identity_fields),
            "source_fields": list(workflow.source_fields),
            "secondary_fields": list(workflow.secondary_fields),
            "extra_edit_fields": list(getattr(parser, "MOBILE_EDITABLE_FIELDS", ())),
            "analysis_fields": list(workflow.analysis_fields),
            "columns": parser.COLUMNS,
        },
        "writeback_enabled": enabled,
        "analysis_mode": analysis_mode,
        "dependency_blocked": dependency_blocked,
        "dependency_message": (
            "该任务已进入基础管控研判队列，网格员仍可继续核查并修改结果"
            if dependency_blocked else ""
        ),
        "qmf_preview": qmf_preview,
        "qmf_registration": qmf_registration,
        "qmf_feedback": qmf_feedback,
        "qmf_status": qmf_by_row.get(row_key),
        "residence_status": residence_by_row.get(row_key),
        "registration_link": registration_link,
        "address_match": address_match,
        "review_flow": review_flow,
        "registration_manual_confirm_allowed": can_confirm_registration(user),
        "sources": sources,
        "data_source_mode": "local" if local_data_source_enabled() else "tencent",
    }


async def _mobile_task_inline_editors_data(
    parser_type: str,
    row_keys: list[str],
    user: dict,
    conn,
    *,
    analysis_mode: bool = False,
) -> dict:
    """Prepare the current page's editors in one browser request.

    The detail permission, community scope and editable-field calculation stay
    identical to the normal task detail endpoint.  A row that moved out of the
    current scope is returned as unavailable instead of failing the whole page.
    """
    normalized_keys = list(dict.fromkeys(
        str(row_key).strip() for row_key in row_keys if str(row_key).strip()
    ))
    if not normalized_keys:
        raise HTTPException(400, "请至少选择一条任务")

    items: dict[str, dict] = {}
    for row_key in normalized_keys:
        try:
            detail = await _mobile_task_detail_data(
                parser_type,
                row_key,
                user,
                conn,
                analysis_mode=analysis_mode,
            )
        except HTTPException as exc:
            if exc.status_code not in {404, 409}:
                raise
            items[row_key] = {
                "available": False,
                "reason": str(exc.detail),
            }
            continue
        if (
            len(detail["sources"]) != 1
            or int(detail["task"].get("source_count") or 0) != 1
            or bool(detail["task"].get("conflict"))
        ):
            items[row_key] = {
                "available": False,
                "reason": (
                    "该任务包含多个本地来源，请进入详情处理"
                    if local_data_source_enabled()
                    else "该任务包含多个腾讯来源，请进入详情选择来源后修改"
                ),
            }
            continue
        items[row_key] = {
            "available": True,
            "detail": detail,
        }
    return {"items": items, "analysis_mode": analysis_mode}


@router.post("/analysis/{parser_type}/inline-editors")
async def get_mobile_task_analysis_inline_editors(
    parser_type: str,
    data: InlineEditorRequest,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    return await _mobile_task_inline_editors_data(
        parser_type,
        data.row_keys,
        user,
        conn,
        analysis_mode=True,
    )


@router.post("/{parser_type}/inline-editors")
async def get_mobile_task_inline_editors(
    parser_type: str,
    data: InlineEditorRequest,
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    return await _mobile_task_inline_editors_data(
        parser_type,
        data.row_keys,
        user,
        conn,
    )


@router.get("/analysis/{parser_type}/{row_key}")
async def get_mobile_task_analysis_detail(
    parser_type: str,
    row_key: str,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    return await _mobile_task_detail_data(
        parser_type, row_key, user, conn, analysis_mode=True
    )


@router.patch("/analysis/{parser_type}/source-rows/{source_id}/decision")
async def decide_mobile_task_unverifiable_review(
    parser_type: str,
    source_id: int,
    data: UnverifiableDecision,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """提交结构化初步/深度研判决定，并原子写入延时截止日期。"""
    if not supports_unverifiable_review(parser_type):
        raise HTTPException(400, "该业务不支持无法核实两级研判")
    scoped_user = _require_unverifiable_reviewer(user)
    workflow = TASK_WORKFLOWS[parser_type]
    analysis_field = next(
        (field for field in workflow.analysis_fields if field), ""
    )
    secondary_field = next(
        (field for field in workflow.secondary_fields if field), ""
    )
    deadline_field = workflow.date_fields[0] if workflow.date_fields else ""
    if not analysis_field:
        raise HTTPException(400, "该业务没有可填写的研判字段")
    prepared_holder: dict = {}

    def validate_source(current: dict) -> None:
        if not is_unverifiable_result(current.get(workflow.result_field)):
            raise HTTPException(409, "该任务当前已不是无法核实，请刷新后重试")

    async def transaction_prepare(*, cur, source, current_values, changes):
        del changes
        if int(source.get("revision") or 0) != data.expected_revision:
            raise HTTPException(409, "腾讯来源版本已经变化，请刷新后重试")
        prepared = await prepare_decision(
            cur,
            parser_type=parser_type,
            source=source,
            current_values=current_values,
            stage=data.stage,
            outcome=data.outcome,
            opinion=data.opinion,
            expected_flow_version=data.flow_version,
            expected_row_hash=data.expected_row_hash,
        )
        prepared_holder["value"] = prepared
        result = {analysis_field: prepared["summary"]}
        if prepared["due_date"] and deadline_field:
            result[deadline_field] = prepared["due_date"].isoformat()
        if prepared["next_state"] in {
            INITIAL_EXTENSION, DEEP_PENDING, DEEP_EXTENSION,
        } and secondary_field:
            # 腾讯字段只展示当前阶段的反馈；上一阶段原文已由结构化事件保留。
            result[secondary_field] = ""
        return result

    async def transaction_callback(*, cur, source, before, after, row_key_before, row_key_after, revision):
        del before, after, row_key_before, row_key_after
        prepared = prepared_holder.get("value")
        if not prepared:
            raise HTTPException(409, "研判决定未准备完成，请刷新后重试")
        prepared_holder["applied"] = await apply_decision(
            cur,
            prepared=prepared,
            source=source,
            revision=revision,
            actor_user_id=int(scoped_user.get("id") or 0),
        )

    result = await queue_source_fields(
        parser_type=parser_type,
        source_id=source_id,
        changes={analysis_field: data.opinion.strip()},
        base_values={analysis_field: ""},
        expected_revision=data.expected_revision,
        request=request,
        user=scoped_user,
        conn=conn,
        explicit_text_edit=True,
        allowed_columns={
            field for field in (analysis_field, deadline_field, secondary_field)
            if field
        },
        current_values_validator=validate_source,
        redact_audit_values=True,
        audit_action="unverifiable_review_decision",
        transaction_prepare=transaction_prepare,
        transaction_callback=transaction_callback,
        record_unverifiable_save=False,
    )
    return {**result, "review_flow": prepared_holder.get("applied")}


@router.patch("/analysis/{parser_type}/source-rows/{source_id}")
async def update_mobile_task_analysis(
    parser_type: str,
    source_id: int,
    data: TaskBatchUpdate,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    if supports_unverifiable_review(parser_type):
        raise HTTPException(410, "无法核实任务请使用结构化研判决定接口")
    scoped_user = _require_analysis_user(user)
    parser = get_parser(parser_type)
    analysis_fields = {
        field for field in TASK_WORKFLOWS[parser_type].analysis_fields
        if field in parser.COLUMNS
    }
    if not analysis_fields:
        raise HTTPException(400, "该业务没有可填写的研判字段")
    if not data.changes or any(field not in analysis_fields for field in data.changes):
        raise HTTPException(400, "研判入口只能修改研判字段")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT values_json FROM _online_source_rows "
            "WHERE id=%s AND parser_type=%s",
            (source_id, parser_type),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "腾讯来源行不存在")
        values = json_value(row[0], {})
        if TASK_WORKFLOWS[parser_type].review_stage(values) not in {
            "waiting_analysis", "analyzed"
        }:
            raise HTTPException(409, "该任务当前不属于网格核查研判范围")
    def validate_analysis_source(current: dict) -> None:
        if TASK_WORKFLOWS[parser_type].review_stage(current) not in {
            "waiting_analysis", "analyzed"
        }:
            raise HTTPException(409, "该任务当前不属于网格核查研判范围")

    return await queue_source_fields(
        parser_type=parser_type,
        source_id=source_id,
        changes=data.changes,
        base_values=data.base_values,
        expected_revision=data.expected_revision,
        request=request,
        user=scoped_user,
        conn=conn,
        explicit_text_edit=True,
        allowed_columns=analysis_fields,
        current_values_validator=validate_analysis_source,
        redact_audit_values=True,
    )


@router.get("/{parser_type}/{row_key}/residence-detail")
async def get_mobile_task_residence_detail(
    parser_type: str,
    row_key: str,
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    detail = await _mobile_task_detail_data(
        parser_type,
        row_key,
        user,
        conn,
    )
    residence_status = detail.get("residence_status") or {}
    if residence_status.get("state") != "registered":
        raise HTTPException(409, "该任务尚无可展示的居住证登记资料")
    sources = detail.get("sources") or []
    if detail["task"].get("conflict") or len(sources) != 1:
        raise HTTPException(409, "该任务来源不唯一，暂不能读取居住证人员资料")
    source = sources[0]
    if not source.get("source_available"):
        raise HTTPException(409, "该任务来源已变化，请刷新后重试")
    try:
        return await residence_detail_for_values(
            conn,
            parser_type,
            source.get("values") or {},
        )
    except ResidencePlatformError as exc:
        status_code = 409 if exc.code in {
            "detail_not_registered",
            "invalid_identity",
            "community_missing",
            "community_not_found",
            "community_ambiguous",
            "community_code_missing",
            "session_not_ready",
        } else 502
        raise HTTPException(status_code, str(exc)) from exc


@router.get("/{parser_type}/{row_key}")
async def get_mobile_task_detail(
    parser_type: str,
    row_key: str,
    user: dict = Depends(require_permission(ONLINE_RAW_VIEW)),
    conn=Depends(get_db),
):
    return await _mobile_task_detail_data(parser_type, row_key, user, conn)


async def _mobile_task_save_context(conn, parser_type: str, row_key: str) -> dict:
    """Load only the task state changed by an incremental save."""
    if settings.TASK_READ_MODEL_ENABLED:
        source_rows = []
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT source.id,source.physical_row,source.revision "
                "FROM _online_source_rows AS source "
                "WHERE source.parser_type=%s AND source.row_key=%s "
                "AND source.archived_at IS NULL "
                + active_source_sql_filter(parser_type, "source")
                + " ORDER BY source.physical_row,source.id",
                (parser_type, row_key),
            )
            source_rows = await cur.fetchall()
            if not source_rows:
                return {}
            snapshots = await _read_task_snapshots_for_source_rows(
                parser_type, source_rows,
            )
            state_tasks = [
                {
                    "task_id": stable_task_id(
                        get_parser(parser_type).table_name, int(source[1]),
                    ),
                    "revision": int(source[2] or 0),
                }
                for source in source_rows
            ]
            states_by_task = await _read_task_view_states_for_records(state_tasks)
            values_by_source = [
                (int(source[0]), snapshots[int(source[0])])
                for source in source_rows
            ]
            values = values_by_source[0][1]
            source_count = len(values_by_source)
            conflict = any(
                stable_json(snapshot) != stable_json(values)
                for _source_id, snapshot in values_by_source[1:]
            )
            domains = states_by_task[state_tasks[0]["task_id"]]
            registration_link = domains["registration"]["state"]
            review_flow = domains["review"]["state"]
            address_state = domains["address_match"]["state"]
            address_candidate = address_state.get("candidate") or {}
            address_match = {
                **address_state,
                "small_community_id": address_candidate.get("entry_id"),
                "small_community_name": address_candidate.get("name") or address_candidate.get("community_name", ""),
            }
        task = _task_record(
            parser_type,
            row_key,
            values,
            source_count,
            conflict,
            False,
            TASK_WORKFLOWS[parser_type].state(values),
            registration_link=registration_link,
            review_flow=review_flow,
            address_match=address_match,
        )
        return {
            "task_update": {
                key: task[key]
                for key in (
                    "row_key", "summary", "community", "inspector", "state",
                    "needs_review", "review_stage", "review_flow", "source_count",
                    "conflict", "pending_sync", "sync_state", "priority",
                    "registration_link", "address_match",
                )
            },
            "registration_link": registration_link,
            "review_flow": review_flow,
        }

    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT values_json,source_count,conflict,pending_state,task_state "
            "FROM _online_source_projection WHERE parser_type=%s AND row_key=%s",
            (parser_type, row_key),
        )
        projection = await cur.fetchone()
        if not projection:
            return {}
        values = json_value(projection[0], {})
        registration_link = (await registration_links_by_rows(
            cur,
            parser_type,
            [row_key],
        )).get(row_key)
        review_flow = (await review_flows_by_rows(
            cur,
            [(parser_type, row_key)],
        )).get((parser_type, row_key))
        address_match = (await _address_matches_by_rows(
            cur,
            parser_type,
            [row_key],
        )).get(row_key)
    source_count = int(projection[1] or 0)
    conflict = bool(projection[2])
    pending = bool(str(projection[3] or ""))
    task_state_value = str(projection[4] or "")
    task = _task_record(
        parser_type,
        row_key,
        values,
        source_count,
        conflict,
        pending,
        task_state_value,
        registration_link=registration_link,
        review_flow=review_flow,
        address_match=address_match,
    )
    return {
        "task_update": {
            key: task[key]
            for key in (
                "row_key", "summary", "community", "inspector", "state",
                "needs_review", "review_stage", "review_flow", "source_count",
                "conflict", "pending_sync", "sync_state", "priority",
                "registration_link", "address_match",
            )
        },
        "registration_link": registration_link,
        "review_flow": review_flow,
    }


@router.post("/{parser_type}/{row_key}/registration/confirm")
async def manually_confirm_registration(
    parser_type: str,
    row_key: str,
    data: RegistrationManualConfirm,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """高权限人工确认登记，仅限居住证有效但自动匹配需复核的任务。"""
    if parser_type not in REGISTRATION_TASK_TYPES:
        raise HTTPException(400, "该业务不支持登记闭环")
    if not can_confirm_registration(user):
        raise HTTPException(403, "只有基础管控及以上岗位可以人工确认登记")
    user = _require_task_edit_user(user)
    context = await _flow_context(conn, user)
    config = await load_residence_config(conn)
    if not config.session_ready:
        raise HTTPException(409, "居住证平台配置尚未就绪，不能人工确认")
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT link.status,link.reason_code,link.source_id,projection.identity_hmac,"
            "projection.community,projection.source_count "
            "FROM _task_registration_links AS link "
            "JOIN _online_source_projection AS projection "
            "ON projection.parser_type=link.parser_type AND projection.row_key=link.row_key "
            "WHERE link.parser_type=%s AND link.row_key=%s",
            (parser_type, row_key),
        )
        link = await cur.fetchone()
        if not link or str(link[0] or "") != "review_required" or str(link[1] or "") not in {
            "address_mismatch", "address_ambiguous"
        }:
            raise HTTPException(409, "该任务当前不满足人工确认条件")
        if int(link[5] or 0) != 1:
            raise HTTPException(409, "任务来源不唯一，不能人工确认")
        community_values = context.get("community_values")
        if community_values is not None and str(link[4] or "") not in set(community_values):
            raise HTTPException(403, "无权复核该社区任务")
        expected_hmac = str(link[3] or "")
        await cur.execute(
            "SELECT values_json,revision FROM _online_source_rows "
            "WHERE id=%s AND parser_type=%s AND row_key=%s",
            (int(link[2]), parser_type, row_key),
        )
        source_row = await cur.fetchone()
        if not source_row:
            raise HTTPException(404, "任务来源已不存在")
        current_source_values = json_value(source_row[0], {})
        current_revision = int(source_row[1])
    await conn.rollback()
    if data.expected_revision != current_revision:
        raise HTTPException(409, "任务来源已变化，请刷新后重试")
    try:
        target = await _load_current_target(parser_type, row_key, expected_hmac)
        state, _address, registration_code = await _lookup_registration_address_target(config, target)
    except Exception as exc:
        raise HTTPException(502, "居住证平台查询失败，不能人工确认") from exc
    if state != "registered" or registration_code == "1":
        raise HTTPException(409, "居住证登记无效或已注销，不能人工确认")

    async def confirm_prepare(*, cur, source, current_values, changes):
        del changes
        workflow = TASK_WORKFLOWS[parser_type]
        if str(current_values.get(workflow.result_field) or "").strip() != "待登记":
            raise HTTPException(409, "任务当前已不是待登记，请刷新后重试")
        await cur.execute(
            "SELECT status,reason_code,source_id,property_id "
            "FROM _task_registration_links WHERE parser_type=%s AND row_key=%s FOR UPDATE",
            (parser_type, row_key),
        )
        current_link = await cur.fetchone()
        if (
            not current_link
            or str(current_link[0] or "") != "review_required"
            or str(current_link[1] or "") not in {"address_mismatch", "address_ambiguous"}
            or int(current_link[2] or 0) != int(source["id"])
            or current_link[3] is None
        ):
            raise HTTPException(409, "登记复核状态已变化，请刷新后重试")
        return {}

    async def confirm_callback(*, cur, source, before, after, row_key_before, row_key_after, revision):
        del source, before, after, revision
        await cur.execute(
            "UPDATE _task_registration_links SET status='confirmation_pending',reason_code='',"
            "confirmed_by=%s,manual_confirmed_at=UTC_TIMESTAMP(),confirmed_at=NULL,"
            "manual_reason=%s,manual_note=%s "
            "WHERE parser_type=%s AND row_key=%s AND source_id=%s AND status='review_required'",
            (int(user.get("id")), data.reason, data.note[:500], parser_type, row_key_after, int(link[2])),
        )
        if cur.rowcount != 1:
            raise HTTPException(409, "登记复核状态已变化，请刷新后重试")
        await record_registration_event(
            cur, parser_type=parser_type, row_key=row_key_after,
            source_id=int(link[2]), event_type="manual_registration_confirmed",
            reason_code=data.reason, actor_user_id=int(user.get("id")),
        )

    result = await queue_source_fields(
        parser_type=parser_type,
        source_id=int(link[2]),
        changes={TASK_WORKFLOWS[parser_type].result_field: "已登记"},
        base_values={TASK_WORKFLOWS[parser_type].result_field: str(current_source_values.get(TASK_WORKFLOWS[parser_type].result_field) or "")},
        request=request, user=user, conn=conn, explicit_text_edit=False,
        registration_mode=True, audit_action="manual_registration",
        transaction_prepare=confirm_prepare,
        transaction_callback=confirm_callback,
    )
    await record_admin_audit(
        user, "mobile_tasks.registration.manual_confirm",
        target_type="mobile_task", target_name=f"{parser_type}:{row_key}",
        detail={"reason": data.reason}, **request_audit_fields(request),
    )
    return result


@router.patch("/{parser_type}/source-rows/{source_id}")
async def update_mobile_task(
    parser_type: str,
    source_id: int,
    data: TaskBatchUpdate,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    user = _require_task_edit_user(user)
    analysis_fields = set(TASK_WORKFLOWS[parser_type].analysis_fields)
    if any(field in analysis_fields for field in data.changes):
        raise HTTPException(403, "请从研判页面修改研判内容")
    context = await _flow_context(conn, user)
    async with conn.cursor() as cur:
        await _validate_assignment(cur, context, data.changes)
    transaction_prepare, transaction_callback, registration_mode = (
        _registration_update_hooks(parser_type, data, user)
    )
    result = await queue_source_fields(
        parser_type=parser_type,
        source_id=source_id,
        changes=data.changes,
        base_values=data.base_values,
        expected_revision=data.expected_revision,
        request=request,
        user=user,
        conn=conn,
        explicit_text_edit=True,
        registration_mode=registration_mode,
        transaction_prepare=transaction_prepare,
        transaction_callback=transaction_callback,
    )
    if registration_mode:
        wake_residence_lookup_scheduler()
    return {
        **result,
        **await _mobile_task_save_context(conn, parser_type, str(result["row_key"])),
    }


@router.post("/{parser_type}/source-rows/{source_id}/claim")
async def claim_mobile_task(
    parser_type: str,
    source_id: int,
    data: TaskBatchUpdate,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Let a group member claim an unassigned task while saving their edit."""
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入手机任务工作台")
    user = _require_task_edit_user(user)
    context = await _flow_context(conn, user)
    if context.get("admin_mode") or context.get("position") != "组员":
        raise HTTPException(403, "只有组员可以自主领取未分配任务")
    if not data.changes:
        raise HTTPException(400, "请先填写需要保存的核查内容")
    if "核查人" in data.changes:
        raise HTTPException(400, "领取人由当前登录账号自动确定")
    analysis_fields = set(TASK_WORKFLOWS[parser_type].analysis_fields)
    if any(field in analysis_fields for field in data.changes):
        raise HTTPException(403, "请从研判页面修改研判内容")

    inspector = str(context["name"] or "").strip()
    transaction_prepare, transaction_callback, registration_mode = (
        _registration_update_hooks(parser_type, data, user)
    )

    def validate_unassigned(current: dict) -> None:
        if str(current.get("核查人") or "").strip():
            raise HTTPException(409, "该任务已被领取，请刷新后重试")

    result = await queue_source_fields(
        parser_type=parser_type,
        source_id=source_id,
        changes={"核查人": inspector, **data.changes},
        base_values={**data.base_values, "核查人": ""},
        expected_revision=data.expected_revision,
        request=request,
        user=user,
        conn=conn,
        explicit_text_edit=True,
        audit_action="claim",
        current_values_validator=validate_unassigned,
        registration_mode=registration_mode,
        transaction_prepare=transaction_prepare,
        transaction_callback=transaction_callback,
    )
    if registration_mode:
        wake_residence_lookup_scheduler()
    await record_admin_audit(
        user,
        "mobile_tasks.self_claim",
        target_type="mobile_task",
        target_name=f"{parser_type}:{source_id}",
        detail={"columns": list(data.changes)},
        **request_audit_fields(request),
    )
    return {
        **result,
        **await _mobile_task_save_context(conn, parser_type, str(result["row_key"])),
        "message": "已领取任务并保存到本地任务池",
    }


@router.get("/{parser_type}/internal-transfer-options")
async def get_internal_transfer_options(
    parser_type: str,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    _require_unverifiable_reviewer(user)
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT community.name,member.name
            FROM _communities community
            JOIN _departments department
              ON department.community_id=community.id
             AND department.department_type='community'
             AND department.is_active=1
            LEFT JOIN _grid_member_department_links link
              ON link.department_id=department.id
            LEFT JOIN _grid_members member
              ON member.id=link.member_id
             AND member.position='组长'
             AND member.status='在岗'
            WHERE community.is_active=1
            ORDER BY community.name,member.name
            """
        )
        grouped: dict[str, list[str]] = {}
        for community, leader in await cur.fetchall():
            name = str(community or "").strip()
            if not name:
                continue
            grouped.setdefault(name, [])
            leader_name = str(leader or "").strip()
            if leader_name and leader_name not in grouped[name]:
                grouped[name].append(leader_name)
    return {
        "data": [
            {"community": community, "leaders": leaders}
            for community, leaders in grouped.items()
        ],
    }


@router.post("/{parser_type}/source-rows/{source_id}/internal-transfer")
async def transfer_mobile_task_internally(
    parser_type: str,
    source_id: int,
    data: InternalTransferRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    scoped_user = _require_unverifiable_reviewer(user)
    target_community = data.target_community.strip()
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT row_key,row_hash FROM _online_source_rows source "
            "WHERE source.id=%s AND source.parser_type=%s AND source.archived_at IS NULL "
            f"{active_source_sql_filter(parser_type)}",
            (source_id, parser_type),
        )
        source_row = await cur.fetchone()
        if not source_row:
            raise HTTPException(404, "本地任务来源不存在")
        if str(source_row[0] or "") != data.expected_row_key:
            raise HTTPException(409, "任务主键已变化，请刷新后重试")
        if str(source_row[1] or "") != data.expected_row_hash:
            raise HTTPException(409, "任务已被其他人更新，请刷新后重试")
        await cur.execute(
            """
            SELECT member.name
            FROM _communities community
            JOIN _departments department
              ON department.community_id=community.id
             AND department.department_type='community'
             AND department.is_active=1
            JOIN _grid_member_department_links link
              ON link.department_id=department.id
            JOIN _grid_members member
              ON member.id=link.member_id
             AND member.position='组长'
             AND member.status='在岗'
            WHERE community.is_active=1 AND community.name=%s
            ORDER BY member.name
            """,
            (target_community,),
        )
        leaders = list(dict.fromkeys(
            str(row[0] or "").strip() for row in await cur.fetchall()
            if str(row[0] or "").strip()
        ))
    if not leaders:
        raise HTTPException(409, "目标社区没有在岗组长，请先维护人员配置")
    if len(leaders) == 1:
        target_leader = leaders[0]
    else:
        target_leader = data.target_leader.strip()
        if not target_leader:
            raise HTTPException(422, "目标社区有多名在岗组长，请明确选择接手组长")
        if target_leader not in leaders:
            raise HTTPException(400, "所选人员不是目标社区的在岗组长")

    workflow = TASK_WORKFLOWS[parser_type]
    community_field = get_parser(parser_type).COMMUNITY_COLUMN

    def validate_transfer(current: dict) -> None:
        result = str(current.get(workflow.result_field) or "").strip()
        if result != "移交（所内）":
            raise HTTPException(409, "只有核查结果为“移交（所内）”的任务可以转交")

    async def transfer_callback(
        *, cur, source, before, after, row_key_before, row_key_after, revision,
    ):
        del after, row_key_before
        try:
            await record_internal_transfer(
                cur,
                parser_type=parser_type,
                row_key=row_key_after,
                source_id=int(source["id"]),
                before=before,
                target_community=target_community,
                target_leader=target_leader,
                operator_user_id=int(scoped_user.get("id")) if scoped_user.get("id") else None,
                source_revision=revision,
                community_field=community_field,
            )
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from exc

    result = await queue_source_fields(
        parser_type=parser_type,
        source_id=source_id,
        changes={community_field: target_community, "核查人": target_leader},
        base_values={},
        expected_revision=data.expected_revision,
        request=request,
        user=scoped_user,
        conn=conn,
        explicit_text_edit=True,
        current_values_validator=validate_transfer,
        transaction_callback=transfer_callback,
        audit_action="internal_transfer_local",
    )
    await record_admin_audit(
        scoped_user,
        "mobile_tasks.internal_transfer",
        target_type="mobile_task",
        target_name=f"{parser_type}:{source_id}",
        detail={"target_community": target_community, "target_leader": target_leader},
        **request_audit_fields(request),
    )
    return {**result, "target_community": target_community, "target_leader": target_leader}


@router.post("/{parser_type}/source-rows/{source_id}/resolve-sync-conflict")
async def resolve_mobile_task_sync_conflict(
    parser_type: str,
    source_id: int,
    data: SyncConflictResolution,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    raise HTTPException(410, "腾讯数据源已下线，不能处理旧同步冲突")


@router.post("/{parser_type}/bulk-assign")
async def bulk_assign_mobile_tasks(
    parser_type: str,
    data: BulkAssignmentRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Assign one bounded local task chunk with one incremental projection pass."""
    started_at = time.monotonic()
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    user = _require_task_edit_user(user)
    context = await _flow_context(conn, user)
    if not _can_assign_tasks(context):
        raise HTTPException(403, "只有组长及有权管理任务的上级岗位可以批量分配核查人")

    row_keys = list(dict.fromkeys(str(value).strip() for value in data.row_keys if str(value).strip()))
    inspector = str(data.inspector or "").strip()
    if not row_keys:
        raise HTTPException(400, "请选择任务")
    if data.mode == "single" and not inspector:
        raise HTTPException(400, "请选择在岗组员")
    balanced_total = data.balanced_total or len(row_keys)
    if data.mode == "balanced" and (
        data.balanced_offset + len(row_keys) > balanced_total
    ):
        raise HTTPException(400, "平均分配分块范围无效，请重新发起分配")

    projection_by_key: dict[str, dict] = {}
    source_rows_by_key: dict[str, list[dict]] = {}
    skipped: list[dict[str, str]] = []
    async with conn.cursor() as cur:
        if not local_data_source_enabled() and not await _writeback_enabled(cur):
            raise HTTPException(503, "在线回写已由超级管理员暂停")
        scope_where, scope_params = _scope_where(
            context,
            "all" if context.get("admin_mode") else "community",
        )
        key_placeholders = ", ".join(["%s"] * len(row_keys))
        await cur.execute(
            f"""
            SELECT projection.row_key, projection.community, projection.inspector,
                   projection.task_state, projection.conflict, projection.source_count,
                   projection.address_match_status,
                   projection.small_community_id,
                   projection.small_community_name
            FROM _online_source_projection AS projection
            WHERE projection.parser_type=%s
              AND projection.row_key IN ({key_placeholders})
              AND {scope_where}
            """,
            [parser_type, *row_keys, *scope_params],
        )
        for row in await cur.fetchall():
            projection_by_key[str(row[0])] = {
                "community": str(row[1] or "").strip(),
                "inspector": str(row[2] or "").strip(),
                "state": str(row[3] or "").strip(),
                "conflict": bool(row[4]),
                "source_count": int(row[5] or 0),
                "address_match_status": str(row[6] or "unmatched"),
                "small_community_id": int(row[7]) if row[7] is not None else None,
                "small_community_name": str(row[8] or ""),
            }
        missing_scope = [key for key in row_keys if key not in projection_by_key]
        if missing_scope:
            raise HTTPException(403, "部分任务不在当前账号可操作范围内，请刷新后重试")

        assignment_context = await inspector_option_context(
            cur,
            user,
            assignment_only=True,
        )
        community_aliases = assignment_context["community_aliases"]
        inspectors_by_community = assignment_context["inspectors_by_community"]
        formal_communities = {
            community_aliases.get(item["community"], "")
            for item in projection_by_key.values()
        }
        if not formal_communities or "" in formal_communities:
            raise HTTPException(403, "部分任务社区不在当前账号可分配范围内")
        if len(formal_communities) != 1:
            raise HTTPException(400, "批量分配必须一次只选择同一社区的任务")
        formal_community = next(iter(formal_communities))
        available_inspectors = list(
            inspectors_by_community.get(formal_community) or []
        )
        if not available_inspectors:
            raise HTTPException(400, "该社区当前没有在岗组员可分配")
        if data.mode == "single" and inspector not in available_inspectors:
            raise HTTPException(400, "只能分配给任务所属社区的在岗组员")

        await cur.execute(
            f"""
            SELECT source.id, source.row_key, source.revision,
                   source.physical_row, source.row_hash, source.values_json,
                   source.sheet_id
            FROM _online_source_rows AS source
            WHERE source.parser_type=%s
              AND source.row_key IN ({key_placeholders})
              AND source.archived_at IS NULL
              {active_source_sql_filter(parser_type)}
            ORDER BY source.row_key, source.id
            """,
            [parser_type, *row_keys],
        )
        for source_id, row_key, revision, physical_row, row_hash, values_json, sheet_id in await cur.fetchall():
            source_rows_by_key.setdefault(str(row_key), []).append({
                "id": int(source_id),
                "row_key": str(row_key),
                "revision": int(revision),
                "physical_row": int(physical_row),
                "row_hash": str(row_hash or ""),
                "values": json_value(values_json, {}),
                "sheet_id": str(sheet_id or ""),
            })
        if local_data_source_enabled():
            # Projection rows may still carry the old Tencent source count or
            # conflict bit until the first local rebuild.  Assignment must
            # use the active local rows just loaded above.
            for row_key, item in projection_by_key.items():
                active_count = len(source_rows_by_key.get(row_key) or [])
                item["source_count"] = active_count
                item["conflict"] = active_count > 1

    eligible_keys: list[str] = []
    for row_key in row_keys:
        item = projection_by_key[row_key]
        if item["inspector"]:
            skipped.append({"row_key": row_key, "reason": "已有核查人"})
            continue
        if item["state"] == "completed":
            skipped.append({"row_key": row_key, "reason": "任务已完成"})
            continue
        if item["conflict"]:
            skipped.append({"row_key": row_key, "reason": "来源存在冲突"})
            continue
        if item["source_count"] != 1:
            skipped.append({"row_key": row_key, "reason": "存在重复本地来源，请先处理来源异常"})
            continue
        if not source_rows_by_key.get(row_key):
            skipped.append({"row_key": row_key, "reason": "找不到来源行"})
            continue
        eligible_keys.append(row_key)

    if data.mode == "balanced":
        assignment_plan, assignment_counts = _balanced_assignment_plan(
            row_keys,
            available_inspectors,
            total_count=balanced_total,
            start_index=data.balanced_offset,
        )
    else:
        assignment_plan = {row_key: inspector for row_key in eligible_keys}
        assignment_counts = {
            inspector: len(eligible_keys),
        }

    update_count = 0
    failures: list[dict[str, str]] = []
    successful_keys: list[str] = []
    successful_assignment_counts = {name: 0 for name in assignment_counts}
    if local_data_source_enabled():
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                for row_key in eligible_keys:
                    assigned_inspector = assignment_plan.get(row_key, "")
                    if not assigned_inspector:
                        skipped.append({"row_key": row_key, "reason": "没有生成分配方案"})
                        continue
                    sources = source_rows_by_key.get(row_key) or []
                    if len(sources) != 1:
                        skipped.append({"row_key": row_key, "reason": "本地来源不唯一"})
                        continue
                    source = sources[0]
                    await cur.execute("SAVEPOINT bulk_assign_task")
                    try:
                        await apply_local_system_changes(
                            cur,
                            source={
                                **source,
                                "spreadsheet_id": 0,
                                "spreadsheet": {"parser_type": parser_type},
                            },
                            changes={"核查人": assigned_inspector},
                            user=user,
                            action="bulk_assign_local",
                            rebuild=False,
                        )
                    except (ValueError, LookupError):
                        await cur.execute("ROLLBACK TO SAVEPOINT bulk_assign_task")
                        failures.append({"row_key": row_key, "reason": "任务已变化，请刷新后重试"})
                    else:
                        await capture_first_assignment(
                            cur,
                            parser_type=parser_type,
                            row_key=row_key,
                            community=projection_by_key[row_key]["community"],
                            inspector=assigned_inspector,
                            actor_user_id=int(user.get("id")) if user.get("id") else None,
                            source="bulk_assignment",
                        )
                        update_count += 1
                        successful_keys.append(row_key)
                        successful_assignment_counts[assigned_inspector] = (
                            successful_assignment_counts.get(assigned_inspector, 0) + 1
                        )
                    finally:
                        await cur.execute("RELEASE SAVEPOINT bulk_assign_task")
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    else:
        for row_key in eligible_keys:
            assigned_inspector = assignment_plan.get(row_key, "")
            if not assigned_inspector:
                skipped.append({"row_key": row_key, "reason": "没有生成分配方案"})
                continue
            sources = source_rows_by_key.get(row_key) or []
            if not sources:
                skipped.append({"row_key": row_key, "reason": "找不到来源行"})
                continue
            task_ok = True
            for source in sources:
                try:
                    await queue_source_fields(
                        parser_type=parser_type,
                        source_id=source["id"],
                        changes={"核查人": assigned_inspector},
                        expected_revision=source["revision"],
                        request=request,
                        user=user,
                        conn=conn,
                        explicit_text_edit=True,
                    )
                except HTTPException as exc:
                    task_ok = False
                    reason = {
                        400: "数据校验未通过",
                        403: "没有该任务的编辑权限",
                        409: "任务已变化，请刷新后重试",
                        502: "腾讯回写校验失败",
                    }.get(exc.status_code, "保存失败")
                    failures.append({"row_key": row_key, "reason": reason})
                    break
            if task_ok:
                update_count += 1
                successful_keys.append(row_key)
                successful_assignment_counts[assigned_inspector] = (
                    successful_assignment_counts.get(assigned_inspector, 0) + 1
                )

    result = _bulk_assignment_result(
        updated=update_count,
        skipped=skipped,
        failures=failures,
        inspector=inspector,
        mode=data.mode,
        assignment_counts=successful_assignment_counts,
        duration_ms=max(0, int((time.monotonic() - started_at) * 1000)),
    )
    await record_admin_audit(
        user,
        "mobile_tasks.bulk_assign",
        target_type="mobile_task",
        target_name=parser_type,
        detail={
            "task_count": len(row_keys),
            "updated_count": result["updated"],
            "skipped_count": result["skipped"],
            "failed_count": result["failed"],
            "inspector": inspector if data.mode == "single" else "",
            "allocation_mode": data.mode,
            "assignment_counts": successful_assignment_counts,
        },
        **request_audit_fields(request),
    )
    logger.info(
        "mobile_task_bulk_assignment parser=%s requested=%d eligible=%d "
        "updated=%d skipped=%d failed=%d duration_ms=%d",
        parser_type,
        len(row_keys),
        len(eligible_keys),
        result["updated"],
        result["skipped"],
        result["failed"],
        max(0, int((time.monotonic() - started_at) * 1000)),
    )
    return result


@router.post("/{parser_type}/assignments/cancel")
async def cancel_mobile_task_assignments(
    parser_type: str,
    data: CancelAssignmentsRequest,
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """撤销当前业务范围内尚未完成任务的核查人分配。

    该操作只清除“核查人”字段，不修改核查结果、研判、反馈或任务历史；
    每条变更仍通过本地事务写入审计，便于追溯误分配的恢复过程。
    """
    if parser_type not in TASK_WORKFLOWS:
        raise HTTPException(400, "该业务尚未接入任务工作台")
    user = _require_task_edit_user(user)
    context = await _flow_context(conn, user)
    if not _can_assign_tasks(context):
        raise HTTPException(403, "只有组长及有权管理任务的上级岗位可以撤销批量分配")
    if not local_data_source_enabled():
        raise HTTPException(410, "本地数据源未启用，无法执行本地分配恢复")

    requested_community = str(data.community or "").strip()
    scope_where, scope_params = _scope_where(
        context,
        "all" if context.get("admin_mode") else "community",
    )
    if requested_community:
        aliases = await community_names_for_scopes(conn, [requested_community])
        formal = aliases[0] if aliases else requested_community
        scope_where += " AND projection.community=%s"
        scope_params.append(formal)

    changed = 0
    skipped: list[dict[str, str]] = []
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT source.id, source.row_key, source.revision,
                       source.physical_row, source.values_json,
                       projection.task_state, projection.inspector,
                       projection.source_count, projection.conflict
                FROM _online_source_projection projection
                JOIN _online_source_rows source
                  ON source.parser_type=projection.parser_type
                 AND source.row_key=projection.row_key
                 AND source.archived_at IS NULL
                 AND source.spreadsheet_id=0
                WHERE projection.parser_type=%s
                  AND {scope_where}
                  AND TRIM(COALESCE(projection.inspector,''))<>''
                  AND projection.task_state<>'completed'
                ORDER BY source.id
                LIMIT %s
                """,
                [parser_type, *scope_params, MAX_BULK_ASSIGNMENT_TASKS * 20],
            )
            rows = await cur.fetchall()
            for source_id, row_key, revision, physical_row, values_json, task_state, inspector, source_count, conflict in rows:
                if int(source_count or 0) != 1 or bool(conflict):
                    skipped.append({"row_key": str(row_key), "reason": "来源异常，未自动撤销"})
                    continue
                values = json_value(values_json, {})
                if not str(values.get("核查人") or "").strip():
                    continue
                try:
                    await apply_local_system_changes(
                        cur,
                        source={
                            "id": int(source_id),
                            "revision": int(revision),
                            "physical_row": int(physical_row),
                            "row_key": str(row_key),
                            "values": values,
                            "spreadsheet_id": 0,
                            "spreadsheet": {"parser_type": parser_type},
                        },
                        changes={"核查人": ""},
                        user=user,
                        action="bulk_unassign_local",
                        rebuild=False,
                    )
                    changed += 1
                except (ValueError, LookupError) as exc:
                    skipped.append({"row_key": str(row_key), "reason": str(exc) or "任务已变化，请刷新后重试"})
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise

    await record_admin_audit(
        user,
        "mobile_tasks.bulk_unassign",
        target_type="mobile_task",
        target_name=parser_type,
        detail={
            "community": requested_community,
            "updated_count": changed,
            "skipped_count": len(skipped),
        },
        **request_audit_fields(request),
    )
    return {"updated": changed, "skipped": len(skipped), "details": skipped}
