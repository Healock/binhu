"""腾讯“调照片名单”与照片工单之间的安全、幂等同步。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qs, urlparse

from database import db_manager
from services.registry_security import hmac_digest
from services.txdocs_client import TxDocsClient
from services.workflow_support import platform_schema, queue_user_ids, workflow_notification


SOURCE_CODE = "photo_request_roster"
COLUMNS = ["任务社区", "数据来源", "对象姓名", "身份证号", "申请人员", "申请日期", "处理状态"]
COMPLETED_MARK = "滨湖平台已调照片"
TERMINAL_STATUSES = {"approved", "completed", "rejected", "cancelled", "withdrawn"}
FAILURE_WORDS = ("无照片", "未找到", "身份证错误", "身份证有误", "号码错误", "信息错误")
MARKER_HINT = re.compile(r"(?:截止|截至|[.．。:/：\-－])", re.I)
DATE_RE = re.compile(r"(?:(20\d{2})\D*)?(\d{1,2})[.．/\-－](\d{1,2})")
TIME_RE = re.compile(r"(?:^|\D)(\d{1,2})[.．:：](\d{1,2})(?:\D|$)")
PHOTO_SHEET_OPERATION_LOCK = asyncio.Lock()


@dataclass(slots=True)
class ParsedRow:
    physical_row: int
    kind: str
    values: dict[str, str]
    row_hash: str
    fingerprint: str
    identity_number: str = ""
    identity_hmac: str = ""
    identity_hmac_version: int | None = None
    data_issue: str = ""
    requested_at: datetime | None = None
    marker_at: datetime | None = None
    time_inferred: bool = False


def parse_source_url(value: str) -> tuple[str, str]:
    parsed = urlparse(str(value or "").strip())
    match = re.search(r"/sheet/([A-Za-z0-9_-]+)", parsed.path)
    file_id = match.group(1) if match else ""
    sheet_id = (parse_qs(parsed.query).get("tab") or [""])[0]
    if not file_id or not sheet_id:
        raise ValueError("腾讯表格地址必须包含文件编号和 tab 子表编号")
    return file_id, sheet_id


def _text(value: object) -> str:
    return str(value or "").replace("\u3000", " ").strip()


def _digest(values: list[str]) -> str:
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def normalize_import_identity(value: object) -> tuple[str, str]:
    raw = _text(value)
    if not raw:
        return "", "身份证号为空"
    normalized = re.sub(r"[\s\u3000]+", "", raw)
    normalized = re.sub(r"[，,。.;；、]+$", "", normalized)
    if normalized.endswith(("x", "X")):
        normalized = normalized[:-1] + "X"
    if not re.fullmatch(r"(?:\d{15}|\d{17}[0-9X])", normalized):
        return normalized[:50], "身份证号格式异常"
    return normalized, ""


def _parse_request_date(value: str) -> datetime | None:
    value = _text(value)
    if not value:
        return None
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    match = DATE_RE.search(value)
    if match and match.group(1):
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def _parse_marker(value: str, nearby: datetime | None) -> tuple[datetime | None, bool]:
    text = _text(value).replace("截止", " ").replace("截至", " ")
    date_match = DATE_RE.search(text)
    time_text = text
    if date_match:
        time_text = text[:date_match.start()] + " " + text[date_match.end():]
    time_matches = list(TIME_RE.finditer(time_text))
    clock: time | None = None
    if time_matches:
        last = time_matches[-1]
        try:
            clock = time(int(last.group(1)), int(last.group(2)))
        except ValueError:
            clock = None
    inferred = False
    if date_match:
        year = int(date_match.group(1)) if date_match.group(1) else (nearby.year if nearby else datetime.now().year)
        inferred = date_match.group(1) is None
        try:
            result = datetime(year, int(date_match.group(2)), int(date_match.group(3)))
        except ValueError:
            return None, inferred
        if inferred and nearby and result < nearby - timedelta(days=180):
            result = result.replace(year=result.year + 1)
        if clock:
            result = datetime.combine(result.date(), clock)
        return result, inferred
    if clock and nearby:
        return datetime.combine(nearby.date(), clock), True
    return None, bool(clock)


def _is_marker(values: dict[str, str]) -> bool:
    first = _text(values.get(COLUMNS[0]))
    rest = [_text(values.get(column)) for column in COLUMNS[1:6]]
    return bool(first and not any(rest) and MARKER_HINT.search(first))


def parse_rows(rows: list[dict]) -> list[ParsedRow]:
    parsed: list[ParsedRow] = []
    nearby_date: datetime | None = None
    for raw in rows:
        values = {column: _text((raw.get("values") or {}).get(column)) for column in COLUMNS}
        physical_row = int(raw.get("physical_row") or 0)
        if not physical_row or not any(values.values()):
            continue
        row_hash = _digest([str(physical_row), *[values[column] for column in COLUMNS]])
        fingerprint = _digest([values[column] for column in COLUMNS[:6]])
        if _is_marker(values):
            marker_at, inferred = _parse_marker(values[COLUMNS[0]], nearby_date)
            marker_fingerprint = _digest([
                values[COLUMNS[0]], marker_at.isoformat() if marker_at else "unparsed",
            ])
            parsed.append(ParsedRow(
                physical_row=physical_row, kind="marker", values=values,
                row_hash=row_hash, fingerprint=marker_fingerprint,
                marker_at=marker_at, time_inferred=inferred,
                data_issue="" if marker_at else "批次时间无法识别",
            ))
            continue
        requested_at = _parse_request_date(values["申请日期"])
        if requested_at:
            nearby_date = requested_at
        identity_number, data_issue = normalize_import_identity(values["身份证号"])
        identity_hmac = ""
        hmac_version = None
        if not data_issue:
            identity_hmac, hmac_version = hmac_digest(identity_number, kind="identity")
        issues = [item for item in (data_issue, "申请日期为空或格式异常" if not requested_at else "") if item]
        parsed.append(ParsedRow(
            physical_row=physical_row, kind="request", values=values,
            row_hash=row_hash, fingerprint=fingerprint,
            identity_number=identity_number, identity_hmac=identity_hmac or "",
            identity_hmac_version=hmac_version, data_issue="；".join(issues),
            requested_at=requested_at,
        ))
    return parsed


def preview_summary(parsed: list[ParsedRow]) -> dict:
    requests = [row for row in parsed if row.kind == "request"]
    markers = [row for row in parsed if row.kind == "marker"]
    last_marker_row = max((row.physical_row for row in markers), default=0)
    completed = sum(1 for row in requests if row.physical_row < last_marker_row)
    pending = len(requests) - completed
    issues = sum(1 for row in parsed if row.data_issue)
    duplicate_groups: dict[str, int] = {}
    for row in requests:
        duplicate_groups[row.fingerprint] = duplicate_groups.get(row.fingerprint, 0) + 1
    duplicates = sum(1 for count in duplicate_groups.values() if count > 1)
    digest = _digest([row.row_hash for row in parsed])
    return {
        "rows_read": len(parsed), "requests": len(requests), "markers": len(markers),
        "historical_completed": completed, "pending_after_last_marker": pending,
        "issue_count": issues, "duplicate_groups": duplicates,
        "last_marker_row": last_marker_row or None, "preview_token": digest,
    }


def historical_result(g_value: str) -> tuple[str, str]:
    status = "not_found" if any(word in _text(g_value) for word in FAILURE_WORDS) else "found"
    return status, "腾讯历史批次完成、无平台附件"


async def _oauth_client() -> TxDocsClient:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT client_id, access_token, open_id FROM _config_oauth_tokens ORDER BY id DESC LIMIT 1"
            )
            row = await cur.fetchone()
    if not row or not row[1] or not row[2]:
        raise RuntimeError("腾讯文档 OAuth 尚未配置")
    return TxDocsClient(str(row[0]), str(row[1]), str(row[2]))


async def _global_writeback_enabled() -> bool:
    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT config_value FROM _system_config WHERE config_key='online_writeback_enabled'")
            row = await cur.fetchone()
    return str(row[0] if row else "0").strip().lower() in {"1", "true", "yes", "on"}


async def load_source(cur, *, for_update: bool = False) -> dict:
    await cur.execute(
        "SELECT id, file_url, file_id, sheet_id, header_row, read_enabled, write_enabled, "
        "import_applied_at, legacy_cutoff_row, last_cursor_row, last_full_sync_date, last_sync_at, "
        "last_sync_status, last_error FROM photo_sheet_sources WHERE source_code=%s" + (" FOR UPDATE" if for_update else ""),
        (SOURCE_CODE,),
    )
    row = await cur.fetchone()
    if not row:
        raise RuntimeError("调照片名单配置尚未初始化")
    keys = ["id", "file_url", "file_id", "sheet_id", "header_row", "read_enabled", "write_enabled",
            "import_applied_at", "legacy_cutoff_row", "last_cursor_row", "last_full_sync_date", "last_sync_at",
            "last_sync_status", "last_error"]
    result = dict(zip(keys, row))
    result["id"] = int(result["id"])
    result["header_row"] = int(result["header_row"] or 1)
    result["read_enabled"] = bool(result["read_enabled"])
    result["write_enabled"] = bool(result["write_enabled"])
    return result


async def read_online_rows(source: dict, *, start_row: int | None = None) -> list[dict]:
    if not source.get("file_id") or not source.get("sheet_id"):
        raise RuntimeError("调照片名单尚未配置腾讯文件和子表")
    client = await _oauth_client()
    try:
        if start_row is None:
            return await client.read_all_source_rows(
                str(source["file_id"]), str(source["sheet_id"]), int(source["header_row"]), COLUMNS,
                include_detected_headers=False,
            )
        row_total = await client.get_sheet_row_total(str(source["file_id"]), str(source["sheet_id"]))
        if row_total is None:
            return await client.read_all_source_rows(
                str(source["file_id"]), str(source["sheet_id"]), int(source["header_row"]), COLUMNS,
                include_detected_headers=False,
            )
        current = max(int(source["header_row"]) + 1, int(start_row))
        result: list[dict] = []
        while current <= row_total:
            end = min(row_total, current + 999)
            response = await client.read_range(
                str(source["file_id"]), str(source["sheet_id"]), f"A{current}:G{end}",
            )
            raw_rows = client._raw_rows(response)
            for offset, raw_row in enumerate(raw_rows):
                values, metadata = client._decode_row(raw_row, COLUMNS)
                if any(values.values()) and not client._looks_like_header(values, COLUMNS):
                    result.append({"physical_row": current + offset, "values": values, "cell_meta": metadata})
            current = end + 1
        return result
    finally:
        await client.close()


async def preview_online(cur) -> dict:
    source = await load_source(cur)
    parsed = parse_rows(await read_online_rows(source))
    return {**preview_summary(parsed), "source_id": source["id"]}


async def _match_requester(cur, name: str) -> tuple[int | None, str]:
    name = _text(name)
    if not name:
        return None, "申请人为空"
    schema = platform_schema().replace("`", "")
    await cur.execute(
        f"SELECT user.id FROM `{schema}`._grid_members member "
        f"JOIN `{schema}`._users user ON user.member_id=member.id "
        "WHERE member.name=%s AND member.status<>'离岗' ORDER BY user.id LIMIT 2",
        (name,),
    )
    rows = await cur.fetchall()
    if len(rows) == 1:
        return int(rows[0][0]), ""
    return None, "申请人重名" if len(rows) > 1 else "申请人未匹配"


async def _requester_map(cur) -> dict[str, tuple[int | None, str]]:
    schema = platform_schema().replace("`", "")
    await cur.execute(
        f"SELECT member.name,MIN(user.id),COUNT(*) FROM `{schema}`._grid_members member "
        f"JOIN `{schema}`._users user ON user.member_id=member.id "
        "WHERE member.status<>'离岗' GROUP BY member.name"
    )
    return {
        _text(name): (int(user_id), "") if int(count) == 1 else (None, "申请人重名")
        for name, user_id, count in await cur.fetchall()
    }


async def _photo_workflow(cur) -> tuple[int, list[tuple]]:
    await cur.execute(
        "SELECT version.id FROM workflow_type_versions version JOIN workflow_types type "
        "ON type.id=version.workflow_type_id WHERE type.code='photo_request' "
        "AND version.status='published' ORDER BY version.version_no DESC LIMIT 1"
    )
    row = await cur.fetchone()
    if not row:
        raise RuntimeError("照片调取流程尚未发布")
    version_id = int(row[0])
    await cur.execute(
        "SELECT step.id, step.step_order, step.name, step.default_due_hours, step.config_json "
        "FROM workflow_steps step WHERE step.workflow_version_id=%s ORDER BY step.step_order",
        (version_id,),
    )
    steps = await cur.fetchall()
    if not steps:
        raise RuntimeError("照片调取流程没有处理节点")
    return version_id, steps


def _step_config(value: object) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


async def _create_external_ticket(
    cur, *, source_id: int, row: ParsedRow, batch_id: int | None,
    completed: bool, legacy: bool, notify_queue: bool,
    workflow_context: tuple[int, list[tuple]] | None = None,
    requester_matches: dict[str, tuple[int | None, str]] | None = None,
) -> int:
    requester_name = _text(row.values["申请人员"])
    if requester_matches is None:
        requester_id, requester_issue = await _match_requester(cur, requester_name)
    elif not requester_name:
        requester_id, requester_issue = None, "申请人为空"
    else:
        requester_id, requester_issue = requester_matches.get(requester_name, (None, "申请人未匹配"))
    issue = "；".join(item for item in (row.data_issue, requester_issue) if item)
    version_id, steps = workflow_context or await _photo_workflow(cur)
    first_config = _step_config(steps[0][4])
    queue = str(first_config.get("queue") or steps[0][2] or "基础管控")
    status = "completed" if completed else ("pending_requester" if row.data_issue else "queued")
    result_status = "pending"
    result_note = ""
    if completed:
        result_status, result_note = historical_result(row.values["处理状态"])
    due_hours = steps[0][3]
    due_at = None if completed else datetime.utcnow() + timedelta(hours=int(due_hours or 24))
    form_data = {
        "subject_type": "external_photo_sheet", "subject_id": f"row:{row.physical_row}",
        "subject_name": row.values["对象姓名"], "identity_number": row.identity_number,
        "request_reason": "", "source_parser_type": "", "source_row_key": "",
    }
    await cur.execute(
        "INSERT INTO work_orders (ticket_no, type_code, workflow_version_id, title, description, "
        "requester_user_id, current_queue, status, priority, due_at, submitted_at, completed_at, form_data) "
        "VALUES ('PENDING','photo_request',%s,%s,'',%s,%s,%s,'normal',%s,UTC_TIMESTAMP(),%s,%s)",
        (version_id, f"{row.values['对象姓名'] or '未命名对象'}照片调取"[:200], requester_id, queue,
         status, due_at, datetime.utcnow() if completed else None, json.dumps(form_data, ensure_ascii=False)),
    )
    ticket_id = int(cur.lastrowid)
    ticket_no = f"PHOTO-{datetime.utcnow():%Y%m%d}-{ticket_id:06d}"
    await cur.execute("UPDATE work_orders SET ticket_no=%s WHERE id=%s", (ticket_no, ticket_id))
    for index, step in enumerate(steps):
        config = _step_config(step[4])
        step_queue = str(config.get("queue") or step[2] or "")
        step_status = "completed" if completed else (status if index == 0 else "pending")
        await cur.execute(
            "INSERT INTO work_order_steps (work_order_id, workflow_step_id, step_order, status, queue, due_at, "
            "decision, decision_note, decided_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (ticket_id, step[0], step[1], step_status, step_queue, due_at if index == 0 else None,
             "external_batch" if completed else "", result_note if completed else "",
             datetime.utcnow() if completed else None),
        )
    await cur.execute(
        "INSERT INTO photo_request_details (work_order_id, subject_type, subject_id, subject_name, identity_number, "
        "identity_hmac, identity_hmac_version, community_name, source_label, requester_name_snapshot, requested_at, "
        "external_origin, external_sync_status, legacy_result_note, data_issue, request_reason, result_status, result_note) "
        "VALUES (%s,'external_photo_sheet',%s,%s,%s,%s,%s,%s,%s,%s,%s,'tencent',%s,%s,%s,'',%s,%s)",
        (ticket_id, f"row:{row.physical_row}", row.values["对象姓名"][:100], row.identity_number or None,
         row.identity_hmac or None, row.identity_hmac_version, row.values["任务社区"][:200],
         row.values["数据来源"][:200], row.values["申请人员"][:100], row.requested_at,
         "linked", row.values["处理状态"][:2000], issue[:500], result_status, result_note),
    )
    await cur.execute(
        "INSERT INTO photo_sheet_rows (source_id, work_order_id, physical_row, row_hash, row_fingerprint, origin, "
        "is_legacy, batch_id, sync_status, g_value, last_seen_at) VALUES (%s,%s,%s,%s,%s,'tencent',%s,%s,'linked',%s,UTC_TIMESTAMP())",
        (source_id, ticket_id, row.physical_row, row.row_hash, row.fingerprint, int(legacy), batch_id,
         row.values["处理状态"][:2000]),
    )
    await cur.execute(
        "INSERT INTO work_order_events (work_order_id, event_type, actor_user_id, from_status, to_status, detail_json) "
        "VALUES (%s,'external_import',NULL,'',%s,%s)",
        (ticket_id, status, json.dumps({"source": SOURCE_CODE, "physical_row": row.physical_row,
                                       "legacy": legacy, "has_issue": bool(issue)}, ensure_ascii=False)),
    )
    if notify_queue and status == "queued":
        recipients = await queue_user_ids(cur, queue)
        await workflow_notification(cur, user_ids=recipients, ticket_id=ticket_id,
                                    event_key=f"external_submit_{ticket_id}", title="有新的照片调取申请",
                                    content=f"工单 {ticket_no} 已进入“{queue}”队列。")
    return ticket_id


async def _complete_before_marker(cur, source_id: int, batch_id: int, marker: ParsedRow) -> int:
    await cur.execute(
        "SELECT map.work_order_id, order_row.requester_user_id, order_row.status FROM photo_sheet_rows map "
        "JOIN work_orders order_row ON order_row.id=map.work_order_id "
        "WHERE map.source_id=%s AND map.physical_row<%s AND map.batch_id IS NULL FOR UPDATE",
        (source_id, marker.physical_row),
    )
    rows = await cur.fetchall()
    completed = 0
    for ticket_id, requester_id, status in rows:
        await cur.execute("UPDATE photo_sheet_rows SET batch_id=%s WHERE work_order_id=%s", (batch_id, ticket_id))
        if status in TERMINAL_STATUSES:
            continue
        await cur.execute(
            "UPDATE work_orders SET status='completed', current_assignee_user_id=NULL, completed_at=UTC_TIMESTAMP(), "
            "due_at=NULL, version_no=version_no+1 WHERE id=%s", (ticket_id,),
        )
        await cur.execute(
            "UPDATE work_order_steps SET status='completed', decision='external_batch', "
            "decision_note='腾讯批次完成、平台无附件', decided_at=UTC_TIMESTAMP(), version_no=version_no+1 "
            "WHERE work_order_id=%s AND status NOT IN ('completed','approved','rejected','cancelled')", (ticket_id,),
        )
        await cur.execute(
            "UPDATE photo_request_details SET result_status='found', result_note='腾讯批次完成、平台无附件' "
            "WHERE work_order_id=%s", (ticket_id,),
        )
        await cur.execute(
            "INSERT INTO work_order_events (work_order_id,event_type,actor_user_id,from_status,to_status,detail_json) "
            "VALUES (%s,'external_batch_complete',NULL,%s,'completed',%s)",
            (ticket_id, status, json.dumps({"batch_row": marker.physical_row}, ensure_ascii=False)),
        )
        if requester_id:
            await workflow_notification(cur, user_ids=[int(requester_id)], ticket_id=int(ticket_id),
                                        event_key=f"external_batch_{batch_id}", title="照片调取批次已完成",
                                        content=f"工单 #{ticket_id} 已由腾讯名单批次标记完成。")
        completed += 1
    return completed


async def _upsert_marker(cur, source_id: int, row: ParsedRow, legacy: bool) -> tuple[int, bool]:
    await cur.execute(
        "SELECT id,physical_row FROM photo_sheet_batches WHERE source_id=%s AND marker_hash=%s ORDER BY id LIMIT 2",
        (source_id, row.fingerprint),
    )
    existing_rows = await cur.fetchall()
    if len(existing_rows) == 1:
        await cur.execute(
            "UPDATE photo_sheet_batches SET physical_row=%s,marker_text=%s,completed_at=%s,time_inferred=%s "
            "WHERE id=%s", (row.physical_row, row.values[COLUMNS[0]][:100], row.marker_at,
                            int(row.time_inferred), existing_rows[0][0]),
        )
        return int(existing_rows[0][0]), False
    if len(existing_rows) > 1:
        raise RuntimeError("批次边界重复，无法安全定位")
    await cur.execute(
        "INSERT INTO photo_sheet_batches (source_id, physical_row, marker_text, marker_hash, completed_at, "
        "time_inferred, is_legacy) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (source_id, row.physical_row, row.values[COLUMNS[0]][:100], row.fingerprint, row.marker_at,
         int(row.time_inferred), int(legacy)),
    )
    return int(cur.lastrowid), True


async def import_online(cur, *, expected_token: str, actor_user_id: int) -> dict:
    source = await load_source(cur)
    parsed = parse_rows(await read_online_rows(source))
    summary = preview_summary(parsed)
    if summary["preview_token"] != expected_token:
        raise ValueError("腾讯名单已发生变化，请重新预览后再导入")
    if source["import_applied_at"]:
        return {**summary, "created_tickets": 0, "message": "历史名单已经导入，本次未重复创建工单"}
    markers = [row for row in parsed if row.kind == "marker"]
    workflow_context = await _photo_workflow(cur)
    requester_matches = await _requester_map(cur)
    marker_ids: dict[int, int] = {}
    for marker in markers:
        marker_ids[marker.physical_row], _ = await _upsert_marker(cur, source["id"], marker, True)
    await cur.execute("SELECT physical_row FROM photo_sheet_rows WHERE source_id=%s", (source["id"],))
    existing_rows = {int(item[0]) for item in await cur.fetchall() if item[0] is not None}
    created = 0
    for row in (item for item in parsed if item.kind == "request"):
        if row.physical_row in existing_rows:
            continue
        next_marker = next((marker for marker in markers if marker.physical_row > row.physical_row), None)
        await _create_external_ticket(
            cur, source_id=source["id"], row=row,
            batch_id=marker_ids.get(next_marker.physical_row) if next_marker else None,
            completed=next_marker is not None, legacy=True, notify_queue=False,
            workflow_context=workflow_context, requester_matches=requester_matches,
        )
        created += 1
    await cur.execute(
        "UPDATE photo_sheet_sources SET import_applied_at=UTC_TIMESTAMP(), legacy_cutoff_row=%s, "
        "last_cursor_row=%s, last_sync_at=UTC_TIMESTAMP(), last_sync_status='success', last_error='', updated_by=%s "
        "WHERE id=%s",
        (max((row.physical_row for row in parsed), default=source["header_row"]),
         max((row.physical_row for row in parsed), default=source["header_row"]), actor_user_id, source["id"]),
    )
    return {**summary, "created_tickets": created, "message": "历史名单已导入"}


async def enqueue_outbox(cur, ticket_id: int, action: str) -> None:
    await cur.execute("SELECT id FROM photo_sheet_sources WHERE source_code=%s", (SOURCE_CODE,))
    source = await cur.fetchone()
    if not source:
        return
    await cur.execute(
        "INSERT INTO photo_sheet_outbox (source_id, work_order_id, action) VALUES (%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE status=IF(status='done','done','pending'), next_attempt_at=NULL",
        (source[0], ticket_id, action),
    )
    await cur.execute(
        "UPDATE photo_request_details SET external_sync_status='pending' WHERE work_order_id=%s",
        (ticket_id,),
    )


async def _ticket_values(cur, ticket_id: int) -> dict:
    await cur.execute(
        "SELECT detail.community_name, detail.source_label, detail.subject_name, detail.identity_number, "
        "detail.requester_name_snapshot, detail.requested_at, detail.external_origin "
        "FROM photo_request_details detail WHERE detail.work_order_id=%s", (ticket_id,),
    )
    row = await cur.fetchone()
    if not row:
        raise RuntimeError("照片工单详情不存在")
    requested_at = row[5] or datetime.utcnow()
    return {
        "任务社区": _text(row[0]), "数据来源": _text(row[1]), "对象姓名": _text(row[2]),
        "身份证号": _text(row[3]), "申请人员": _text(row[4]),
        "申请日期": f"{requested_at.year}/{requested_at.month}/{requested_at.day}",
        "处理状态": "", "external_origin": _text(row[6]),
    }


async def _locate_mapping(client: TxDocsClient, source: dict, cur, ticket_id: int) -> tuple[int | None, dict | None]:
    await cur.execute(
        "SELECT physical_row, row_fingerprint FROM photo_sheet_rows WHERE work_order_id=%s", (ticket_id,),
    )
    mapping = await cur.fetchone()
    if mapping and mapping[0]:
        actual = await client.read_source_row(source["file_id"], source["sheet_id"], int(mapping[0]), COLUMNS)
        values = {column: _text(actual["values"].get(column)) for column in COLUMNS}
        if _digest([values[column] for column in COLUMNS[:6]]) == str(mapping[1]):
            return int(mapping[0]), actual
    if not mapping:
        return None, None
    rows = await client.read_all_source_rows(source["file_id"], source["sheet_id"], source["header_row"], COLUMNS)
    candidates = []
    for raw in rows:
        values = {column: _text(raw["values"].get(column)) for column in COLUMNS}
        if _digest([values[column] for column in COLUMNS[:6]]) == str(mapping[1]):
            candidates.append(raw)
    if len(candidates) == 1:
        return int(candidates[0]["physical_row"]), candidates[0]
    return None, None


async def _process_append(
    client: TxDocsClient,
    source: dict,
    cur,
    ticket_id: int,
    known_rows: list[dict] | None = None,
) -> None:
    await cur.execute("SELECT physical_row FROM photo_sheet_rows WHERE work_order_id=%s", (ticket_id,))
    if await cur.fetchone():
        return
    values = await _ticket_values(cur, ticket_id)
    fingerprint = _digest([values[column] for column in COLUMNS[:6]])
    await cur.execute(
        "SELECT physical_row FROM photo_sheet_rows WHERE source_id=%s AND physical_row IS NOT NULL",
        (source["id"],),
    )
    occupied_rows = {int(item[0]) for item in await cur.fetchall()}
    rows = known_rows if known_rows is not None else await client.read_all_source_rows(
        source["file_id"], source["sheet_id"], source["header_row"], COLUMNS,
    )
    candidates = []
    for raw in rows:
        existing = {column: _text(raw["values"].get(column)) for column in COLUMNS}
        if (
            int(raw["physical_row"]) not in occupied_rows
            and _digest([existing[column] for column in COLUMNS[:6]]) == fingerprint
        ):
            candidates.append(raw)
    if len(candidates) > 1:
        raise RuntimeError("发现多个相同候选行，无法安全确认写入结果")
    if candidates:
        physical_row = int(candidates[0]["physical_row"])
        verified = candidates[0]
    else:
        physical_row = max([source["header_row"], *[int(row["physical_row"]) for row in rows]]) + 1
        request = client.build_update_range_request(
            source["sheet_id"], physical_row - 1, 0, [[values[column] for column in COLUMNS]],
        )
        await client.batch_update(source["file_id"], [request])
        verified = await client.read_source_row(source["file_id"], source["sheet_id"], physical_row, COLUMNS)
    verified_values = {column: _text(verified["values"].get(column)) for column in COLUMNS}
    if any(verified_values[column] != values[column] for column in COLUMNS[:6]):
        raise RuntimeError("腾讯新增行回读不一致")
    if known_rows is not None and not candidates:
        known_rows.append(verified)
    await cur.execute(
        "INSERT INTO photo_sheet_rows (source_id,work_order_id,physical_row,row_hash,row_fingerprint,origin,is_legacy," 
        "sync_status,g_value,last_seen_at) VALUES (%s,%s,%s,%s,%s,'platform',0,'linked',%s,UTC_TIMESTAMP())",
        (source["id"], ticket_id, physical_row,
         _digest([str(physical_row), *[verified_values[column] for column in COLUMNS]]), fingerprint,
         verified_values["处理状态"][:2000]),
    )


async def _process_completed(client: TxDocsClient, source: dict, cur, ticket_id: int) -> None:
    physical_row, actual = await _locate_mapping(client, source, cur, ticket_id)
    if not physical_row or not actual:
        raise RuntimeError("无法唯一定位腾讯来源行")
    before = {column: _text(actual["values"].get(column)) for column in COLUMNS}
    if before["处理状态"] == COMPLETED_MARK:
        return
    request = client.build_update_cell_request(source["sheet_id"], physical_row, 6, COMPLETED_MARK,
                                               (actual.get("cell_meta") or {}).get("处理状态"), "处理状态")
    await client.batch_update(source["file_id"], [request])
    verified = await client.read_source_row(source["file_id"], source["sheet_id"], physical_row, COLUMNS)
    if _text(verified["values"].get("处理状态")) != COMPLETED_MARK:
        raise RuntimeError("腾讯 G 列写后回读不一致")
    await cur.execute(
        "UPDATE photo_sheet_rows SET physical_row=%s,g_value=%s,sync_status='linked',last_error='',last_seen_at=UTC_TIMESTAMP() "
        "WHERE work_order_id=%s", (physical_row, COMPLETED_MARK, ticket_id),
    )


async def _process_outbox_once(limit: int = 20) -> dict:
    pool = db_manager.get_pool("workflow")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            source = await load_source(cur)
            if not source["write_enabled"] or not await _global_writeback_enabled():
                return {"processed": 0, "failed": 0, "disabled": True}
    client = await _oauth_client()
    processed = failed = 0
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id,work_order_id,action FROM photo_sheet_outbox WHERE status IN ('pending','retry') "
                    "AND (next_attempt_at IS NULL OR next_attempt_at<=UTC_TIMESTAMP()) ORDER BY id LIMIT %s", (limit,),
                )
                jobs = await cur.fetchall()
            append_rows_cache: list[dict] | None = None
            for outbox_id, ticket_id, action in jobs:
                await conn.begin()
                try:
                    async with conn.cursor() as cur:
                        await cur.execute("SELECT status FROM photo_sheet_outbox WHERE id=%s FOR UPDATE", (outbox_id,))
                        state = await cur.fetchone()
                        if not state or state[0] not in {"pending", "retry"}:
                            await conn.rollback()
                            continue
                        if action == "append_request":
                            if append_rows_cache is None:
                                append_rows_cache = await client.read_all_source_rows(
                                    source["file_id"], source["sheet_id"], source["header_row"], COLUMNS,
                                )
                            await _process_append(
                                client, source, cur, int(ticket_id), known_rows=append_rows_cache,
                            )
                        elif action == "mark_completed":
                            await _process_completed(client, source, cur, int(ticket_id))
                        else:
                            raise RuntimeError("未知照片名单写回动作")
                        await cur.execute("UPDATE photo_sheet_outbox SET status='done',last_error='',error_code='' WHERE id=%s", (outbox_id,))
                        await cur.execute("UPDATE photo_request_details SET external_sync_status='synced' WHERE work_order_id=%s", (ticket_id,))
                    await conn.commit()
                    processed += 1
                except Exception as exc:
                    append_rows_cache = None
                    await conn.rollback()
                    await conn.begin()
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE photo_sheet_outbox SET status='retry',attempt_count=attempt_count+1,next_attempt_at=DATE_ADD(UTC_TIMESTAMP(),INTERVAL 5 MINUTE),last_error=%s,error_code='write_failed' WHERE id=%s",
                            (str(exc)[:500], outbox_id),
                        )
                        await cur.execute("UPDATE photo_request_details SET external_sync_status='retry' WHERE work_order_id=%s", (ticket_id,))
                        if "唯一定位" in str(exc) or "多个相同" in str(exc):
                            await cur.execute(
                                "INSERT INTO photo_sheet_conflicts (source_id,work_order_id,conflict_type,safe_detail) "
                                "SELECT %s,%s,'row_location','无法唯一定位腾讯来源行' FROM DUAL WHERE NOT EXISTS "
                                "(SELECT 1 FROM photo_sheet_conflicts WHERE work_order_id=%s AND conflict_type='row_location' AND status='pending')",
                                (source["id"], ticket_id, ticket_id),
                            )
                    await conn.commit()
                    failed += 1
    finally:
        await client.close()
    return {"processed": processed, "failed": failed, "disabled": False}


async def process_outbox_once(limit: int = 20) -> dict:
    async with PHOTO_SHEET_OPERATION_LOCK:
        return await _process_outbox_once(limit)


async def _sync_online_once(*, full: bool = False, actor_user_id: int | None = None) -> dict:
    pool = db_manager.get_pool("workflow")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            source = await load_source(cur)
            if not source["read_enabled"]:
                return {"created_tickets": 0, "completed_tickets": 0, "disabled": True}
        overlap_start = None
        if not full:
            overlap_start = max(source["header_row"] + 1, int(source["last_cursor_row"] or 1) - 200)
        try:
            parsed = parse_rows(await read_online_rows(source, start_row=overlap_start))
            if not full and not parsed:
                # 行数倒退到重叠窗口之前，或尾部结构异常时，不把空结果当作
                # “腾讯名单已清空”，立即改做一次完整只读重定位。
                full = True
                parsed = parse_rows(await read_online_rows(source))
        except Exception as exc:
            safe_error = f"{type(exc).__name__}: {str(exc)}"[:500]
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO photo_sheet_sync_runs (source_id,run_type,status,summary_json,error_message,finished_at) "
                    "VALUES (%s,%s,'failed',%s,%s,UTC_TIMESTAMP())",
                    (source["id"], "full" if full else "incremental", json.dumps({}, ensure_ascii=False), safe_error),
                )
                await cur.execute(
                    "UPDATE photo_sheet_sources SET last_sync_at=UTC_TIMESTAMP(),last_sync_status='failed',last_error=%s "
                    "WHERE id=%s", (safe_error, source["id"]),
                )
            raise
        summary = preview_summary(parsed)
        await conn.begin()
        created = completed = 0
        try:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO photo_sheet_sync_runs (source_id,run_type,status,summary_json) VALUES (%s,%s,'running',%s)",
                    (source["id"], "full" if full else "incremental", json.dumps({}, ensure_ascii=False)),
                )
                run_id = int(cur.lastrowid)
                unique_existing: dict[str, tuple[int, int | None]] = {}
                unique_incoming: dict[str, ParsedRow] = {}
                if full:
                    await cur.execute(
                        "UPDATE photo_sheet_batches SET physical_row=NULL WHERE source_id=%s",
                        (source["id"],),
                    )
                    await cur.execute(
                        "SELECT id,work_order_id,row_fingerprint FROM photo_sheet_rows WHERE source_id=%s",
                        (source["id"],),
                    )
                    existing_groups: dict[str, list[tuple[int, int]]] = {}
                    for map_id, work_order_id, fingerprint in await cur.fetchall():
                        existing_groups.setdefault(str(fingerprint), []).append((int(map_id), int(work_order_id)))
                    incoming_groups: dict[str, list[ParsedRow]] = {}
                    for item in parsed:
                        if item.kind == "request":
                            incoming_groups.setdefault(item.fingerprint, []).append(item)
                    unique_existing = {
                        fingerprint: (items[0][0], items[0][1])
                        for fingerprint, items in existing_groups.items() if len(items) == 1
                    }
                    unique_incoming = {
                        fingerprint: items[0]
                        for fingerprint, items in incoming_groups.items() if len(items) == 1
                    }
                    relocating_ids = [
                        mapping[0] for fingerprint, mapping in unique_existing.items()
                        if fingerprint in unique_incoming
                    ]
                    if relocating_ids:
                        placeholders = ",".join(["%s"] * len(relocating_ids))
                        await cur.execute(
                            f"UPDATE photo_sheet_rows SET physical_row=NULL WHERE id IN ({placeholders})",
                            tuple(relocating_ids),
                        )
                for row in parsed:
                    if row.kind == "marker":
                        batch_id, inserted = await _upsert_marker(cur, source["id"], row, False)
                        if inserted:
                            completed += await _complete_before_marker(cur, source["id"], batch_id, row)
                        continue
                    existing = None
                    physical_conflict = False
                    if full and row.fingerprint in unique_existing and row.fingerprint in unique_incoming:
                        existing = (unique_existing[row.fingerprint][1], "")
                    else:
                        await cur.execute(
                            "SELECT work_order_id,row_hash,row_fingerprint FROM photo_sheet_rows "
                            "WHERE source_id=%s AND physical_row=%s",
                            (source["id"], row.physical_row),
                        )
                        exact = await cur.fetchone()
                        if exact and str(exact[2]) == row.fingerprint:
                            existing = exact
                        elif exact:
                            physical_conflict = True
                    if existing:
                        await cur.execute(
                            "UPDATE photo_sheet_rows SET physical_row=%s,row_hash=%s,row_fingerprint=%s,g_value=%s,"
                            "last_seen_at=UTC_TIMESTAMP(),sync_status='linked',last_error='' WHERE work_order_id=%s",
                            (row.physical_row, row.row_hash, row.fingerprint, row.values["处理状态"][:2000], existing[0]),
                        )
                        continue
                    if physical_conflict:
                        await cur.execute(
                            "INSERT INTO photo_sheet_conflicts (source_id,physical_row,conflict_type,safe_detail) "
                            "SELECT %s,%s,'row_changed','物理行内容变化，等待完整重定位' FROM DUAL WHERE NOT EXISTS "
                            "(SELECT 1 FROM photo_sheet_conflicts WHERE source_id=%s AND physical_row=%s "
                            "AND conflict_type='row_changed' AND status='pending')",
                            (source["id"], row.physical_row, source["id"], row.physical_row),
                        )
                        continue
                    await _create_external_ticket(cur, source_id=source["id"], row=row, batch_id=None,
                                                  completed=False, legacy=False, notify_queue=True)
                    created += 1
                parsed_cursor = max((row.physical_row for row in parsed), default=source["header_row"])
                cursor = parsed_cursor if full else max(
                    int(source["last_cursor_row"] or source["header_row"]), parsed_cursor,
                )
                await cur.execute(
                    "UPDATE photo_sheet_sources SET last_cursor_row=%s,last_sync_at=UTC_TIMESTAMP(),last_sync_status='success',last_error='',updated_by=COALESCE(%s,updated_by),last_full_sync_date=IF(%s,CURDATE(),last_full_sync_date) WHERE id=%s",
                    (cursor, actor_user_id, int(full), source["id"]),
                )
                await cur.execute(
                    "UPDATE photo_sheet_sync_runs SET status='success',rows_read=%s,requests_found=%s,markers_found=%s,created_tickets=%s,completed_tickets=%s,issue_count=%s,summary_json=%s,finished_at=UTC_TIMESTAMP() WHERE id=%s",
                    (summary["rows_read"], summary["requests"], summary["markers"], created, completed,
                     summary["issue_count"], json.dumps(summary, ensure_ascii=False), run_id),
                )
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    return {**summary, "created_tickets": created, "completed_tickets": completed, "disabled": False}


async def sync_online_once(*, full: bool = False, actor_user_id: int | None = None) -> dict:
    async with PHOTO_SHEET_OPERATION_LOCK:
        return await _sync_online_once(full=full, actor_user_id=actor_user_id)


async def run_photo_sheet_maintenance_once() -> dict:
    outbox = await process_outbox_once()
    pool = db_manager.get_pool("workflow")
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            source = await load_source(cur)
            do_full = source["last_full_sync_date"] != date.today()
    sync = await sync_online_once(full=do_full)
    return {"outbox": outbox, "sync": sync}
