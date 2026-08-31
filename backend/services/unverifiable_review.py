"""“无法核实”两级研判、延时复核和最终归档状态机。"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from typing import Any

from services.business_time import get_business_date
from services.online_source import active_source_sql_filter, json_value, rebuild_projection
from services.task_workflow import TASK_WORKFLOWS


UNVERIFIABLE_REVIEW_TYPES = (
    "全链条",
    "出租房屋核查",
    "寄递业",
    "疑似返苏",
    "苏州涉警",
    "交通涉警",
)

INITIAL_PENDING = "initial_pending"
INITIAL_EXTENSION = "initial_extension"
DEEP_PENDING = "deep_pending"
DEEP_EXTENSION = "deep_extension"
FINAL_UNVERIFIABLE = "final_unverifiable"
RESOLVED = "resolved"
ARCHIVED = "archived"
SOURCE_EXCEPTION = "source_exception"

ACTIVE_STATES = {
    INITIAL_PENDING,
    INITIAL_EXTENSION,
    DEEP_PENDING,
    DEEP_EXTENSION,
    FINAL_UNVERIFIABLE,
    SOURCE_EXCEPTION,
}
PENDING_DECISION_STATES = {INITIAL_PENDING, DEEP_PENDING}
EXTENSION_STATES = {INITIAL_EXTENSION, DEEP_EXTENSION}

STATE_LABELS = {
    INITIAL_PENDING: "初步待研判",
    INITIAL_EXTENSION: "初步延时复核中",
    DEEP_PENDING: "深度待研判",
    DEEP_EXTENSION: "深度延时复核中",
    FINAL_UNVERIFIABLE: "最终无法核实",
    RESOLVED: "已核实结束",
    ARCHIVED: "已归档",
    SOURCE_EXCEPTION: "来源异常",
}

_WAKE_EVENT = asyncio.Event()


def supports_unverifiable_review(parser_type: str) -> bool:
    return parser_type in UNVERIFIABLE_REVIEW_TYPES


def is_unverifiable_result(value: Any) -> bool:
    return "无法核实" in str(value or "").strip()


def review_due_date(business_date: date, state: str) -> date:
    """成功研判后的腾讯截止日期；按完整自然日计算。"""
    if state == INITIAL_PENDING:
        return business_date + timedelta(days=2)
    if state == DEEP_PENDING:
        return business_date + timedelta(days=1)
    raise ValueError("当前阶段不能进入延时复核")


def review_stage_for_state(state: str) -> str:
    return state if state in ACTIVE_STATES else ""


def _iso(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value.isoformat() + "Z"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _flow_payload(row: tuple | None) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": int(row[0]),
        "parser_type": str(row[1]),
        "row_key": str(row[2]),
        "cycle_no": int(row[3]),
        "source_id": int(row[4]) if row[4] is not None else None,
        "source_revision": int(row[5] or 0),
        "source_row_hash": str(row[6] or ""),
        "state": str(row[7]),
        "state_label": STATE_LABELS.get(str(row[7]), str(row[7])),
        "flow_version": int(row[8] or 1),
        "review_due_date": _iso(row[9]),
        "original_deadline": str(row[10] or ""),
        "previous_deadline": str(row[11] or ""),
        "feedback_submitted": bool(row[12]),
        "safe_reason_code": str(row[13] or ""),
        "last_action_at": _iso(row[14]),
        "resolved_at": _iso(row[15]),
        "finalized_at": _iso(row[16]),
        "archived_at": _iso(row[17]),
        "updated_at": _iso(row[18]),
    }


FLOW_SELECT = """
    SELECT id,parser_type,row_key,cycle_no,source_id,source_revision,
           source_row_hash,state,flow_version,review_due_date,
           original_deadline,previous_deadline,feedback_submitted,
           safe_reason_code,last_action_at,resolved_at,finalized_at,
           archived_at,updated_at
    FROM _unverifiable_review_flows
"""


async def ensure_unverifiable_review_schema(cur) -> None:
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _unverifiable_review_flows (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            parser_type VARCHAR(50) NOT NULL,
            row_key CHAR(32) NOT NULL,
            cycle_no INT NOT NULL,
            source_id BIGINT DEFAULT NULL,
            source_revision BIGINT NOT NULL DEFAULT 0,
            source_row_hash CHAR(64) NOT NULL DEFAULT '',
            state VARCHAR(40) NOT NULL DEFAULT 'initial_pending',
            flow_version BIGINT NOT NULL DEFAULT 1,
            review_due_date DATE DEFAULT NULL,
            original_deadline VARCHAR(100) NOT NULL DEFAULT '',
            previous_deadline VARCHAR(100) NOT NULL DEFAULT '',
            feedback_submitted TINYINT(1) NOT NULL DEFAULT 0,
            safe_reason_code VARCHAR(100) NOT NULL DEFAULT '',
            created_by INT DEFAULT NULL,
            last_actor_id INT DEFAULT NULL,
            last_action_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            resolved_at DATETIME DEFAULT NULL,
            finalized_at DATETIME DEFAULT NULL,
            archived_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_unverifiable_cycle (parser_type,row_key,cycle_no),
            INDEX idx_unverifiable_state (state,review_due_date),
            INDEX idx_unverifiable_source (source_id,state),
            INDEX idx_unverifiable_row (parser_type,row_key,cycle_no)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _unverifiable_review_events (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            flow_id BIGINT NOT NULL,
            stage VARCHAR(30) NOT NULL DEFAULT '',
            action VARCHAR(50) NOT NULL,
            outcome VARCHAR(30) NOT NULL DEFAULT '',
            protected_text TEXT DEFAULT NULL,
            actor_user_id INT DEFAULT NULL,
            automatic TINYINT(1) NOT NULL DEFAULT 0,
            source_revision BIGINT NOT NULL DEFAULT 0,
            source_row_hash CHAR(64) NOT NULL DEFAULT '',
            safe_reason_code VARCHAR(100) NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_unverifiable_event_flow (flow_id,id),
            INDEX idx_unverifiable_event_action (action,created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
          COLLATE=utf8mb4_unicode_ci
    """)


async def _latest_flow(cur, parser_type: str, row_key: str, *, for_update: bool = False):
    await cur.execute(
        FLOW_SELECT
        + " WHERE parser_type=%s AND row_key=%s ORDER BY cycle_no DESC LIMIT 1"
        + (" FOR UPDATE" if for_update else ""),
        (parser_type, row_key),
    )
    return await cur.fetchone()


async def review_flows_by_rows(
    cur,
    rows: list[tuple[str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not rows:
        return {}
    pairs = list(dict.fromkeys((str(p), str(k)) for p, k in rows))
    clauses = " OR ".join("(parser_type=%s AND row_key=%s)" for _ in pairs)
    params = [value for pair in pairs for value in pair]
    await cur.execute(
        FLOW_SELECT
        + f" WHERE ({clauses}) ORDER BY parser_type,row_key,cycle_no DESC",
        params,
    )
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in await cur.fetchall():
        # A few lightweight callers/tests use cursors that return their last
        # result set for every query.  Ignore malformed rows rather than
        # treating an online-source row as a flow record; production flow rows
        # always contain the full projection selected above.
        if len(row) < 19:
            continue
        key = (str(row[1]), str(row[2]))
        result.setdefault(key, _flow_payload(row) or {})
    return result


async def review_events_for_flow(cur, flow_id: int) -> list[dict[str, Any]]:
    await cur.execute("""
        SELECT stage,action,outcome,protected_text,actor_user_id,automatic,
               safe_reason_code,created_at
        FROM _unverifiable_review_events
        WHERE flow_id=%s ORDER BY id
    """, (flow_id,))
    return [
        {
            "stage": str(row[0] or ""),
            "action": str(row[1] or ""),
            "outcome": str(row[2] or ""),
            "text": str(row[3] or ""),
            "actor_user_id": row[4],
            "automatic": bool(row[5]),
            "safe_reason_code": str(row[6] or ""),
            "created_at": _iso(row[7]),
        }
        for row in await cur.fetchall()
    ]


async def review_events_by_flow_ids(
    cur,
    flow_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    if not flow_ids:
        return {}
    unique_ids = list(dict.fromkeys(int(value) for value in flow_ids))
    placeholders = ",".join(["%s"] * len(unique_ids))
    await cur.execute(
        f"""
        SELECT event.flow_id,event.stage,event.action,event.outcome,
               event.protected_text,event.actor_user_id,event.automatic,
               event.safe_reason_code,event.created_at,
               COALESCE(NULLIF(member.name,''),NULLIF(account.display_name,''),
                        account.username,'') AS actor_name
        FROM _unverifiable_review_events event
        LEFT JOIN _users account ON account.id=event.actor_user_id
        LEFT JOIN _grid_members member ON member.id=account.member_id
        WHERE event.flow_id IN ({placeholders})
        ORDER BY event.flow_id,event.id
        """,
        unique_ids,
    )
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in await cur.fetchall():
        grouped.setdefault(int(row[0]), []).append({
            "stage": str(row[1] or ""),
            "action": str(row[2] or ""),
            "outcome": str(row[3] or ""),
            "text": str(row[4] or ""),
            "actor_user_id": row[5],
            "automatic": bool(row[6]),
            "safe_reason_code": str(row[7] or ""),
            "created_at": _iso(row[8]),
            "actor_name": str(row[9] or ""),
        })
    return grouped


def review_export_fields(events: list[dict[str, Any]]) -> dict[str, str]:
    """把完整事件历史整理为归档工作簿的固定列，不丢失重复反馈。"""
    result = {
        "initial_review": "", "initial_actor": "", "initial_at": "",
        "first_feedback": "", "first_feedback_actor": "", "first_feedback_at": "",
        "deep_review": "", "deep_actor": "", "deep_at": "",
        "second_feedback": "", "second_feedback_actor": "", "second_feedback_at": "",
        "automatic_events": "",
    }

    def joined(items: list[dict[str, Any]], field: str) -> str:
        return "；".join(str(item.get(field) or "").strip() for item in items if str(item.get(field) or "").strip())

    initial = [item for item in events if item["action"] == "review_decision" and item["stage"] == INITIAL_PENDING]
    deep = [item for item in events if item["action"] == "review_decision" and item["stage"] == DEEP_PENDING]
    first_feedback = [item for item in events if item["action"] == "feedback_recorded" and item["stage"] == INITIAL_EXTENSION]
    second_feedback = [item for item in events if item["action"] == "feedback_recorded" and item["stage"] == DEEP_EXTENSION]
    automatic = [item for item in events if item.get("automatic") and item["action"] != "legacy_unverifiable_backfill"]

    for prefix, items in (("initial", initial), ("deep", deep)):
        if items:
            latest = items[-1]
            outcome = "研判成功" if latest["outcome"] == "success" else "研判失败"
            result[f"{prefix}_review"] = f"{outcome}：{latest['text']}"
            result[f"{prefix}_actor"] = str(latest.get("actor_name") or "")
            result[f"{prefix}_at"] = str(latest.get("created_at") or "")
    for prefix, items in (("first_feedback", first_feedback), ("second_feedback", second_feedback)):
        result[prefix] = joined(items, "text")
        result[f"{prefix}_actor"] = joined(items, "actor_name")
        result[f"{prefix}_at"] = joined(items, "created_at")
    action_labels = {
        "overdue_auto_transition": "逾期自动流转",
        "automatic_transition_paused": "来源异常，自动流转暂停",
        "formal_result_detected": "检测到正式核查结果，流程结束",
        "archive_exported": "导出归档完成",
    }
    result["automatic_events"] = "；".join(
        f"{item.get('created_at') or ''} {action_labels.get(item['action'], item['action'])}".strip()
        for item in automatic
    )
    return result


async def audit_missing_unverifiable_flows(cur) -> list[dict[str, Any]]:
    """只读查找上线前已经存在、但尚未建立结构化流程的无法核实任务。"""
    placeholders = ",".join(["%s"] * len(UNVERIFIABLE_REVIEW_TYPES))
    active_filter = active_source_sql_filter("", "source")
    await cur.execute(
        f"""
        SELECT projection.parser_type,projection.row_key,projection.values_json,
               (
                 SELECT COUNT(*) FROM _online_source_rows source
                 WHERE source.parser_type=projection.parser_type
                   AND source.row_key=projection.row_key{active_filter}
               ) AS active_source_count,
               (
                 SELECT COUNT(DISTINCT source.row_hash) FROM _online_source_rows source
                 WHERE source.parser_type=projection.parser_type
                   AND source.row_key=projection.row_key{active_filter}
               ) AS active_hash_count
        FROM _online_source_projection projection
        WHERE projection.parser_type IN ({placeholders})
          AND NOT EXISTS (
              SELECT 1 FROM _unverifiable_review_flows flow
              WHERE flow.parser_type=projection.parser_type
                AND flow.row_key=projection.row_key
          )
        ORDER BY projection.parser_type,projection.row_key
        """,
        UNVERIFIABLE_REVIEW_TYPES,
    )
    missing: list[dict[str, Any]] = []
    for parser_type, row_key, values_json, source_count, active_hash_count in await cur.fetchall():
        parser_type = str(parser_type)
        workflow = TASK_WORKFLOWS.get(parser_type)
        values = json_value(values_json, {})
        if not workflow or not is_unverifiable_result(values.get(workflow.result_field)):
            continue
        missing.append({
            "parser_type": parser_type,
            "row_key": str(row_key),
            "values": values,
            "source_count": int(source_count or 0),
            "conflict": int(active_hash_count or 0) > 1,
        })
    return missing


async def _backfill_missing_unverifiable_flows_in_connection(conn) -> dict[str, int]:
    created = exceptions = 0
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            missing = await audit_missing_unverifiable_flows(cur)
            for item in missing:
                parser_type = item["parser_type"]
                row_key = item["row_key"]
                values = item["values"]
                workflow = TASK_WORKFLOWS[parser_type]
                await cur.execute(
                    f"""
                    SELECT id,revision,row_hash
                    FROM _online_source_rows source
                    WHERE parser_type=%s AND row_key=%s
                    {active_source_sql_filter(parser_type, 'source')}
                    ORDER BY id
                    """,
                    (parser_type, row_key),
                )
                source_rows = await cur.fetchall()
                source_safe = (
                    item["source_count"] == 1
                    and not item["conflict"]
                    and len(source_rows) == 1
                )
                source_id = int(source_rows[0][0]) if source_safe else None
                source_revision = int(source_rows[0][1] or 0) if source_safe else 0
                source_row_hash = str(source_rows[0][2] or "") if source_safe else ""
                state = INITIAL_PENDING if source_safe else SOURCE_EXCEPTION
                safe_reason = "" if source_safe else "legacy_source_context_invalid"
                deadline_field = workflow.date_fields[0] if workflow.date_fields else ""
                original_deadline = str(values.get(deadline_field) or "")
                await cur.execute(
                    """
                    INSERT IGNORE INTO _unverifiable_review_flows
                    (parser_type,row_key,cycle_no,source_id,source_revision,
                     source_row_hash,state,original_deadline,previous_deadline,
                     safe_reason_code,last_action_at)
                    VALUES (%s,%s,1,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP())
                    """,
                    (
                        parser_type, row_key, source_id, source_revision,
                        source_row_hash, state, original_deadline,
                        original_deadline, safe_reason,
                    ),
                )
                if cur.rowcount != 1:
                    continue
                flow_id = int(cur.lastrowid)
                legacy_analysis = workflow.first_value(values, workflow.analysis_fields)
                await _event(
                    cur, flow_id=flow_id, stage=state,
                    action="legacy_unverifiable_backfill", text=legacy_analysis,
                    automatic=True, source_revision=source_revision,
                    source_row_hash=source_row_hash,
                    safe_reason_code=safe_reason,
                )
                if source_safe:
                    created += 1
                else:
                    exceptions += 1
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {"initial_pending": created, "source_exception": exceptions}


async def backfill_missing_unverifiable_flows(conn=None) -> dict[str, int]:
    """幂等接管历史无法核实任务，不根据旧自由文字猜测研判阶段。"""
    if conn is not None:
        return await _backfill_missing_unverifiable_flows_in_connection(conn)
    from database import db_manager

    pool = db_manager.get_pool("online_data")
    async with pool.acquire() as pooled_conn:
        return await _backfill_missing_unverifiable_flows_in_connection(pooled_conn)


async def _event(
    cur,
    *,
    flow_id: int,
    stage: str,
    action: str,
    outcome: str = "",
    text: str = "",
    actor_user_id: int | None = None,
    automatic: bool = False,
    source_revision: int = 0,
    source_row_hash: str = "",
    safe_reason_code: str = "",
) -> None:
    await cur.execute("""
        INSERT INTO _unverifiable_review_events
        (flow_id,stage,action,outcome,protected_text,actor_user_id,automatic,
         source_revision,source_row_hash,safe_reason_code)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        flow_id, stage, action, outcome, text or None, actor_user_id,
        int(automatic), source_revision, source_row_hash, safe_reason_code,
    ))


async def ensure_flow_for_values(
    cur,
    *,
    parser_type: str,
    row_key: str,
    source_id: int,
    source_revision: int,
    source_row_hash: str,
    values: dict[str, Any],
    actor_user_id: int | None = None,
) -> dict[str, Any] | None:
    if not supports_unverifiable_review(parser_type):
        return None
    workflow = TASK_WORKFLOWS[parser_type]
    unable = is_unverifiable_result(values.get(workflow.result_field))
    latest = await _latest_flow(cur, parser_type, row_key, for_update=True)
    flow = _flow_payload(latest)
    if not unable:
        if flow and flow["state"] in ACTIVE_STATES:
            await cur.execute("""
                UPDATE _unverifiable_review_flows
                SET state=%s,flow_version=flow_version+1,resolved_at=UTC_TIMESTAMP(),
                    last_actor_id=%s,last_action_at=UTC_TIMESTAMP(),safe_reason_code=''
                WHERE id=%s
            """, (RESOLVED, actor_user_id, flow["id"]))
            await _event(
                cur, flow_id=flow["id"], stage=flow["state"],
                action="formal_result_submitted", actor_user_id=actor_user_id,
                source_revision=source_revision, source_row_hash=source_row_hash,
            )
        return None
    if flow and flow["state"] in ACTIVE_STATES:
        if flow["source_id"] != source_id:
            await cur.execute("""
                UPDATE _unverifiable_review_flows
                SET state=%s,flow_version=flow_version+1,
                    safe_reason_code='source_identity_changed',last_action_at=UTC_TIMESTAMP()
                WHERE id=%s
            """, (SOURCE_EXCEPTION, flow["id"]))
        return _flow_payload(await _latest_flow(cur, parser_type, row_key))

    cycle_no = (flow["cycle_no"] if flow else 0) + 1
    deadline_field = workflow.date_fields[0] if workflow.date_fields else ""
    original_deadline = str(values.get(deadline_field) or "")
    await cur.execute("""
        INSERT INTO _unverifiable_review_flows
        (parser_type,row_key,cycle_no,source_id,source_revision,source_row_hash,
         state,original_deadline,previous_deadline,created_by,last_actor_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        parser_type, row_key, cycle_no, source_id, source_revision,
        source_row_hash, INITIAL_PENDING, original_deadline, original_deadline,
        actor_user_id, actor_user_id,
    ))
    flow_id = int(cur.lastrowid)
    await _event(
        cur, flow_id=flow_id, stage=INITIAL_PENDING,
        action="entered_unverifiable", actor_user_id=actor_user_id,
        source_revision=source_revision, source_row_hash=source_row_hash,
    )
    return _flow_payload(await _latest_flow(cur, parser_type, row_key))


async def record_task_save(
    cur,
    *,
    parser_type: str,
    source: dict[str, Any],
    before: dict[str, Any],
    after: dict[str, Any],
    changes: dict[str, str],
    row_key_after: str,
    revision: int,
    actor_user_id: int | None,
) -> None:
    if not supports_unverifiable_review(parser_type):
        return
    flow = await ensure_flow_for_values(
        cur,
        parser_type=parser_type,
        row_key=row_key_after,
        source_id=int(source["id"]),
        source_revision=revision,
        source_row_hash=str(source.get("row_hash") or ""),
        values=after,
        actor_user_id=actor_user_id,
    )
    if not flow or flow["state"] not in EXTENSION_STATES:
        if changed_feedback := next(
            (field for field in TASK_WORKFLOWS[parser_type].secondary_fields if field in changes),
            "",
        ):
            if is_unverifiable_result(after.get(TASK_WORKFLOWS[parser_type].result_field)):
                raise ValueError("二次反馈只能在初步或深度延时复核期间填写")
        return
    workflow = TASK_WORKFLOWS[parser_type]
    changed_feedback = next(
        (field for field in workflow.secondary_fields if field in changes),
        "",
    )
    if not changed_feedback:
        return
    feedback = str(after.get(changed_feedback) or "").strip()
    await cur.execute("""
        UPDATE _unverifiable_review_flows
        SET feedback_submitted=%s,source_revision=%s,source_row_hash=%s,
            flow_version=flow_version+1,last_actor_id=%s,last_action_at=UTC_TIMESTAMP()
        WHERE id=%s
    """, (bool(feedback), revision, str(source.get("row_hash") or ""), actor_user_id, flow["id"]))
    await _event(
        cur, flow_id=flow["id"], stage=flow["state"],
        action="feedback_recorded" if feedback else "feedback_cleared",
        text=feedback, actor_user_id=actor_user_id,
        source_revision=revision, source_row_hash=str(source.get("row_hash") or ""),
    )


async def refresh_unverifiable_source_context(
    cur,
    *,
    source_id: int,
    parser_type: str,
    previous_revision: int,
    current_revision: int,
    current_row_hash: str,
) -> None:
    """Advance flow snapshots after our own verified Tencent writeback.

    A successful local writeback necessarily changes the cached source
    revision/hash.  That change is not an external conflict, so active flow
    rows for the exact prior snapshot must move forward with it.  If another
    process already changed the flow snapshot, leave it untouched and let the
    normal reconciliation pass put it into ``source_exception`` safely.
    """
    if not supports_unverifiable_review(parser_type):
        return
    await cur.execute(
        """
        UPDATE _unverifiable_review_flows
        SET source_revision=%s,source_row_hash=%s,
            flow_version=flow_version+1,last_action_at=UTC_TIMESTAMP()
        WHERE parser_type=%s AND source_id=%s
          AND state IN (%s,%s,%s,%s,%s)
          AND source_revision=%s
        """,
        (
            int(current_revision), str(current_row_hash or ""), parser_type,
            int(source_id), INITIAL_PENDING, INITIAL_EXTENSION, DEEP_PENDING,
            DEEP_EXTENSION, FINAL_UNVERIFIABLE,
            int(previous_revision),
        ),
    )


def _flow_matches_source(flow: dict[str, Any], source: dict[str, Any]) -> bool:
    return (
        int(flow.get("source_id") or 0) == int(source.get("id") or 0)
        and int(flow.get("source_revision") or 0)
        == int(source.get("revision") or 0)
        and str(flow.get("source_row_hash") or "")
        == str(source.get("row_hash") or "")
        and str(flow.get("row_key") or "") == str(source.get("row_key") or "")
        and not str(flow.get("safe_reason_code") or "")
    )


async def reconcile_unverifiable_source_contexts(
    cur,
    parser_type: str | None = None,
) -> int:
    """Persistently pause active flows whose unique Tencent source changed."""
    params: list[Any] = [
        INITIAL_PENDING,
        INITIAL_EXTENSION,
        DEEP_PENDING,
        DEEP_EXTENSION,
        FINAL_UNVERIFIABLE,
        SOURCE_EXCEPTION,
    ]
    parser_clause = ""
    if parser_type:
        if not supports_unverifiable_review(parser_type):
            return 0
        parser_clause = " AND flow.parser_type=%s"
        params.append(parser_type)
    await cur.execute(
        f"""
        SELECT flow.id,flow.state,flow.source_id,flow.source_revision,
               flow.source_row_hash,flow.row_key,
               source.id,source.revision,source.row_hash,source.row_key,
               projection.source_count,projection.conflict
        FROM _unverifiable_review_flows flow
        LEFT JOIN _online_source_rows source ON source.id=flow.source_id
        LEFT JOIN _online_source_projection projection
          ON projection.parser_type=flow.parser_type
         AND projection.row_key=flow.row_key
        WHERE flow.state IN (%s,%s,%s,%s,%s,%s){parser_clause}
        ORDER BY flow.id
        FOR UPDATE
        """,
        params,
    )
    paused = 0
    for row in await cur.fetchall():
        # 腾讯物理行被删除后，在线同步会保留带有平台本地修改的来源快照
        # （physical_row < 0），这样滨湖平台仍可继续保存核查结果和推进
        # 两级研判。只要当前投影仍然唯一且没有内容冲突，删除腾讯行不应
        # 把平台内的流程强制暂停；后续写回队列会单独记录 source_missing。
        source_missing_but_platform_snapshot = (
            row[6] is None
            and int(row[10] or 0) == 1
            and not bool(row[11])
        )
        if source_missing_but_platform_snapshot:
            if str(row[1]) == SOURCE_EXCEPTION:
                # 之前已因来源删除而暂停的流程，恢复到暂停前最后一个
                # 结构化阶段。历史事件是平台保存的安全快照，不读取或
                # 记录腾讯原始内容，因此可以幂等地恢复本地流程。
                await cur.execute(
                    "SELECT stage FROM _unverifiable_review_events "
                    "WHERE flow_id=%s AND action<>'automatic_transition_paused' "
                    "ORDER BY id DESC LIMIT 1",
                    (int(row[0]),),
                )
                previous = await cur.fetchone()
                restored_state = str(previous[0] or "") if previous else INITIAL_PENDING
                if restored_state not in {
                    INITIAL_PENDING, INITIAL_EXTENSION, DEEP_PENDING,
                    DEEP_EXTENSION, FINAL_UNVERIFIABLE,
                }:
                    restored_state = INITIAL_PENDING
                await cur.execute(
                    "UPDATE _unverifiable_review_flows "
                    "SET state=%s,flow_version=flow_version+1,safe_reason_code='',"
                    "last_action_at=UTC_TIMESTAMP() WHERE id=%s AND state=%s",
                    (restored_state, int(row[0]), SOURCE_EXCEPTION),
                )
                if cur.rowcount == 1:
                    await _event(
                        cur,
                        flow_id=int(row[0]),
                        stage=restored_state,
                        action="automatic_transition_resumed",
                        automatic=True,
                        safe_reason_code="source_missing_ignored",
                    )
            continue
        source_valid = (
            row[6] is not None
            and int(row[2] or 0) == int(row[6] or 0)
            and int(row[3] or 0) == int(row[7] or 0)
            and str(row[4] or "") == str(row[8] or "")
            and str(row[5] or "") == str(row[9] or "")
            and int(row[10] or 0) == 1
            and not bool(row[11])
        )
        if source_valid or source_missing_but_platform_snapshot:
            continue
        await cur.execute(
            """
            UPDATE _unverifiable_review_flows
            SET state=%s,flow_version=flow_version+1,
                safe_reason_code='source_context_changed',
                last_action_at=UTC_TIMESTAMP()
            WHERE id=%s AND state=%s
            """,
            (SOURCE_EXCEPTION, int(row[0]), str(row[1])),
        )
        if cur.rowcount != 1:
            continue
        await _event(
            cur,
            flow_id=int(row[0]),
            stage=str(row[1]),
            action="automatic_transition_paused",
            automatic=True,
            source_revision=int(row[7] or 0),
            source_row_hash=str(row[8] or ""),
            safe_reason_code="source_context_changed",
        )
        paused += 1
    return paused


async def prepare_decision(
    cur,
    *,
    parser_type: str,
    source: dict[str, Any],
    current_values: dict[str, Any],
    stage: str,
    outcome: str,
    opinion: str,
    expected_flow_version: int,
    expected_row_hash: str,
) -> dict[str, Any]:
    if not supports_unverifiable_review(parser_type):
        raise ValueError("该业务不支持无法核实研判闭环")
    workflow = TASK_WORKFLOWS[parser_type]
    if not is_unverifiable_result(current_values.get(workflow.result_field)):
        raise ValueError("任务当前已不是无法核实")
    flow = await ensure_flow_for_values(
        cur,
        parser_type=parser_type,
        row_key=str(source["row_key"]),
        source_id=int(source["id"]),
        source_revision=int(source["revision"]),
        source_row_hash=str(source.get("row_hash") or ""),
        values=current_values,
    )
    if not flow or flow["state"] not in PENDING_DECISION_STATES:
        raise ValueError("当前阶段不能重复提交研判决定")
    if not _flow_matches_source(flow, source):
        raise ValueError("腾讯来源版本已经变化，流程已暂停，请刷新后重新核对")
    if flow["state"] != stage:
        raise ValueError("研判阶段已经变化，请刷新后重试")
    if flow["flow_version"] != expected_flow_version:
        raise ValueError("研判流程已经被其他人更新，请刷新后重试")
    if expected_row_hash != str(source.get("row_hash") or ""):
        raise ValueError("腾讯来源版本已经变化，请刷新后重试")
    if outcome not in {"success", "failure"}:
        raise ValueError("请选择研判成功或研判失败")
    if not opinion.strip():
        raise ValueError("请填写研判意见")
    business_date = await get_business_date(cur)
    due = review_due_date(business_date, stage) if outcome == "success" else None
    next_state = (
        INITIAL_EXTENSION if stage == INITIAL_PENDING and outcome == "success"
        else DEEP_PENDING if stage == INITIAL_PENDING
        else DEEP_EXTENSION if outcome == "success"
        else FINAL_UNVERIFIABLE
    )
    stage_name = "初步研判" if stage == INITIAL_PENDING else "深度研判"
    outcome_name = "成功" if outcome == "success" else "失败"
    summary = f"{stage_name}{outcome_name}：{opinion.strip()}"
    if due:
        summary += f"；复核截止 {due.isoformat()}"
    return {
        "flow": flow,
        "next_state": next_state,
        "due_date": due,
        "opinion": opinion.strip(),
        "summary": summary,
        "outcome": outcome,
        "stage": stage,
        "previous_deadline": str(current_values.get(workflow.date_fields[0]) or ""),
    }


async def apply_decision(
    cur,
    *,
    prepared: dict[str, Any],
    source: dict[str, Any],
    revision: int,
    actor_user_id: int,
) -> dict[str, Any]:
    flow = prepared["flow"]
    await cur.execute("""
        UPDATE _unverifiable_review_flows
        SET state=%s,review_due_date=%s,previous_deadline=%s,
            feedback_submitted=0,source_revision=%s,source_row_hash=%s,
            flow_version=flow_version+1,last_actor_id=%s,last_action_at=UTC_TIMESTAMP(),
            finalized_at=IF(%s=%s,UTC_TIMESTAMP(),finalized_at),safe_reason_code=''
        WHERE id=%s AND flow_version=%s
    """, (
        prepared["next_state"], prepared["due_date"],
        prepared["previous_deadline"], revision,
        str(source.get("row_hash") or ""), actor_user_id,
        prepared["next_state"], FINAL_UNVERIFIABLE,
        flow["id"], flow["flow_version"],
    ))
    if cur.rowcount != 1:
        raise ValueError("研判流程已经变化，请刷新后重试")
    await _event(
        cur, flow_id=flow["id"], stage=prepared["stage"],
        action="review_decision", outcome=prepared["outcome"],
        text=prepared["opinion"], actor_user_id=actor_user_id,
        source_revision=revision, source_row_hash=str(source.get("row_hash") or ""),
    )
    return _flow_payload(await _latest_flow(cur, flow["parser_type"], flow["row_key"])) or {}


async def mark_flow_archived(cur, parser_type: str, row_key: str, export_id: int) -> None:
    flow = await _latest_flow(cur, parser_type, row_key, for_update=True)
    payload = _flow_payload(flow)
    if not payload:
        # 普通终态（离苏、无需登记等）没有“无法核实”研判流程，来源归档时
        # 不应为了补造一条流程而阻断业务归档。
        return
    previous_state = payload["state"]
    if previous_state == ARCHIVED:
        return
    if previous_state not in {FINAL_UNVERIFIABLE, RESOLVED}:
        # 活动流程、来源异常和未知状态都必须留在任务池中人工核对，不能被
        # 导出动作绕过状态机。
        raise RuntimeError("review_flow_state_conflict")
    await cur.execute("""
        UPDATE _unverifiable_review_flows
        SET state=%s,flow_version=flow_version+1,archived_at=UTC_TIMESTAMP(),
            last_action_at=UTC_TIMESTAMP(),safe_reason_code=''
        WHERE id=%s
    """, (ARCHIVED, payload["id"]))
    await _event(
        cur, flow_id=payload["id"], stage=previous_state,
        action="archive_exported", outcome=str(export_id), automatic=True,
        source_revision=payload["source_revision"],
        source_row_hash=payload["source_row_hash"],
        safe_reason_code=(
            "resolved_source_archived" if previous_state == RESOLVED else ""
        ),
    )


async def _system_enqueue(
    conn,
    cur,
    *,
    source_row: tuple,
    parser_type: str,
    changes: dict[str, str],
) -> int:
    # Keep this service importable during database bootstrap.  The database
    # module imports ``ensure_unverifiable_review_schema`` before db_manager is
    # initialized, while the writeback service itself imports database.
    from services.local_source import local_data_source_enabled, local_sheet_id
    from services.online_local_writeback import (
        apply_local_system_changes,
        enqueue_local_changes,
    )

    source_id, row_key, revision, row_hash, physical_row, spreadsheet_id, sheet_id, values_json = source_row[:8]
    if local_data_source_enabled():
        source = {
            "id": int(source_id),
            "row_key": str(row_key),
            "row_hash": str(row_hash or ""),
            "revision": int(revision),
            "physical_row": int(physical_row),
            "spreadsheet_id": 0,
            "sheet_id": local_sheet_id(parser_type),
            "values": json_value(values_json, {}),
            "spreadsheet": {"parser_type": parser_type},
        }
        _, next_revision, _, _ = await apply_local_system_changes(
            cur,
            source=source,
            changes=changes,
            user={"id": 0, "username": "system"},
            action="review_advance",
        )
        return next_revision

    await cur.execute(
        "SELECT id,name,file_id,data_sheet_id,header_row,parser_type "
        "FROM _config_spreadsheets WHERE id=%s",
        (spreadsheet_id,),
    )
    spreadsheet = await cur.fetchone()
    if not spreadsheet:
        raise LookupError("spreadsheet_unavailable")
    await cur.execute("""
        INSERT INTO _online_writeback_audit
        (user_id,username,action,parser_type,spreadsheet_id,sheet_id,
         physical_row,column_name,row_key_before,row_key_after,sync_status)
        VALUES (0,'system','review_advance',%s,%s,%s,%s,%s,%s,%s,'pending')
    """, (
        parser_type, spreadsheet_id, sheet_id, physical_row,
        "、".join(changes), row_key, row_key,
    ))
    audit_id = int(cur.lastrowid)
    source = {
        "id": int(source_id),
        "row_key": str(row_key),
        "row_hash": str(row_hash or ""),
        "revision": int(revision),
        "physical_row": int(physical_row),
        "spreadsheet_id": int(spreadsheet_id),
        "sheet_id": str(sheet_id),
        "values": json_value(values_json, {}),
        "spreadsheet": {
            "id": int(spreadsheet[0]), "name": str(spreadsheet[1]),
            "file_id": str(spreadsheet[2] or ""),
            "data_sheet_id": str(spreadsheet[3] or ""),
            "header_row": int(spreadsheet[4] or 1),
            "parser_type": str(spreadsheet[5]),
        },
    }
    return await enqueue_local_changes(
        conn,
        source=source,
        changes=changes,
        user={"id": 0, "username": "system"},
        audit_id=audit_id,
    )


async def reconcile_unverifiable_once() -> int:
    """幂等推进已过期延时；只写安全摘要，不伪造二次反馈。"""
    from database import db_manager
    from services.online_local_writeback import (
        launch_local_change_processing,
        load_local_changes,
        overlay_local_values,
    )

    pool = db_manager.get_pool("online_data")
    source_ids_to_launch: list[int] = []
    advanced = 0
    async with pool.acquire() as conn:
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                await reconcile_unverifiable_source_contexts(cur)
                business_date = await get_business_date(cur)
                await cur.execute("""
                    SELECT flow.id,flow.parser_type,flow.row_key,flow.state,
                           flow.flow_version,flow.source_id,flow.source_revision,
                           flow.source_row_hash,flow.review_due_date,
                           flow.feedback_submitted,
                           source.id,source.row_key,source.revision,source.row_hash,
                           source.physical_row,source.spreadsheet_id,source.sheet_id,
                           source.values_json,projection.source_count,projection.conflict
                    FROM _unverifiable_review_flows flow
                    LEFT JOIN _online_source_rows source ON source.id=flow.source_id
                    LEFT JOIN _online_source_projection projection
                      ON projection.parser_type=flow.parser_type
                     AND projection.row_key=flow.row_key
                    WHERE flow.state IN (%s,%s)
                      AND flow.review_due_date < %s
                    ORDER BY flow.review_due_date,flow.id
                    FOR UPDATE
                """, (INITIAL_EXTENSION, DEEP_EXTENSION, business_date))
                rows = await cur.fetchall()
                for row in rows:
                    flow_id, parser_type, row_key, state = int(row[0]), str(row[1]), str(row[2]), str(row[3])
                    workflow = TASK_WORKFLOWS.get(parser_type)
                    if (
                        not workflow or row[10] is None or str(row[11]) != row_key
                        or int(row[18] or 0) != 1 or bool(row[19])
                        or int(row[12] or 0) != int(row[6] or 0)
                        or str(row[13] or "") != str(row[7] or "")
                    ):
                        await cur.execute("""
                            UPDATE _unverifiable_review_flows
                            SET state=%s,flow_version=flow_version+1,
                                safe_reason_code='source_context_changed',
                                last_action_at=UTC_TIMESTAMP() WHERE id=%s
                        """, (SOURCE_EXCEPTION, flow_id))
                        await _event(
                            cur, flow_id=flow_id, stage=state,
                            action="automatic_transition_paused", automatic=True,
                            safe_reason_code="source_context_changed",
                        )
                        continue
                    changes_by_source = await load_local_changes(cur, [int(row[10])])
                    source_changes = changes_by_source.get(int(row[10]), [])
                    if any(change.get("status") == "conflict" for change in source_changes):
                        await cur.execute("""
                            UPDATE _unverifiable_review_flows
                            SET safe_reason_code='writeback_conflict',
                                flow_version=flow_version+1,last_action_at=UTC_TIMESTAMP()
                            WHERE id=%s AND safe_reason_code<>'writeback_conflict'
                        """, (flow_id,))
                        if cur.rowcount == 1:
                            await _event(
                                cur, flow_id=flow_id, stage=state,
                                action="automatic_transition_paused", automatic=True,
                                safe_reason_code="writeback_conflict",
                            )
                        continue
                    await cur.execute(
                        "UPDATE _unverifiable_review_flows SET safe_reason_code='' "
                        "WHERE id=%s AND safe_reason_code='writeback_conflict'",
                        (flow_id,),
                    )
                    values = overlay_local_values(
                        json_value(row[17], {}), source_changes
                    )
                    if not is_unverifiable_result(values.get(workflow.result_field)):
                        await cur.execute("""
                            UPDATE _unverifiable_review_flows
                            SET state=%s,flow_version=flow_version+1,resolved_at=UTC_TIMESTAMP(),
                                last_action_at=UTC_TIMESTAMP(),safe_reason_code=''
                            WHERE id=%s
                        """, (RESOLVED, flow_id))
                        await _event(
                            cur, flow_id=flow_id, stage=state,
                            action="formal_result_detected", automatic=True,
                        )
                        continue
                    next_state = DEEP_PENDING if state == INITIAL_EXTENSION else FINAL_UNVERIFIABLE
                    summary = (
                        "初步延时已到期，自动进入深度研判"
                        if next_state == DEEP_PENDING
                        else "深度延时已到期，形成最终无法核实"
                    )
                    transition_changes = {workflow.analysis_fields[0]: summary}
                    if next_state == DEEP_PENDING and workflow.secondary_fields:
                        transition_changes[workflow.secondary_fields[0]] = ""
                    revision = await _system_enqueue(
                        conn, cur,
                        source_row=(row[10], row[11], row[12], row[13], row[14], row[15], row[16], row[17]),
                        parser_type=parser_type,
                        changes=transition_changes,
                    )
                    await cur.execute("""
                        UPDATE _unverifiable_review_flows
                        SET state=%s,review_due_date=NULL,feedback_submitted=0,
                            source_revision=%s,flow_version=flow_version+1,
                            last_action_at=UTC_TIMESTAMP(),
                            finalized_at=IF(%s=%s,UTC_TIMESTAMP(),finalized_at),
                            safe_reason_code=''
                        WHERE id=%s
                    """, (next_state, revision, next_state, FINAL_UNVERIFIABLE, flow_id))
                    await _event(
                        cur, flow_id=flow_id, stage=state,
                        action="overdue_auto_transition", outcome=next_state,
                        automatic=True, source_revision=revision,
                        source_row_hash=str(row[13] or ""),
                        safe_reason_code="feedback_recorded" if row[9] else "no_feedback",
                    )
                    source_ids_to_launch.append(int(row[10]))
                    advanced += 1
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
    for source_id in source_ids_to_launch:
        launch_local_change_processing(source_id)
    return advanced


def wake_unverifiable_review_scheduler() -> None:
    _WAKE_EVENT.set()


async def run_unverifiable_review_scheduler() -> None:
    try:
        await reconcile_unverifiable_once()
    except Exception as exc:
        print(f"[UNVERIFIABLE_REVIEW] initial reconcile failed: {type(exc).__name__}")
    while True:
        try:
            await asyncio.wait_for(_WAKE_EVENT.wait(), timeout=300)
        except asyncio.TimeoutError:
            pass
        _WAKE_EVENT.clear()
        try:
            await reconcile_unverifiable_once()
        except Exception as exc:
            print(f"[UNVERIFIABLE_REVIEW] reconcile failed: {type(exc).__name__}")
