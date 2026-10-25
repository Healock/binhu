"""PR-B1 顺序流程引擎。

流程定义只包含有序 nodes 数组。该模块不解析 edges、不创建升级副本任务，
升级始终更新同一条 process_tasks 记录并追加 process_events。
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Iterable

from services.workflow_support import platform_schema, workflow_notification


ASSIGNEE_TYPES = {
    "指定成员",
    "发起人自选",
    "表单联系人字段",
    "直属主管",
    "部门主管",
    "岗位",
    "发起人本人",
}
POSITION_ORDER_CONFIG = "process_position_order"
MAX_REJECTIONS = 3


class ProcessValidationError(ValueError):
    """流程定义或表单输入不符合 B1 契约。"""


def parse_json(value: Any, default: Any) -> Any:
    if isinstance(value, type(default)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, type(default)) else default
        except (TypeError, ValueError):
            return default
    return default


def validate_nodes(nodes: Any) -> list[dict[str, Any]]:
    """验证顺序节点；条件/并行字段可保存，但 B1 不执行。"""
    if not isinstance(nodes, list) or not nodes:
        raise ProcessValidationError("流程至少需要一个节点")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in nodes:
        if not isinstance(raw, dict):
            raise ProcessValidationError("流程节点必须是对象")
        key = str(raw.get("node_key") or "").strip()
        name = str(raw.get("name") or "").strip()
        assignee_type = str(raw.get("assignee_type") or "").strip()
        if not key or key in seen or len(key) > 100:
            raise ProcessValidationError("节点标识必须唯一且不能为空")
        if not name:
            raise ProcessValidationError("节点名称不能为空")
        if assignee_type not in ASSIGNEE_TYPES:
            raise ProcessValidationError("不支持的处理人类型")
        multi_mode = str(raw.get("multi_mode") or "single")
        if multi_mode != "single":
            raise ProcessValidationError("PR-B1 只支持 single 处理策略")
        empty_policy = str(raw.get("empty_policy") or "hold_and_notify")
        if empty_policy not in {"auto_pass", "to_admin", "hold_and_notify"}:
            raise ProcessValidationError("空处理人策略无效")
        assignee_params = raw.get("assignee_params") or {}
        if not isinstance(assignee_params, dict):
            raise ProcessValidationError("assignee_params 必须是 JSON 对象")
        timeout_hours = raw.get("timeout_hours")
        if timeout_hours is not None and (not isinstance(timeout_hours, int) or timeout_hours < 1):
            raise ProcessValidationError("timeout_hours 必须是正整数")
        normalized.append({
            "node_key": key,
            "name": name[:200],
            "assignee_type": assignee_type,
            "assignee_params": assignee_params,
            "multi_mode": multi_mode,
            "empty_policy": empty_policy,
            "timeout_hours": timeout_hours,
            "timeout_action": str(raw.get("timeout_action") or "escalate"),
            "field_permissions": raw.get("field_permissions") or {},
            "condition": raw.get("condition"),
        })
        seen.add(key)
    return normalized


def _unique_ids(values: Iterable[Any]) -> list[int]:
    result: list[int] = []
    for value in values:
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item > 0 and item not in result:
            result.append(item)
    return result


def _form_value(form_data: dict[str, Any], params: dict[str, Any]) -> list[int]:
    field = str(params.get("field") or "").strip()
    value = form_data.get(field) if field else params.get("user_id")
    if isinstance(value, dict):
        value = value.get("user_id") or value.get("id")
    if isinstance(value, (list, tuple)):
        return _unique_ids(value)
    return _unique_ids([value])


async def resolve_assignees(cur, requester_user_id: int, assignee_type: str,
                            params: dict[str, Any] | None, form_data: dict[str, Any]) -> list[int]:
    """只解析候选集合，不决定审批人。"""
    params = params or {}
    if assignee_type == "发起人本人":
        return [int(requester_user_id)]
    if assignee_type == "指定成员":
        return _unique_ids(params.get("user_ids") or [params.get("user_id")])
    if assignee_type in {"发起人自选", "表单联系人字段"}:
        return _form_value(form_data, params)
    if assignee_type == "岗位":
        position = str(params.get("position") or "").strip()
        if not position:
            return []
        department_id = params.get("department_id")
        if department_id:
            await cur.execute(
                "SELECT DISTINCT account.id FROM _grid_member_department_links link "
                "JOIN _grid_members member ON member.id=link.member_id "
                "JOIN _users account ON account.member_id=member.id "
                "WHERE link.department_id=%s AND member.position=%s AND member.status='在岗' "
                "ORDER BY account.id", (int(department_id), position),
            )
        else:
            await cur.execute(
                "SELECT account.id FROM _grid_members member "
                "JOIN _users account ON account.member_id=member.id "
                "WHERE member.position=%s AND member.status='在岗' ORDER BY account.id",
                (position,),
            )
        return _unique_ids(row[0] for row in await cur.fetchall())
    if assignee_type == "部门主管":
        department_id = params.get("department_id")
        by_department = params.get("user_ids_by_department") or {}
        configured = by_department.get(str(department_id), []) if department_id else []
        return _unique_ids(configured or params.get("user_ids") or [])
    if assignee_type == "直属主管":
        # 直属关系由部门树向上寻找；发起人没有上级部门时返回空集合，
        # 由节点 empty_policy 决定挂起或自动处理。
        await cur.execute("SELECT member_id FROM _users WHERE id=%s", (int(requester_user_id),))
        row = await cur.fetchone()
        if not row or row[0] is None:
            return []
        await cur.execute(
            "SELECT department.parent_id FROM _grid_member_department_links link "
            "JOIN _departments department ON department.id=link.department_id "
            "WHERE link.member_id=%s AND department.parent_id IS NOT NULL "
            "ORDER BY link.sort_order LIMIT 1", (int(row[0]),)
        )
        parent = await cur.fetchone()
        if not parent:
            return []
        parent_id = int(parent[0])
        by_department = params.get("user_ids_by_department") or {}
        configured = by_department.get(str(parent_id), [])
        return _unique_ids(configured or params.get("user_ids") or [])
    return []


def _instance_no() -> str:
    return "P" + datetime.utcnow().strftime("%Y%m%d%H%M%S") + secrets.token_hex(3).upper()


class ProcessEngine:
    """在一个已开启事务的 workflow cursor 上运行流程。"""

    def __init__(self, cur):
        self.cur = cur

    async def create_form(self, *, code: str, name: str, description: str = "",
                          created_by: int | None = None) -> int:
        await self.cur.execute(
            "INSERT INTO form_definitions (code,name,description,enabled,created_by,updated_by) "
            "VALUES (%s,%s,%s,0,%s,%s)",
            (code, name, description, created_by, created_by),
        )
        return int(self.cur.lastrowid)

    async def publish_form(self, form_definition_id: int, schema: dict[str, Any],
                           published_by: int | None = None) -> int:
        if not isinstance(schema, dict):
            raise ProcessValidationError("表单 schema 必须是 JSON 对象")
        await self.cur.execute("SELECT id FROM form_definitions WHERE id=%s FOR UPDATE", (form_definition_id,))
        if not await self.cur.fetchone():
            raise ProcessValidationError("表单定义不存在")
        payload = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        await self.cur.execute(
            "SELECT COALESCE(MAX(version_no),0) FROM form_versions WHERE form_definition_id=%s",
            (form_definition_id,),
        )
        version_no = int((await self.cur.fetchone())[0]) + 1
        await self.cur.execute(
            "INSERT INTO form_versions (form_definition_id,version_no,schema_json,status,published_by,published_at) "
            "VALUES (%s,%s,%s,'published',%s,UTC_TIMESTAMP())",
            (form_definition_id, version_no, payload, published_by),
        )
        version_id = int(self.cur.lastrowid)
        await self.cur.execute(
            "UPDATE form_definitions SET current_version_id=%s,enabled=1,updated_by=%s WHERE id=%s",
            (version_id, published_by, form_definition_id),
        )
        return version_id

    async def create_process(self, *, code: str, name: str, description: str = "",
                             form_definition_id: int | None = None,
                             module_id: str = "", default_due_hours: int | None = None,
                             created_by: int | None = None) -> int:
        await self.cur.execute(
            "INSERT INTO process_definitions "
            "(code,name,description,form_definition_id,module_id,default_due_hours,enabled,created_by,updated_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,0,%s,%s)",
            (code, name, description, form_definition_id, module_id, default_due_hours,
             created_by, created_by),
        )
        return int(self.cur.lastrowid)

    async def publish_process(self, process_definition_id: int, nodes: Any,
                              published_by: int | None = None) -> int:
        normalized = validate_nodes(nodes)
        await self.cur.execute(
            "SELECT id FROM process_definitions WHERE id=%s FOR UPDATE", (process_definition_id,)
        )
        if not await self.cur.fetchone():
            raise ProcessValidationError("流程定义不存在")
        await self.cur.execute(
            "SELECT COALESCE(MAX(version_no),0) FROM process_versions WHERE process_definition_id=%s",
            (process_definition_id,),
        )
        version_no = int((await self.cur.fetchone())[0]) + 1
        payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
        await self.cur.execute(
            "INSERT INTO process_versions (process_definition_id,version_no,nodes_json,status,published_by,published_at) "
            "VALUES (%s,%s,%s,'published',%s,UTC_TIMESTAMP())",
            (process_definition_id, version_no, payload, published_by),
        )
        version_id = int(self.cur.lastrowid)
        await self.cur.execute(
            "UPDATE process_definitions SET current_version_id=%s, enabled=1, updated_by=%s WHERE id=%s",
            (version_id, published_by, process_definition_id),
        )
        return version_id

    async def _version(self, version_id: int) -> tuple[int, list[dict[str, Any]]]:
        await self.cur.execute(
            "SELECT version.process_definition_id,version.nodes_json,version.status,definition.enabled "
            "FROM process_versions version JOIN process_definitions definition "
            "ON definition.id=version.process_definition_id WHERE version.id=%s",
            (version_id,),
        )
        row = await self.cur.fetchone()
        if not row or str(row[2]) != "published" or not bool(row[3]):
            raise ProcessValidationError("流程版本不存在或未发布")
        return int(row[0]), validate_nodes(parse_json(row[1], []))

    async def _event(self, instance_id: int, task_id: int | None, event_type: str,
                     actor: int | None, from_status: str = "", to_status: str = "",
                     detail: dict[str, Any] | None = None) -> None:
        await self.cur.execute(
            "INSERT INTO process_events (instance_id,task_id,event_type,actor_user_id,from_status,to_status,detail_json) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (instance_id, task_id, event_type, actor, from_status, to_status,
             json.dumps(detail or {}, ensure_ascii=False)),
        )

    async def _create_task(self, instance_id: int, node: dict[str, Any], order: int,
                           requester: int, form_data: dict[str, Any], *, now=None) -> int | None:
        assignees = await resolve_assignees(self.cur, requester, node["assignee_type"],
                                            node.get("assignee_params"), form_data)
        if not assignees:
            policy = node.get("empty_policy") or "hold_and_notify"
            if policy == "auto_pass":
                await self.cur.execute(
                    "SELECT process_version_id FROM process_instances WHERE id=%s", (instance_id,)
                )
                instance_row = await self.cur.fetchone()
                _, nodes = await self._version(int(instance_row[0]))
                index = next(i for i, item in enumerate(nodes) if item["node_key"] == node["node_key"])
                if index + 1 < len(nodes):
                    next_node = nodes[index + 1]
                    await self.cur.execute(
                        "UPDATE process_instances SET current_node_key=%s WHERE id=%s",
                        (next_node["node_key"], instance_id),
                    )
                    await self._event(instance_id, None, "empty_auto_pass", None,
                                      "running", "running", {"node_key": node["node_key"]})
                    return await self._create_task(
                        instance_id, next_node, order + 1, requester, form_data, now=now,
                    )
                status = "completed"
            else:
                status = "admin_hold"
            completion_time = (now or datetime.utcnow()) if status == "completed" else None
            await self.cur.execute(
                "UPDATE process_instances SET status=%s,current_node_key=%s,completed_at=%s WHERE id=%s",
                (status, "" if status == "completed" else node["node_key"], completion_time, instance_id),
            )
            await self._event(instance_id, None, "admin_intervention_required" if status == "admin_hold" else "empty_auto_pass",
                              None, "running", status, {"node_key": node["node_key"]})
            if status == "completed":
                return None
            await self._notify_admins(instance_id, "流程节点无人可处理，需管理员介入")
            return None
        now = now or datetime.utcnow()
        await self.cur.execute(
            "SELECT definition.default_due_hours FROM process_instances instance "
            "JOIN process_definitions definition ON definition.id=instance.process_definition_id "
            "WHERE instance.id=%s", (instance_id,),
        )
        due_config = await self.cur.fetchone()
        default_due_hours = due_config[0] if due_config else None
        due = now + timedelta(hours=int(node.get("timeout_hours") or default_due_hours or 24))
        await self.cur.execute(
            "INSERT INTO process_tasks (instance_id,node_key,node_name,task_order,status,assignees,primary_assignee_user_id,due_at) "
            "VALUES (%s,%s,%s,%s,'pending',%s,%s,%s)",
            (instance_id, node["node_key"], node["name"], order,
             json.dumps(assignees), assignees[0], due),
        )
        task_id = int(self.cur.lastrowid)
        await self._event(instance_id, task_id, "task_created", requester, "", "pending",
                          {"node_key": node["node_key"], "assignees": assignees})
        return task_id

    async def _notify_admins(self, instance_id: int, content: str) -> None:
        schema = platform_schema().replace("`", "")
        await self.cur.execute(
            f"SELECT id FROM `{schema}`._users WHERE role IN ('admin','super_admin') ORDER BY id"
        )
        admins = _unique_ids(row[0] for row in await self.cur.fetchall())
        await workflow_notification(
            self.cur, user_ids=admins, ticket_id=instance_id,
            event_key=f"process_admin_{instance_id}", title="流程需要管理员介入",
            content=content, severity="warning",
        )

    async def _position_order(self) -> list[str]:
        """读取平台域中的岗位顺序配置。

        ``_system_config`` 在 PLATFORM_DOMAIN_ACTIVE 开启时由域路由改写到
        PlatformData；继续使用当前工作流游标可以让查询与事务保持同一请求
        生命周期，也避免从另一个连接池跨域读取。
        """
        await self.cur.execute(
            "SELECT config_value FROM _system_config WHERE config_key=%s",
            (POSITION_ORDER_CONFIG,),
        )
        row = await self.cur.fetchone()
        return parse_json(row[0], []) if row else []

    async def start(self, *, process_version_id: int, requester_user_id: int,
                    title: str, description: str = "", form_data: dict[str, Any] | None = None,
                    now=None) -> dict[str, Any]:
        process_id, nodes = await self._version(process_version_id)
        data = dict(form_data or {})
        instance_no = _instance_no()
        await self.cur.execute(
            "INSERT INTO process_instances (instance_no,process_definition_id,process_version_id,title,description,requester_user_id,status,current_node_key,form_data,submitted_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,'running',%s,%s,%s)",
            (instance_no, process_id, process_version_id, title, description,
             requester_user_id, nodes[0]["node_key"], json.dumps(data, ensure_ascii=False), now or datetime.utcnow()),
        )
        instance_id = int(self.cur.lastrowid)
        await self._event(instance_id, None, "instance_submitted", requester_user_id,
                          "", "running", {"form_snapshot": data})
        await self._create_task(instance_id, nodes[0], 0, requester_user_id, data, now=now)
        return {"id": instance_id, "instance_no": instance_no, "process_version_id": process_version_id}

    async def _nodes_for_instance(self, instance_id: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        await self.cur.execute(
            "SELECT process_version_id,requester_user_id,form_data,status FROM process_instances WHERE id=%s FOR UPDATE",
            (instance_id,),
        )
        row = await self.cur.fetchone()
        if not row:
            raise ProcessValidationError("流程实例不存在")
        _, nodes = await self._version(int(row[0]))
        return nodes, {"version_id": int(row[0]), "requester": int(row[1]),
                       "form_data": parse_json(row[2], {}), "status": str(row[3])}

    async def decide(self, *, task_id: int, actor_user_id: int, decision: str,
                     note: str = "", now=None) -> dict[str, Any]:
        await self.cur.execute(
            "SELECT id,instance_id,node_key,task_order,status,assignees,node_name FROM process_tasks WHERE id=%s FOR UPDATE",
            (task_id,),
        )
        row = await self.cur.fetchone()
        if not row:
            raise ProcessValidationError("任务不存在")
        assignees = _unique_ids(parse_json(row[5], []))
        if str(row[4]) != "pending":
            raise ProcessValidationError("任务已处理")
        if int(actor_user_id) not in assignees:
            raise ProcessValidationError("当前账号不在任务处理人快照中")
        instance_id = int(row[1])
        nodes, instance = await self._nodes_for_instance(instance_id)
        node_index = next((i for i, node in enumerate(nodes) if node["node_key"] == row[2]), None)
        if node_index is None:
            raise ProcessValidationError("任务节点不在流程版本中")
        now = now or datetime.utcnow()
        if decision in {"reject", "return"}:
            await self.cur.execute(
                "UPDATE process_tasks SET status='returned',decision=%s,decision_note=%s,decided_by=%s,decided_at=%s,returned_at=%s,revision_no=revision_no+1 WHERE id=%s",
                (decision, note, actor_user_id, now, now, task_id),
            )
            await self.cur.execute(
                "SELECT COUNT(*) FROM process_events WHERE instance_id=%s AND event_type='task_returned'",
                (instance_id,),
            )
            count = int((await self.cur.fetchone())[0]) + 1
            status = "admin_hold" if count >= MAX_REJECTIONS else "returned"
            await self.cur.execute("UPDATE process_instances SET status=%s WHERE id=%s", (status, instance_id))
            await self._event(instance_id, task_id, "task_returned", actor_user_id, "pending", status,
                              {"rejection_count": count, "note": note})
            if status == "admin_hold":
                await self._notify_admins(instance_id, "流程已连续退回三次，需管理员处理")
            return {"instance_id": instance_id, "status": status, "rejection_count": count}
        if decision not in {"approve", "complete"}:
            raise ProcessValidationError("不支持的处理决定")
        await self.cur.execute(
            "UPDATE process_tasks SET status='completed',decision=%s,decision_note=%s,decided_by=%s,decided_at=%s,revision_no=revision_no+1 WHERE id=%s",
            (decision, note, actor_user_id, now, task_id),
        )
        if node_index + 1 >= len(nodes):
            await self.cur.execute("UPDATE process_instances SET status='completed',completed_at=%s,current_node_key='' WHERE id=%s", (now, instance_id))
            target_status = "completed"
        else:
            next_node = nodes[node_index + 1]
            await self.cur.execute("UPDATE process_instances SET current_node_key=%s,revision_no=revision_no+1 WHERE id=%s", (next_node["node_key"], instance_id))
            await self._create_task(instance_id, next_node, int(row[3]) + 1, instance["requester"], instance["form_data"], now=now)
            await self.cur.execute("SELECT status FROM process_instances WHERE id=%s", (instance_id,))
            target_status = str((await self.cur.fetchone())[0])
        await self._event(instance_id, task_id, "task_decided", actor_user_id, "pending", target_status,
                          {"decision": decision, "note": note})
        return {"instance_id": instance_id, "status": target_status}

    async def resubmit(self, *, instance_id: int, requester_user_id: int,
                       form_data: dict[str, Any], now=None) -> dict[str, Any]:
        nodes, instance = await self._nodes_for_instance(instance_id)
        if instance["requester"] != int(requester_user_id) or instance["status"] != "returned":
            raise ProcessValidationError("只有被退回的本人可以重提")
        await self.cur.execute(
            "SELECT node_key,task_order FROM process_tasks WHERE instance_id=%s AND status='returned' ORDER BY id DESC LIMIT 1",
            (instance_id,),
        )
        row = await self.cur.fetchone()
        if not row:
            raise ProcessValidationError("没有可重提的退回任务")
        node = next((item for item in nodes if item["node_key"] == row[0]), None)
        if not node:
            raise ProcessValidationError("退回节点不存在")
        await self.cur.execute("UPDATE process_instances SET status='running',form_data=%s,current_node_key=%s,revision_no=revision_no+1 WHERE id=%s",
                               (json.dumps(form_data, ensure_ascii=False), node["node_key"], instance_id))
        task_id = await self._create_task(instance_id, node, int(row[1]), requester_user_id, form_data, now=now)
        await self._event(instance_id, task_id, "task_resubmitted", requester_user_id, "returned", "running",
                          {"form_snapshot": form_data})
        return {"instance_id": instance_id, "task_id": task_id, "status": "running"}

    async def advance_timeouts(self, *, now=None) -> int:
        now = now or datetime.utcnow()
        await self.cur.execute(
            "SELECT id,instance_id,node_key,status,assignees,primary_assignee_user_id,escalation_stage,due_at FROM process_tasks "
            "WHERE status='pending' AND due_at IS NOT NULL AND due_at<=%s FOR UPDATE", (now,)
        )
        rows = await self.cur.fetchall()
        changed = 0
        for row in rows:
            task_id, instance_id, node_key, _, assignees_json, primary, stage, _ = row
            nodes, instance = await self._nodes_for_instance(int(instance_id))
            node = next((item for item in nodes if item["node_key"] == node_key), None)
            if not node:
                continue
            candidates: list[int] = []
            next_stage = int(stage) + 1
            if int(stage) == 0 and primary:
                await self.cur.execute(
                    "SELECT member.id,member.position FROM _users account "
                    "JOIN _grid_members member ON member.id=account.member_id WHERE account.id=%s",
                    (primary,),
                )
                primary_member = await self.cur.fetchone()
                if primary_member:
                    primary_member_id, position = primary_member
                    await self.cur.execute(
                        "SELECT DISTINCT peer_account.id FROM _grid_member_department_links link1 "
                        "JOIN _grid_member_department_links link2 ON link2.department_id=link1.department_id "
                        "JOIN _grid_members member2 ON member2.id=link2.member_id "
                        "JOIN _users peer_account ON peer_account.member_id=member2.id "
                        "WHERE link1.member_id=%s AND member2.position=%s AND member2.status='在岗' "
                        "AND member2.id<>%s ORDER BY peer_account.id",
                        (primary_member_id, position, primary_member_id),
                    )
                    candidates = _unique_ids(x[0] for x in await self.cur.fetchall())
            elif int(stage) >= 1:
                # 岗位顺序表是独立配置；空表按 D8 直接挂起。
                order = await self._position_order()
                promotion_source = int(primary) if primary else 0
                if int(stage) >= 2:
                    await self.cur.execute(
                        "SELECT detail_json FROM process_events WHERE task_id=%s AND event_type='task_escalated' "
                        "ORDER BY id DESC LIMIT 1", (task_id,),
                    )
                    escalation_event = await self.cur.fetchone()
                    detail = parse_json(escalation_event[0], {}) if escalation_event else {}
                    prior_added = _unique_ids(detail.get("added_assignees") or [])
                    if prior_added:
                        promotion_source = prior_added[-1]
                if order and promotion_source:
                    await self.cur.execute(
                        "SELECT member.id,member.position FROM _users account "
                        "JOIN _grid_members member ON member.id=account.member_id WHERE account.id=%s",
                        (promotion_source,),
                    )
                    current = await self.cur.fetchone()
                    if current and current[1] in order and order.index(current[1]) > 0:
                        member_id = int(current[0])
                        target = order[order.index(current[1]) - 1]
                        await self.cur.execute(
                            "SELECT DISTINCT target_account.id FROM _grid_member_department_links primary_link "
                            "JOIN _grid_member_department_links target_link ON target_link.department_id=primary_link.department_id "
                            "JOIN _grid_members target_member ON target_member.id=target_link.member_id "
                            "JOIN _users target_account ON target_account.member_id=target_member.id "
                            "WHERE primary_link.member_id=%s AND target_member.position=%s "
                            "AND target_member.status='在岗' AND target_member.id<>%s ORDER BY target_account.id",
                            (member_id, target, member_id),
                        )
                        candidates = _unique_ids(x[0] for x in await self.cur.fetchall())
            if candidates:
                merged = _unique_ids([*parse_json(assignees_json, []), *candidates])
                due = now + timedelta(hours=int(node.get("timeout_hours") or 24))
                await self.cur.execute("UPDATE process_tasks SET assignees=%s,escalation_stage=%s,due_at=%s,revision_no=revision_no+1 WHERE id=%s",
                                       (json.dumps(merged), next_stage, due, task_id))
                await self._event(int(instance_id), int(task_id), "task_escalated", None, "pending", "pending",
                                  {"stage": next_stage, "added_assignees": candidates})
            elif next_stage >= 2:
                await self.cur.execute("UPDATE process_tasks SET status='admin_hold',revision_no=revision_no+1 WHERE id=%s", (task_id,))
                await self.cur.execute("UPDATE process_instances SET status='admin_hold' WHERE id=%s", (instance_id,))
                await self._event(int(instance_id), int(task_id), "admin_intervention_required", None, "pending", "admin_hold",
                                  {"reason": "岗位序列为空或没有可用上级"})
                await self._notify_admins(int(instance_id), "岗位顺序表为空或没有可用上级，流程已挂起")
            else:
                # T1 只扩大同岗位候选；即使没有其他同事，也继续等待到 T2。
                due = now + timedelta(hours=int(node.get("timeout_hours") or 24))
                await self.cur.execute(
                    "UPDATE process_tasks SET escalation_stage=1,due_at=%s,revision_no=revision_no+1 WHERE id=%s",
                    (due, task_id),
                )
                await self._event(int(instance_id), int(task_id), "same_position_window_opened", None,
                                  "pending", "pending", {"stage": 1, "added_assignees": []})
            changed += 1
        return changed

    async def add_attachment(self, *, instance_id: int, file_id: str, storage_key: str,
                             sha256: str, task_id: int | None = None,
                             classification: str = "ordinary", retention_until=None) -> int:
        await self.cur.execute(
            "INSERT INTO process_attachments (instance_id,task_id,file_id,storage_key,sha256,classification,retention_until) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (instance_id, task_id, file_id, storage_key, sha256, classification, retention_until),
        )
        return int(self.cur.lastrowid)
