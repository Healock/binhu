"""Task dependency graph orchestration for the internal Rete task view.

The graph stores only stable task references and safe state transitions.  The
actual task subject, address and other business fields remain in their owning
domain and are resolved by the read API when the caller is authorized.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from config import settings
from services.task_workflow import TASK_WORKFLOWS


TERMINAL_NODE_STATES = {"completed", "cancelled", "source_missing"}
GRAPH_ENABLED_KEY = "task_graph_enabled"


def _workflow_schema() -> str:
    return f"`{settings.MYSQL_WORKFLOW_DB.replace('`', '')}`"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_detail(**values: Any) -> str:
    allowed = {
        str(key): str(value)
        for key, value in values.items()
        if value is not None
    }
    return json.dumps(allowed, ensure_ascii=False, separators=(",", ":"))


async def _resolve_online_owner(cur, inspector: str) -> tuple[str, str]:
    inspector = _text(inspector)
    if not inspector:
        return "queue", "unassigned"
    platform = settings.MYSQL_PLATFORM_DB.replace("`", "")
    await cur.execute(
        f"SELECT user.id FROM `{platform}`._users user "
        f"JOIN `{platform}`._grid_members member ON member.id=user.member_id "
        "WHERE member.name=%s ORDER BY user.id LIMIT 2",
        (inspector,),
    )
    rows = await cur.fetchall()
    if len(rows) == 1:
        return "user", str(int(rows[0][0]))
    return "queue", "unmapped"


async def task_graph_enabled(cur) -> bool:
    await cur.execute(
        "SELECT config_value FROM _system_config WHERE config_key=%s",
        (GRAPH_ENABLED_KEY,),
    )
    row = await cur.fetchone()
    return _text(row[0]).lower() in {"1", "true", "yes", "on"} if row else False


async def set_task_graph_enabled(cur, enabled: bool) -> None:
    value = "1" if enabled else "0"
    await cur.execute(
        "INSERT INTO _system_config (config_key, config_value) VALUES (%s,%s) "
        "ON DUPLICATE KEY UPDATE config_value=%s",
        (GRAPH_ENABLED_KEY, value, value),
    )


async def online_task_blocked(cur, parser_type: str, row_key: str) -> bool:
    if not await task_graph_enabled(cur):
        return False
    schema = _workflow_schema()
    await cur.execute(
        f"SELECT 1 FROM {schema}.task_graph_nodes successor "
        f"JOIN {schema}.task_graph_dependencies dependency "
        "ON dependency.successor_node_id=successor.id "
        "WHERE successor.task_type='online_check' AND successor.provider='online' "
        "AND successor.parser_type=%s AND successor.source_ref=%s "
        "AND dependency.state='active' LIMIT 1",
        (parser_type, row_key),
    )
    return bool(await cur.fetchone())


def _node_key(task_type: str, parser_type: str, row_key: str) -> str:
    # row_key is already an MD5 business key. Keep the node key deterministic
    # while avoiding any possibility of putting source content in WorkflowData.
    return f"{task_type}:{parser_type}:{row_key}"


def analysis_graph_state(parser_type: str, values: dict[str, Any], *, existing: bool = False) -> dict[str, str] | None:
    workflow = TASK_WORKFLOWS.get(parser_type)
    if workflow is None:
        return None
    result = _text(values.get(workflow.result_field))
    analysis_value = next(
        (_text(values.get(field)) for field in workflow.analysis_fields if _text(values.get(field))),
        "",
    )
    unable = "无法核实" in result
    if not unable and not analysis_value and not existing:
        return None
    return {
        "online_status": "blocked" if unable and not analysis_value else (
            "completed" if workflow.state(values) == "completed" else "ready"
        ),
        "analysis_status": "completed" if analysis_value else ("ready" if unable else "cancelled"),
        "dependency_state": "active" if unable and not analysis_value else (
            "satisfied" if analysis_value else "cancelled"
        ),
    }


def chain_should_archive(statuses: set[str], dependency_state: str) -> bool:
    return bool(statuses) and statuses.issubset(TERMINAL_NODE_STATES) and dependency_state != "active"


async def _load_node(cur, task_type: str, parser_type: str, row_key: str):
    schema = _workflow_schema()
    await cur.execute(
        f"SELECT id,node_key,status,completed_at,archived_at,owner_type,owner_ref "
        f"FROM {schema}.task_graph_nodes "
        "WHERE task_type=%s AND provider='online' AND parser_type=%s "
        "AND source_ref=%s FOR UPDATE",
        (task_type, parser_type, row_key),
    )
    return await cur.fetchone()


async def _rename_node_source(cur, parser_type: str, row_key_before: str, row_key_after: str) -> None:
    if not row_key_before or row_key_before == row_key_after:
        return
    schema = _workflow_schema()
    await cur.execute(
        f"UPDATE {schema}.task_graph_nodes SET source_ref=%s, "
        "node_key=CONCAT(task_type,':',parser_type,':',%s) "
        "WHERE provider='online' AND parser_type=%s AND source_ref=%s",
        (row_key_after, row_key_after, parser_type, row_key_before),
    )


async def _ensure_node(
    cur,
    *,
    task_type: str,
    parser_type: str,
    row_key: str,
    owner_type: str,
    owner_ref: str,
    status: str,
    reason_code: str,
    actor_user_id: int | None,
    event_type: str,
):
    existing = await _load_node(cur, task_type, parser_type, row_key)
    schema = _workflow_schema()
    if existing:
        node_id, node_key, old_status, completed_at, archived_at, old_owner_type, old_owner_ref = existing
        changed = (
            _text(old_status) != status
            or _text(old_owner_type) != owner_type
            or _text(old_owner_ref) != owner_ref
            or (status == "completed" and completed_at is None)
            or (status not in TERMINAL_NODE_STATES and archived_at is not None)
        )
        if changed:
            next_completed = "UTC_TIMESTAMP()" if status == "completed" else "completed_at"
            await cur.execute(
                f"UPDATE {schema}.task_graph_nodes SET status=%s, owner_type=%s, owner_ref=%s, "
                f"reason_code=%s, completed_at={next_completed}, "
                "archived_at=NULL, updated_at=UTC_TIMESTAMP() WHERE id=%s",
                (status, owner_type, owner_ref, reason_code, node_id),
            )
            await cur.execute(
                f"INSERT INTO {schema}.task_graph_events "
                "(node_id,event_type,actor_user_id,detail_json) VALUES (%s,%s,%s,%s)",
                (node_id, event_type, actor_user_id, _safe_detail(status=status, reason=reason_code)),
            )
        return {"id": int(node_id), "status": status, "created": False, "changed": changed}

    node_key = _node_key(task_type, parser_type, row_key)
    await cur.execute(
        f"INSERT INTO {schema}.task_graph_nodes "
        "(node_key,task_type,provider,parser_type,source_ref,owner_type,owner_ref,status,reason_code,completed_at) "
        "VALUES (%s,%s,'online',%s,%s,%s,%s,%s,%s,%s)",
        (
            node_key,
            task_type,
            parser_type,
            row_key,
            owner_type,
            owner_ref,
            status,
            reason_code,
            datetime.utcnow() if status == "completed" else None,
        ),
    )
    await cur.execute(
        f"SELECT id FROM {schema}.task_graph_nodes WHERE node_key=%s",
        (node_key,),
    )
    row = await cur.fetchone()
    node_id = int(row[0])
    await cur.execute(
        f"INSERT INTO {schema}.task_graph_events "
        "(node_id,event_type,actor_user_id,detail_json) VALUES (%s,%s,%s,%s)",
        (node_id, event_type, actor_user_id, _safe_detail(status=status, reason=reason_code)),
    )
    return {"id": node_id, "status": status, "created": True, "changed": True}


async def _load_dependency(cur, predecessor_id: int, successor_id: int, reason_code: str):
    schema = _workflow_schema()
    await cur.execute(
        f"SELECT id,state FROM {schema}.task_graph_dependencies "
        "WHERE predecessor_node_id=%s AND successor_node_id=%s AND reason_code=%s FOR UPDATE",
        (predecessor_id, successor_id, reason_code),
    )
    return await cur.fetchone()


async def _set_dependency(
    cur,
    *,
    predecessor_id: int,
    successor_id: int,
    state: str,
    reason_code: str,
    actor_user_id: int | None,
    event_type: str,
):
    existing = await _load_dependency(cur, predecessor_id, successor_id, reason_code)
    schema = _workflow_schema()
    if existing:
        dependency_id, old_state = int(existing[0]), _text(existing[1])
        if old_state == state:
            return {"id": dependency_id, "changed": False}
        if state == "active":
            await cur.execute(
                f"UPDATE {schema}.task_graph_dependencies SET state='active', "
                "satisfied_at=NULL,cancelled_at=NULL,updated_at=UTC_TIMESTAMP() WHERE id=%s",
                (dependency_id,),
            )
        elif state == "satisfied":
            await cur.execute(
                f"UPDATE {schema}.task_graph_dependencies SET state='satisfied', "
                "satisfied_at=UTC_TIMESTAMP(),updated_at=UTC_TIMESTAMP() WHERE id=%s",
                (dependency_id,),
            )
        else:
            await cur.execute(
                f"UPDATE {schema}.task_graph_dependencies SET state='cancelled', "
                "cancelled_at=UTC_TIMESTAMP(),updated_at=UTC_TIMESTAMP() WHERE id=%s",
                (dependency_id,),
            )
        await cur.execute(
            f"INSERT INTO {schema}.task_graph_events "
            "(dependency_id,event_type,actor_user_id,detail_json) VALUES (%s,%s,%s,%s)",
            (dependency_id, event_type, actor_user_id, _safe_detail(from_state=old_state, to_state=state, reason=reason_code)),
        )
        return {"id": dependency_id, "changed": True}

    await cur.execute(
        f"INSERT INTO {schema}.task_graph_dependencies "
        "(predecessor_node_id,successor_node_id,state,reason_code,created_by_event,satisfied_at,cancelled_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            predecessor_id,
            successor_id,
            state,
            reason_code,
            event_type,
            datetime.utcnow() if state == "satisfied" else None,
            datetime.utcnow() if state == "cancelled" else None,
        ),
    )
    await cur.execute(
        f"SELECT id FROM {schema}.task_graph_dependencies "
        "WHERE predecessor_node_id=%s AND successor_node_id=%s AND reason_code=%s",
        (predecessor_id, successor_id, reason_code),
    )
    dependency_id = int((await cur.fetchone())[0])
    await cur.execute(
        f"INSERT INTO {schema}.task_graph_events "
        "(dependency_id,event_type,actor_user_id,detail_json) VALUES (%s,%s,%s,%s)",
        (dependency_id, event_type, actor_user_id, _safe_detail(to_state=state, reason=reason_code)),
    )
    return {"id": dependency_id, "changed": True}


async def _archive_pair(cur, online_id: int, analysis_id: int, dependency_id: int, actor_user_id: int | None):
    schema = _workflow_schema()
    await cur.execute(
        f"SELECT status FROM {schema}.task_graph_nodes WHERE id IN (%s,%s)",
        (online_id, analysis_id),
    )
    statuses = {_text(row[0]) for row in await cur.fetchall()}
    await cur.execute(
        f"SELECT state FROM {schema}.task_graph_dependencies WHERE id=%s",
        (dependency_id,),
    )
    dependency = await cur.fetchone()
    if dependency and chain_should_archive(statuses, _text(dependency[0])):
        await cur.execute(
            f"UPDATE {schema}.task_graph_nodes SET archived_at=UTC_TIMESTAMP() "
            "WHERE id IN (%s,%s) AND archived_at IS NULL",
            (online_id, analysis_id),
        )
        if int(getattr(cur, "rowcount", 0) or 0) > 0:
            await cur.execute(
                f"INSERT INTO {schema}.task_graph_events "
                "(node_id,event_type,actor_user_id,detail_json) VALUES (%s,'chain_archived',%s,%s)",
                (online_id, actor_user_id, _safe_detail(reason="all_nodes_terminal")),
            )
    else:
        await cur.execute(
            f"UPDATE {schema}.task_graph_nodes SET archived_at=NULL "
            "WHERE id IN (%s,%s)",
            (online_id, analysis_id),
        )


async def reconcile_online_task_graph(
    cur,
    *,
    parser_type: str,
    row_key_before: str,
    row_key_after: str,
    before: dict[str, Any] | None,
    after: dict[str, Any],
    actor_user_id: int | None = None,
    event_type: str = "online_task_reconcile",
    force: bool = False,
) -> dict[str, Any]:
    if not force and not await task_graph_enabled(cur):
        return {"enabled": False, "changed": False}
    workflow = TASK_WORKFLOWS.get(parser_type)
    if workflow is None:
        return {"enabled": True, "changed": False}
    row_key = row_key_after or row_key_before
    initial_state = analysis_graph_state(parser_type, after)
    if initial_state is None and not row_key_before:
        return {"enabled": True, "changed": False}
    await _rename_node_source(cur, parser_type, row_key_before, row_key)
    existing_online = await _load_node(cur, "online_check", parser_type, row_key)
    existing_analysis = await _load_node(cur, "analysis", parser_type, row_key)
    if initial_state is None and not existing_online and not existing_analysis:
        return {"enabled": True, "changed": False}
    state = initial_state or analysis_graph_state(parser_type, after, existing=True)
    assert state is not None
    owner_type, owner_ref = await _resolve_online_owner(cur, _text(after.get("核查人")))
    online = await _ensure_node(
        cur,
        task_type="online_check",
        parser_type=parser_type,
        row_key=row_key,
        owner_type=owner_type,
        owner_ref=owner_ref,
        status=state["online_status"],
        reason_code="mobile_task",
        actor_user_id=actor_user_id,
        event_type=event_type,
    )
    analysis = await _ensure_node(
        cur,
        task_type="analysis",
        parser_type=parser_type,
        row_key=row_key,
        owner_type="queue",
        owner_ref="基础管控",
        status=state["analysis_status"],
        reason_code="unable_to_verify",
        actor_user_id=actor_user_id,
        event_type=event_type,
    )
    dependency_state = state["dependency_state"]
    dependency = await _set_dependency(
        cur,
        predecessor_id=analysis["id"],
        successor_id=online["id"],
        state=dependency_state,
        reason_code="analysis_before_followup",
        actor_user_id=actor_user_id,
        event_type=event_type,
    )
    dependency_id = int(dependency["id"])
    await _archive_pair(cur, online["id"], analysis["id"], dependency_id, actor_user_id)
    return {
        "enabled": True,
        "changed": bool(online["changed"] or analysis["changed"] or dependency["changed"]),
        "online_node_id": online["id"],
        "analysis_node_id": analysis["id"],
        "dependency_id": dependency_id,
        "dependency_state": dependency_state,
    }


async def task_graph_preview(cur) -> dict[str, int]:
    counts = {"projection_rows": 0, "unable_to_verify": 0, "analyzed": 0, "historical_analysis": 0, "eligible_chains": 0, "blank_inspector": 0, "unmatched_inspector": 0}
    inspectors: set[str] = set()
    await cur.execute("SELECT parser_type,values_json FROM _online_source_projection")
    for parser_type, raw_values in await cur.fetchall():
        workflow = TASK_WORKFLOWS.get(str(parser_type))
        if not workflow:
            continue
        counts["projection_rows"] += 1
        values = raw_values if isinstance(raw_values, dict) else json.loads(raw_values or "{}")
        result = _text(values.get(workflow.result_field))
        analysis = any(_text(values.get(field)) for field in workflow.analysis_fields)
        unable = "无法核实" in result
        eligible = unable or analysis
        if unable:
            counts["unable_to_verify"] += 1
            if analysis:
                counts["analyzed"] += 1
        elif analysis:
            counts["historical_analysis"] += 1
        if eligible:
            counts["eligible_chains"] += 1
            if not _text(values.get("核查人")):
                counts["blank_inspector"] += 1
            else:
                inspectors.add(_text(values.get("核查人")))
    if inspectors:
        platform = settings.MYSQL_PLATFORM_DB.replace("`", "")
        placeholders = ",".join(["%s"] * len(inspectors))
        await cur.execute(
            f"SELECT member.name,COUNT(user.id) FROM `{platform}`._grid_members member "
            f"LEFT JOIN `{platform}`._users user ON user.member_id=member.id "
            f"WHERE member.name IN ({placeholders}) GROUP BY member.name",
            tuple(sorted(inspectors)),
        )
        mapped = {str(row[0]): int(row[1] or 0) for row in await cur.fetchall()}
        counts["unmatched_inspector"] = sum(mapped.get(name, 0) != 1 for name in inspectors)
    return counts


async def backfill_task_graph(cur, actor_user_id: int | None = None) -> dict[str, int]:
    await cur.execute("SELECT parser_type,row_key,values_json FROM _online_source_projection")
    result = {"processed": 0, "changed": 0}
    for parser_type, row_key, raw_values in await cur.fetchall():
        workflow = TASK_WORKFLOWS.get(str(parser_type))
        if not workflow:
            continue
        values = raw_values if isinstance(raw_values, dict) else json.loads(raw_values or "{}")
        outcome = await reconcile_online_task_graph(
            cur,
            parser_type=str(parser_type),
            row_key_before="",
            row_key_after=str(row_key),
            before=None,
            after=values,
            actor_user_id=actor_user_id,
            event_type="task_graph_backfill",
            force=True,
        )
        result["processed"] += 1
        result["changed"] += int(bool(outcome.get("changed")))
    return result


async def reconcile_projection_task_graph(cur, parser_type: str) -> dict[str, int]:
    """Idempotently reconcile graph chains after a source projection rebuild."""
    if not await task_graph_enabled(cur) or parser_type not in TASK_WORKFLOWS:
        return {"processed": 0, "changed": 0, "source_missing": 0}
    schema = _workflow_schema()
    await cur.execute(
        "SELECT row_key,values_json FROM _online_source_projection WHERE parser_type=%s",
        (parser_type,),
    )
    values_by_key = {
        str(row_key): (raw if isinstance(raw, dict) else json.loads(raw or "{}"))
        for row_key, raw in await cur.fetchall()
    }
    workflow = TASK_WORKFLOWS[parser_type]
    relevant = {
        key for key, values in values_by_key.items()
        if "无法核实" in _text(values.get(workflow.result_field))
        or any(_text(values.get(field)) for field in workflow.analysis_fields)
    }
    await cur.execute(
        f"SELECT DISTINCT source_ref FROM {schema}.task_graph_nodes "
        "WHERE provider='online' AND parser_type=%s AND archived_at IS NULL",
        (parser_type,),
    )
    persisted = {str(row[0]) for row in await cur.fetchall()}
    result = {"processed": 0, "changed": 0, "source_missing": 0}
    for row_key in sorted(relevant | (persisted & set(values_by_key))):
        outcome = await reconcile_online_task_graph(
            cur,
            parser_type=parser_type,
            row_key_before=row_key,
            row_key_after=row_key,
            before=values_by_key[row_key],
            after=values_by_key[row_key],
            event_type="source_projection_reconcile",
            force=True,
        )
        result["processed"] += 1
        result["changed"] += int(bool(outcome.get("changed")))

    missing = persisted - set(values_by_key)
    if missing:
        placeholders = ",".join(["%s"] * len(missing))
        await cur.execute(
            f"SELECT id,status FROM {schema}.task_graph_nodes "
            f"WHERE provider='online' AND parser_type=%s AND source_ref IN ({placeholders}) FOR UPDATE",
            (parser_type, *sorted(missing)),
        )
        for node_id, old_status in await cur.fetchall():
            if _text(old_status) == "source_missing":
                continue
            await cur.execute(
                f"UPDATE {schema}.task_graph_nodes SET status='source_missing', "
                "reason_code='source_removed',completed_at=COALESCE(completed_at,UTC_TIMESTAMP()),"
                "updated_at=UTC_TIMESTAMP() WHERE id=%s",
                (node_id,),
            )
            await cur.execute(
                f"INSERT INTO {schema}.task_graph_events "
                "(node_id,event_type,detail_json) VALUES (%s,'source_removed',%s)",
                (node_id, _safe_detail(status="source_missing", reason="source_removed")),
            )
            result["source_missing"] += 1
        await cur.execute(
            f"UPDATE {schema}.task_graph_dependencies dependency "
            f"JOIN {schema}.task_graph_nodes predecessor ON predecessor.id=dependency.predecessor_node_id "
            f"JOIN {schema}.task_graph_nodes successor ON successor.id=dependency.successor_node_id "
            "SET dependency.state='cancelled',dependency.cancelled_at=COALESCE(dependency.cancelled_at,UTC_TIMESTAMP()) "
            f"WHERE dependency.state='active' AND predecessor.parser_type=%s AND successor.parser_type=%s "
            f"AND (predecessor.source_ref IN ({placeholders}) "
            f"OR successor.source_ref IN ({placeholders}))",
            (parser_type, parser_type, *sorted(missing), *sorted(missing)),
        )
        await cur.execute(
            f"UPDATE {schema}.task_graph_nodes SET archived_at=UTC_TIMESTAMP() "
            f"WHERE provider='online' AND parser_type=%s AND source_ref IN ({placeholders})",
            (parser_type, *sorted(missing)),
        )
    return result
