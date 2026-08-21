"""Super-admin task dependency graph API for the Rete workbench."""

from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from database import get_db
from config import settings
from deps import require_super_admin
from services.audit import record_admin_audit
from services.parsers import get_parser
from services.task_graph import (
    backfill_task_graph,
    set_task_graph_enabled,
    task_graph_enabled,
    task_graph_preview,
)
from services.task_workflow import TASK_WORKFLOWS


router = APIRouter(prefix="/api/task-graph", tags=["任务依赖图"])
WORKFLOW_SCHEMA = f"`{settings.MYSQL_WORKFLOW_DB.replace('`', '')}`"


class TaskGraphSearch(BaseModel):
    view: Literal["person", "queue"] = "person"
    person_user_id: int | None = Field(default=None, gt=0)
    queue: str = Field(default="基础管控", max_length=190)
    history: bool = False
    task_types: list[str] = Field(default_factory=list, max_length=10)
    keyword: str = Field(default="", max_length=100)
    cursors: dict[str, int] = Field(default_factory=dict)
    page_size: int = Field(default=20, ge=1, le=50)


class TaskGraphConfigUpdate(BaseModel):
    enabled: bool


def _text(value: Any) -> str:
    return str(value or "").strip()


def _json_value(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item or "") for key, item in value.items()}
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _node_key(task_type: str, parser_type: str, row_key: str) -> str:
    return f"{task_type}:{parser_type}:{row_key}"


def task_graph_access(
    *,
    task_type: str,
    status: str,
    view: str,
    history: bool,
    owner_type: str,
    owner_ref: str,
    selected_owner_ref: str,
) -> tuple[str, str]:
    if (
        task_type == "online_check"
        and not history
        and status not in {"completed", "cancelled", "source_missing"}
        and view == "person"
        and owner_type == "user"
        and owner_ref == selected_owner_ref
    ):
        return "editable", "owned"
    if task_type == "analysis" and not history and view == "queue" and status in {"ready", "blocked"}:
        return "editable", "owned"
    return "readonly", "predecessor" if task_type == "analysis" else "successor"


def _row_node(
    *,
    task_type: str,
    parser_type: str,
    row_key: str,
    values: dict[str, str],
    owner_ref: str,
    status: str,
    access_mode: str,
    relationship: str,
    completed_at: Any = None,
    archived_at: Any = None,
    sync_warning: bool = False,
) -> dict[str, Any]:
    workflow = TASK_WORKFLOWS[parser_type]
    summary = workflow.summary(values)
    analysis = task_type == "analysis"
    open_path = (
        f"/police-analysis/{parser_type}/{row_key}?scope=all"
        if analysis
        else f"/tasks/{parser_type}/{row_key}?scope=all"
    )
    if access_mode != "editable":
        open_path += "&readonly=1"
    return {
        "id": _node_key(task_type, parser_type, row_key),
        "task_type": task_type,
        "category": "基础管控研判" if analysis else workflow.label,
        "parser_type": parser_type,
        "row_key": row_key,
        "title": summary["title"],
        "community": _text(values.get("下发社区") or values.get("社区")),
        "owner": "基础管控" if analysis else owner_ref,
        "status": status,
        "access_mode": access_mode,
        "relationship": relationship,
        "description": (
            "研判完成后，原核查任务可以继续处理"
            if analysis and status == "completed"
            else "正在等待基础管控填写研判结果"
            if analysis
            else "这条任务需要当前核查人继续处理"
        ),
        "completed_at": completed_at,
        "archived_at": archived_at,
        "sync_warning": sync_warning,
        "open_path": open_path,
    }


async def _load_graph_records(cur, history: bool):
    archive_clause = "archived_at IS NOT NULL" if history else "archived_at IS NULL"
    await cur.execute(
        f"SELECT id,task_type,parser_type,source_ref,owner_type,owner_ref,status,"
        f"completed_at,archived_at FROM {WORKFLOW_SCHEMA}.task_graph_nodes "
        f"WHERE {archive_clause}"
    )
    nodes = {int(row[0]): row for row in await cur.fetchall()}
    if not nodes:
        return nodes, []
    placeholders = ",".join(["%s"] * len(nodes))
    await cur.execute(
        f"SELECT id,predecessor_node_id,successor_node_id,state,reason_code "
        f"FROM {WORKFLOW_SCHEMA}.task_graph_dependencies "
        f"WHERE predecessor_node_id IN ({placeholders}) OR successor_node_id IN ({placeholders})",
        (*nodes, *nodes),
    )
    edges = await cur.fetchall()
    return nodes, edges


async def _projection_rows(cur, parser_type: str, *, view: str, owner: str, history: bool):
    workflow = TASK_WORKFLOWS[parser_type]
    where = ["parser_type=%s"]
    params: list[Any] = [parser_type]
    if view == "person":
        where.append("inspector=%s")
        params.append(owner)
    else:
        # Queue view is intentionally limited to the first dependency type.
        result_path = '$."' + workflow.result_field.replace('"', '\\"') + '"'
        where.append("JSON_UNQUOTE(JSON_EXTRACT(values_json,%s)) LIKE %s")
        params.extend([result_path, "%无法核实%"])
    if not history:
        where.append("task_state<>'completed'")
    else:
        where.append("task_state='completed'")
    await cur.execute(
        "SELECT row_key,values_json,inspector,task_state,pending_state,updated_at "
        "FROM _online_source_projection WHERE " + " AND ".join(where) +
        " ORDER BY updated_at DESC,row_key LIMIT 2000",
        params,
    )
    return await cur.fetchall()


@router.get("/config")
async def get_task_graph_config(user: dict = Depends(require_super_admin), conn=Depends(get_db)):
    del user
    async with conn.cursor() as cur:
        enabled = await task_graph_enabled(cur)
    return {"enabled": enabled, "internal_only": True}


@router.put("/config")
async def update_task_graph_config(
    data: TaskGraphConfigUpdate,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await set_task_graph_enabled(cur, data.enabled)
    await conn.commit()
    await record_admin_audit(
        user,
        "task_graph.config.update",
        target_type="system_config",
        target_name="task_graph_enabled",
        detail={"enabled": data.enabled},
    )
    return {"enabled": data.enabled, "internal_only": True}


@router.post("/backfill/preview")
async def preview_task_graph(user: dict = Depends(require_super_admin), conn=Depends(get_db)):
    del user
    async with conn.cursor() as cur:
        return await task_graph_preview(cur)


@router.post("/backfill")
async def run_task_graph_backfill(user: dict = Depends(require_super_admin), conn=Depends(get_db)):
    async with conn.cursor() as cur:
        result = await backfill_task_graph(cur, int(user.get("id")) if user.get("id") else None)
    await conn.commit()
    await record_admin_audit(
        user,
        "task_graph.backfill",
        target_type="task_graph",
        target_name="analysis_before_followup",
        detail={"processed": result["processed"], "changed": result["changed"]},
    )
    return result


@router.get("/options")
async def task_graph_options(user: dict = Depends(require_super_admin), conn=Depends(get_db)):
    del user
    async with conn.cursor() as cur:
        platform = settings.MYSQL_PLATFORM_DB.replace("`", "")
        await cur.execute(
            "SELECT mapped.user_id,mapped.name,COUNT(projection.row_key) FROM ("
            f"SELECT member.name,MIN(user.id) user_id FROM `{platform}`._users user "
            f"JOIN `{platform}`._grid_members member ON member.id=user.member_id "
            "GROUP BY member.name HAVING COUNT(user.id)=1) mapped "
            "JOIN _online_source_projection projection ON projection.inspector=mapped.name "
            "WHERE projection.task_state<>'completed' "
            "GROUP BY mapped.user_id,mapped.name ORDER BY COUNT(projection.row_key) DESC,mapped.name"
        )
        inspectors = [
            {"value": str(int(row[0])), "label": str(row[1]), "count": int(row[2] or 0)}
            for row in await cur.fetchall()
        ]
    return {"inspectors": inspectors, "queues": [{"value": "基础管控", "label": "基础管控"}]}


@router.post("/search")
async def search_task_graph(
    data: TaskGraphSearch,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        enabled = await task_graph_enabled(cur)
        if not enabled:
            return {"enabled": False, "nodes": [], "edges": [], "facets": {}, "next_cursors": {}}
        if data.view == "person":
            if not data.person_user_id:
                raise HTTPException(400, "请选择要代入查看的人员账号")
            platform = settings.MYSQL_PLATFORM_DB.replace("`", "")
            await cur.execute(
                f"SELECT member.name FROM `{platform}`._users user "
                f"JOIN `{platform}`._grid_members member ON member.id=user.member_id "
                "WHERE user.id=%s",
                (data.person_user_id,),
            )
            person_row = await cur.fetchone()
            if not person_row:
                raise HTTPException(404, "人员账号不存在或尚未关联人员")
            owner_key = str(data.person_user_id)
            projection_owner = _text(person_row[0])
        else:
            owner_key = _text(data.queue) or "基础管控"
            projection_owner = owner_key
        graph_nodes, graph_edges = await _load_graph_records(cur, data.history)
        selected_ids = {
            node_id for node_id, row in graph_nodes.items()
            if (_text(row[4]) == "user" and _text(row[5]) == owner_key)
            or (_text(row[4]) == "queue" and _text(row[5]) == owner_key)
        }
        changed = True
        while changed:
            changed = False
            for _, predecessor, successor, _, _ in graph_edges:
                predecessor = int(predecessor)
                successor = int(successor)
                if predecessor in selected_ids and successor in graph_nodes and successor not in selected_ids:
                    selected_ids.add(successor)
                    changed = True
                if successor in selected_ids and predecessor in graph_nodes and predecessor not in selected_ids:
                    selected_ids.add(predecessor)
                    changed = True
        selected_records = {node_id: graph_nodes[node_id] for node_id in selected_ids if node_id in graph_nodes}

        node_rows: dict[str, dict[str, Any]] = {}
        raw_rows_by_type: dict[str, list[tuple]] = {}
        parser_types = list(TASK_WORKFLOWS)
        for parser_type in parser_types:
            if parser_type not in TASK_WORKFLOWS:
                continue
            rows = await _projection_rows(cur, parser_type, view=data.view, owner=projection_owner, history=data.history)
            if data.keyword:
                needle = data.keyword.lower()
                rows = [
                    row for row in rows
                    if needle in json.dumps(_json_value(row[1]), ensure_ascii=False).lower()
                ]
            raw_rows_by_type[parser_type] = rows

        refs_by_type: dict[str, set[str]] = {}
        for record in selected_records.values():
            parser_type, row_key = _text(record[2]), _text(record[3])
            if parser_type in TASK_WORKFLOWS and row_key:
                refs_by_type.setdefault(parser_type, set()).add(row_key)
        for parser_type, refs in refs_by_type.items():
            present = {str(row[0]) for row in raw_rows_by_type.get(parser_type, [])}
            missing_refs = sorted(refs - present)
            if not missing_refs:
                continue
            placeholders = ",".join(["%s"] * len(missing_refs))
            await cur.execute(
                "SELECT row_key,values_json,inspector,task_state,pending_state,updated_at "
                f"FROM _online_source_projection WHERE parser_type=%s AND row_key IN ({placeholders})",
                (parser_type, *missing_refs),
            )
            raw_rows_by_type.setdefault(parser_type, []).extend(await cur.fetchall())

        if data.keyword:
            needle = data.keyword.lower()
            matching_refs = {
                (parser_type, str(row[0]))
                for parser_type, rows in raw_rows_by_type.items()
                for row in rows
                if needle in json.dumps(_json_value(row[1]), ensure_ascii=False).lower()
            }
            selected_records = {
                node_id: record for node_id, record in selected_records.items()
                if (_text(record[2]), _text(record[3])) in matching_refs
            }
            raw_rows_by_type = {
                parser_type: [row for row in rows if (parser_type, str(row[0])) in matching_refs]
                for parser_type, rows in raw_rows_by_type.items()
            }

        graph_by_source = {
            (_text(record[1]), _text(record[2]), _text(record[3])): record
            for record in selected_records.values()
        }
        for parser_type, rows in raw_rows_by_type.items():
            for row_key, raw_values, inspector, task_state, pending_state, updated_at in rows:
                values = _json_value(raw_values)
                task_key = _node_key("online_check", parser_type, str(row_key))
                graph_record = graph_by_source.get(("online_check", parser_type, str(row_key)))
                if not graph_record:
                    continue
                review_stage = TASK_WORKFLOWS[parser_type].review_stage(values)
                status = _text(graph_record[6]) if graph_record else (
                    "blocked" if review_stage == "waiting_analysis" else "completed" if _text(task_state) == "completed" else "ready"
                )
                relationship = "owned"
                access = "editable" if data.view == "person" and _text(inspector) == projection_owner else "readonly"
                node_rows[task_key] = _row_node(
                    task_type="online_check",
                    parser_type=parser_type,
                    row_key=str(row_key),
                    values=values,
                    owner_ref=_text(inspector),
                    status=status,
                    access_mode=access,
                    relationship=relationship,
                    completed_at=graph_record[7] if graph_record else None,
                    archived_at=graph_record[8] if graph_record else None,
                    sync_warning=bool(_text(pending_state)),
                )

        for node_id, record in selected_records.items():
            task_type, parser_type, row_key = _text(record[1]), _text(record[2]), _text(record[3])
            if not parser_type or parser_type not in TASK_WORKFLOWS:
                continue
            projection = next((row for row in raw_rows_by_type.get(parser_type, []) if str(row[0]) == row_key), None)
            values = _json_value(projection[1]) if projection else {}
            if data.task_types and task_type not in data.task_types:
                continue
            key = _node_key(task_type, parser_type, row_key)
            status = _text(record[6])
            access, relationship = task_graph_access(
                task_type=task_type,
                status=status,
                view=data.view,
                history=data.history,
                owner_type=_text(record[4]),
                owner_ref=_text(record[5]),
                selected_owner_ref=owner_key,
            )
            node_rows[key] = _row_node(
                task_type=task_type,
                parser_type=parser_type,
                row_key=row_key,
                values=values,
                owner_ref=_text(record[5]),
                status=status,
                access_mode=access,
                relationship=relationship,
                completed_at=record[7],
                archived_at=record[8],
                sync_warning=bool(projection and _text(projection[4])),
            )

        # Independent tasks are paged per business type. Dependency chains
        # already selected above are never hidden by the per-type page cursor.
        next_cursors: dict[str, int] = {}
        if not data.history and (not data.task_types or "online_check" in data.task_types):
            for parser_type, rows in raw_rows_by_type.items():
                cursor = max(0, int(data.cursors.get(parser_type, 0) or 0))
                independent = [
                    row for row in rows
                    if _node_key("online_check", parser_type, str(row[0])) not in node_rows
                ]
                page = independent[cursor:cursor + data.page_size]
                for row_key, raw_values, inspector, task_state, pending_state, updated_at in page:
                    values = _json_value(raw_values)
                    review_stage = TASK_WORKFLOWS[parser_type].review_stage(values)
                    status = "blocked" if review_stage == "waiting_analysis" else "completed" if _text(task_state) == "completed" else "ready"
                    node_rows[_node_key("online_check", parser_type, str(row_key))] = _row_node(
                        task_type="online_check",
                        parser_type=parser_type,
                        row_key=str(row_key),
                        values=values,
                        owner_ref=_text(inspector),
                        status=status,
                        access_mode="editable" if data.view == "person" else "readonly",
                        relationship="owned" if data.view == "person" else "successor",
                        sync_warning=bool(_text(pending_state)),
                    )
                next_cursor = cursor + len(page)
                if next_cursor < len(independent):
                    next_cursors[parser_type] = next_cursor

        output_edges = []
        keys_by_graph_id = {}
        for node_id, record in selected_records.items():
            key = _node_key(_text(record[1]), _text(record[2]), _text(record[3]))
            keys_by_graph_id[node_id] = key
        for edge_id, predecessor, successor, state, reason_code in graph_edges:
            source = keys_by_graph_id.get(int(predecessor))
            target = keys_by_graph_id.get(int(successor))
            if source in node_rows and target in node_rows:
                output_edges.append({
                    "id": f"dependency:{edge_id}",
                    "source": source,
                    "target": target,
                    "state": _text(state),
                    "reason_code": _text(reason_code),
                    "system": True,
                    "deletable": False,
                })
        return {
            "enabled": True,
            "nodes": list(node_rows.values()),
            "edges": output_edges,
            "facets": {"total": len(node_rows), "view": data.view, "owner": projection_owner},
            "next_cursors": next_cursors,
        }
