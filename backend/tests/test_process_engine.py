from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.process_engine import ProcessEngine, ProcessValidationError, validate_nodes
from services.process_engine import resolve_assignees


def test_nodes_are_a_sequence_and_only_single_is_executable():
    nodes = [{
        "node_key": "approve", "name": "科长审批", "assignee_type": "指定成员",
        "assignee_params": {"user_id": 7}, "multi_mode": "single",
        "condition": {"reserved": True},
    }]
    assert validate_nodes(nodes)[0]["node_key"] == "approve"
    with pytest.raises(ProcessValidationError, match="single"):
        validate_nodes([{**nodes[0], "multi_mode": "all"}])
    with pytest.raises(ProcessValidationError, match="唯一"):
        validate_nodes([nodes[0], nodes[0]])


def test_workflow_domain_schema_contains_the_eight_b1_tables():
    source = (Path(__file__).resolve().parents[1] / "services" / "domain_schema.py").read_text(encoding="utf-8")
    for table in (
        "form_definitions", "form_versions", "process_definitions", "process_versions",
        "process_instances", "process_tasks", "process_events", "process_attachments",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in source
    assert "process_version_id BIGINT NOT NULL" in source
    assert "file_id CHAR(36) NOT NULL UNIQUE" in source
    assert "sha256 CHAR(64) NOT NULL" in source


@pytest.mark.asyncio
async def test_department_manager_candidates_are_explicitly_configured_per_node():
    class NoQueryCursor:
        async def execute(self, *_args, **_kwargs):
            raise AssertionError("explicit department manager candidates need no guessed role query")

    candidates = await resolve_assignees(
        NoQueryCursor(), 7, "部门主管",
        {"department_id": 22, "user_ids_by_department": {"22": [31, 32, 31]}}, {},
    )
    assert candidates == [31, 32]


@pytest.mark.asyncio
async def test_position_candidates_return_account_ids_not_member_ids():
    class PositionCursor:
        async def execute(self, query, params=()):
            assert "JOIN _users account ON account.member_id=member.id" in query
            self.params = params

        async def fetchall(self):
            return [(901,), (902,)]

    assert await resolve_assignees(PositionCursor(), 7, "岗位", {"position": "组长"}, {}) == [901, 902]


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _MemoryCursor:
    """Small SQL-boundary fake for end-to-end definition/start/decision checks."""

    def __init__(self):
        self.lastrowid = 0
        self._one = None
        self._all = []
        self.processes = {}
        self.versions = {}
        self.instances = {}
        self.tasks = {}
        self.events = []
        self.member_positions = {11: "组长", 12: "组员", 20: "组长"}
        self.user_member_ids = {11: 11, 12: 12, 20: 20, 101: 11, 102: 12, 120: 20}
        self.department_peers = {11: [12], 12: [11]}
        self.promotion_candidates = {}
        self.position_order = []
        self.next_id = 0

    def _id(self):
        self.next_id += 1
        self.lastrowid = self.next_id
        return self.next_id

    async def execute(self, sql, params=()):
        query = " ".join(sql.split())
        self._one, self._all = None, []
        if query.startswith("INSERT INTO process_definitions"):
            id_ = self._id()
            self.processes[id_] = {"code": params[0], "enabled": 0}
        elif query.startswith("SELECT id FROM process_definitions"):
            self._one = (params[0],) if params[0] in self.processes else None
        elif query.startswith("SELECT COALESCE(MAX(version_no),0) FROM process_versions"):
            self._one = (max((v["version_no"] for v in self.versions.values()
                              if v["process_id"] == params[0]), default=0),)
        elif query.startswith("INSERT INTO process_versions"):
            id_ = self._id()
            self.versions[id_] = {"process_id": params[0], "version_no": params[1],
                                  "nodes": json.loads(params[2]), "status": "published"}
        elif query.startswith("UPDATE process_definitions SET current_version_id"):
            self.processes[params[2]].update({"version_id": params[0], "enabled": 1})
        elif query.startswith("SELECT version.process_definition_id,version.nodes_json,version.status"):
            v = self.versions.get(params[0])
            enabled = self.processes[v["process_id"]]["enabled"] if v else False
            self._one = (v["process_id"], json.dumps(v["nodes"]), v["status"], enabled) if v else None
        elif query.startswith("INSERT INTO process_instances"):
            id_ = self._id()
            self.instances[id_] = {"no": params[0], "process_id": params[1], "version_id": params[2],
                                   "requester": params[5], "status": "running", "form_data": json.loads(params[7]),
                                   "node": params[6]}
        elif query.startswith("INSERT INTO process_events"):
            id_ = self._id()
            self.events.append({"id": id_, "instance": params[0], "task": params[1],
                                "type": params[2], "detail": json.loads(params[6])})
        elif query.startswith("INSERT INTO process_tasks"):
            id_ = self._id()
            self.tasks[id_] = {"instance": params[0], "node": params[1], "name": params[2],
                               "order": params[3], "status": "pending", "assignees": json.loads(params[4]),
                               "primary": params[5], "stage": 0, "due": params[6]}
        elif query.startswith("SELECT id,instance_id,node_key,task_order,status,assignees,node_name FROM process_tasks"):
            t = self.tasks.get(params[0])
            self._one = (params[0], t["instance"], t["node"], t["order"], t["status"],
                         json.dumps(t["assignees"]), t["name"]) if t else None
        elif query.startswith("SELECT process_version_id,requester_user_id,form_data,status FROM process_instances"):
            i = self.instances.get(params[0])
            self._one = (i["version_id"], i["requester"], json.dumps(i["form_data"]), i["status"]) if i else None
        elif query.startswith("SELECT definition.default_due_hours FROM process_instances"):
            self._one = (None,)
        elif query.startswith("UPDATE process_tasks SET status='completed'"):
            self.tasks[params[4]].update({"status": "completed", "decision": params[0]})
        elif query.startswith("UPDATE process_instances SET status='completed'"):
            self.instances[params[1]]["status"] = "completed"
        elif query.startswith("UPDATE process_instances SET current_node_key"):
            self.instances[params[1]]["node"] = params[0]
        elif query.startswith("UPDATE process_tasks SET status='returned'"):
            self.tasks[params[5]].update({"status": "returned", "decision": params[0]})
        elif query.startswith("SELECT COUNT(*) FROM process_events"):
            self._one = (sum(event["type"] == "task_returned" and event["instance"] == params[0]
                             for event in self.events),)
        elif query.startswith("UPDATE process_instances SET status=%s WHERE id=%s"):
            self.instances[params[1]]["status"] = params[0]
        elif query.startswith("SELECT node_key,task_order FROM process_tasks"):
            matched = [task for task in self.tasks.values()
                       if task["instance"] == params[0] and task["status"] == "returned"]
            task = matched[-1] if matched else None
            self._one = (task["node"], task["order"]) if task else None
        elif query.startswith("UPDATE process_instances SET status='running',form_data="):
            self.instances[params[2]].update({"status": "running", "form_data": json.loads(params[0]),
                                             "node": params[1]})
        elif query.startswith("SELECT id,instance_id,node_key,status,assignees,primary_assignee_user_id,escalation_stage,due_at FROM process_tasks"):
            self._all = [
                (task_id, task["instance"], task["node"], task["status"], json.dumps(task["assignees"]),
                 task["primary"], task["stage"], task["due"])
                for task_id, task in self.tasks.items()
                if task["status"] == "pending" and task["due"] is not None and task["due"] <= params[0]
            ]
        elif query.startswith("SELECT position FROM _grid_members WHERE id="):
            position = self.member_positions.get(int(params[0]))
            self._one = (position,) if position else None
        elif query.startswith("SELECT member.id,member.position FROM _users account"):
            member_id = self.user_member_ids.get(int(params[0]))
            position = self.member_positions.get(member_id)
            self._one = (member_id, position) if member_id and position else None
        elif query.startswith("SELECT DISTINCT link2.member_id"):
            self._all = [(member_id,) for member_id in self.department_peers.get(int(params[0]), [])
                         if member_id != int(params[-1])]
        elif query.startswith("SELECT DISTINCT peer_account.id"):
            member_id, position, excluded_member_id = int(params[0]), str(params[1]), int(params[2])
            account_ids = [user_id for user_id, peer_member_id in self.user_member_ids.items()
                           if peer_member_id in self.department_peers.get(member_id, [])
                           and peer_member_id != excluded_member_id
                           and self.member_positions.get(peer_member_id) == position]
            self._all = [(user_id,) for user_id in sorted(account_ids)]
        elif query.startswith("SELECT config_value FROM _system_config"):
            self._one = (json.dumps(self.position_order),) if self.position_order else None
        elif query.startswith("SELECT detail_json FROM process_events"):
            matches = [event for event in self.events if event["task"] == params[0]
                       and event["type"] == "task_escalated"]
            self._one = (json.dumps(matches[-1]["detail"]),) if matches else None
        elif query.startswith("SELECT DISTINCT target_link.member_id"):
            self._all = [(member_id,) for member_id in self.promotion_candidates.get(
                (int(params[0]), str(params[1])), [])]
        elif query.startswith("SELECT DISTINCT target_account.id"):
            member_id, position = int(params[0]), str(params[1])
            candidate_members = self.promotion_candidates.get((member_id, position), [])
            account_ids = [user_id for user_id, candidate_member in self.user_member_ids.items()
                           if candidate_member in candidate_members]
            self._all = [(user_id,) for user_id in sorted(account_ids)]
        elif query.startswith("UPDATE process_tasks SET assignees="):
            self.tasks[params[3]].update({"assignees": json.loads(params[0]), "stage": params[1], "due": params[2]})
        elif query.startswith("UPDATE process_tasks SET escalation_stage="):
            self.tasks[params[1]].update({"stage": 1, "due": params[0]})
        elif query.startswith("UPDATE process_tasks SET status='admin_hold'"):
            self.tasks[params[0]]["status"] = "admin_hold"
        elif query.startswith("UPDATE process_instances SET status='admin_hold'"):
            self.instances[params[0]]["status"] = "admin_hold"
        elif query.startswith("SELECT id FROM `") and "_users WHERE role IN" in query:
            self._all = [(1,), (2,)]
        elif query.startswith("INSERT IGNORE INTO `") and "_notifications" in query:
            pass
        elif query.startswith("SELECT id,code,name,description"):
            self._all = []
        else:
            raise AssertionError(f"unexpected SQL: {query}")

    async def fetchone(self):
        return self._one

    async def fetchall(self):
        return self._all


class _Connection:
    def __init__(self):
        self.cursor_value = _MemoryCursor()

    def cursor(self):
        return _CursorContext(self.cursor_value)

    async def commit(self):
        pass


@pytest.mark.asyncio
async def test_two_json_processes_run_without_process_specific_code():
    conn = _Connection()
    cursor = conn.cursor_value
    async with conn.cursor() as cur:
        engine = ProcessEngine(cur)
        specs = [
            ("fake_leave", "提交 → 科长审批 → 完成", 11),
            ("fake_visit", "提交 → 组长审批 → 完成", 12),
        ]
        for code, approver_name, approver in specs:
            definition = await engine.create_process(code=code, name=code)
            version = await engine.publish_process(definition, [{
                "node_key": "approval", "name": approver_name, "assignee_type": "指定成员",
                "assignee_params": {"user_id": approver}, "timeout_hours": 24,
            }])
            instance = await engine.start(process_version_id=version, requester_user_id=3,
                                          title=code, form_data={"reason": code})
            task_id = next(task_id for task_id, task in cursor.tasks.items()
                           if task["instance"] == instance["id"])
            result = await engine.decide(task_id=task_id, actor_user_id=approver, decision="approve")
            assert result["status"] == "completed"
            assert cursor.instances[instance["id"]]["version_id"] == version
        assert [item["type"] for item in cursor.events].count("instance_submitted") == 2
        assert len(cursor.tasks) == 2


@pytest.mark.asyncio
async def test_timeout_keeps_one_task_and_empty_position_order_holds_and_notifies():
    conn = _Connection()
    cursor = conn.cursor_value
    async with conn.cursor() as cur:
        engine = ProcessEngine(cur)
        definition = await engine.create_process(code="fake_escalation", name="假流程")
        cursor.user_member_ids = {101: 11, 102: 12}
        cursor.member_positions[12] = "组长"
        version = await engine.publish_process(definition, [{
            "node_key": "approval", "name": "直属主管审批", "assignee_type": "指定成员",
            "assignee_params": {"user_id": 101}, "timeout_hours": 1,
        }])
        now = datetime(2026, 10, 23, 12)
        instance = await engine.start(process_version_id=version, requester_user_id=3,
                                      title="假流程", now=now - timedelta(hours=3))
        task_id = next(iter(cursor.tasks))
        assert await engine.advance_timeouts(now=now - timedelta(hours=1)) == 1
        assert cursor.tasks[task_id]["stage"] == 1
        assert cursor.tasks[task_id]["assignees"] == [101, 102]
        cursor.tasks[task_id]["due"] = now - timedelta(seconds=1)
        assert await engine.advance_timeouts(now=now) == 1
        assert cursor.tasks[task_id]["status"] == "admin_hold"
        assert cursor.instances[instance["id"]]["status"] == "admin_hold"
        assert len([task for task in cursor.tasks.values() if task["instance"] == instance["id"]]) == 1
        assert any(event["type"] == "admin_intervention_required" for event in cursor.events)


@pytest.mark.asyncio
async def test_position_sequence_escalates_same_task_one_rank_per_timeout_then_holds():
    conn = _Connection()
    cursor = conn.cursor_value
    cursor.member_positions[13] = "中队长"
    cursor.department_peers[11] = [20]
    cursor.member_positions[20] = "组长"
    cursor.promotion_candidates[(11, "组员")] = [12]
    cursor.user_member_ids = {101: 11, 102: 12, 120: 20}
    async with conn.cursor() as cur:
        engine = ProcessEngine(cur)
        definition = await engine.create_process(code="fake_position_order", name="岗位序列假流程")
        version = await engine.publish_process(definition, [{
            "node_key": "approval", "name": "领导审批", "assignee_type": "指定成员",
            "assignee_params": {"user_id": 101}, "timeout_hours": 1,
        }])
        now = datetime(2026, 10, 23, 12)
        instance = await engine.start(process_version_id=version, requester_user_id=3,
                                      title="假流程", now=now - timedelta(hours=3))
        task_id = next(iter(cursor.tasks))
        cursor.position_order = ["组员", "组长", "中队长"]
        await engine.advance_timeouts(now=now - timedelta(hours=1))
        assert cursor.tasks[task_id]["assignees"] == [101, 120]
        cursor.tasks[task_id]["due"] = now - timedelta(seconds=1)
        await engine.advance_timeouts(now=now)
        assert cursor.tasks[task_id]["assignees"] == [101, 120, 102]
        assert cursor.tasks[task_id]["stage"] == 2
        cursor.tasks[task_id]["due"] = now + timedelta(hours=1)
        await engine.advance_timeouts(now=now + timedelta(hours=2))
        assert cursor.tasks[task_id]["status"] == "admin_hold"
        assert len([task for task in cursor.tasks.values() if task["instance"] == instance["id"]]) == 1


@pytest.mark.asyncio
async def test_return_and_resubmit_preserves_instance_and_snapshots_each_submission():
    conn = _Connection()
    cursor = conn.cursor_value
    async with conn.cursor() as cur:
        engine = ProcessEngine(cur)
        definition = await engine.create_process(code="fake_return", name="假退回流程")
        version = await engine.publish_process(definition, [{
            "node_key": "approval", "name": "领导审批", "assignee_type": "表单联系人字段",
            "assignee_params": {"field": "leader"},
        }])
        instance = await engine.start(process_version_id=version, requester_user_id=3,
                                      title="假流程", form_data={"leader": 11, "revision": 1})
        first_task = next(iter(cursor.tasks))
        result = await engine.decide(task_id=first_task, actor_user_id=11, decision="reject", note="补充")
        assert result["instance_id"] == instance["id"]
        resubmitted = await engine.resubmit(instance_id=instance["id"], requester_user_id=3,
                                            form_data={"leader": 12, "revision": 2})
        assert resubmitted["instance_id"] == instance["id"]
        assert resubmitted["task_id"] != first_task
        assert cursor.tasks[resubmitted["task_id"]]["stage"] == 0
        assert cursor.tasks[resubmitted["task_id"]]["assignees"] == [12]
        snapshots = [event["detail"]["form_snapshot"] for event in cursor.events
                     if event["type"] in {"instance_submitted", "task_resubmitted"}]
        assert snapshots == [{"leader": 11, "revision": 1}, {"leader": 12, "revision": 2}]
        second_task = resubmitted["task_id"]
        await engine.decide(task_id=second_task, actor_user_id=12, decision="reject", note="仍需补充")
        second_resubmit = await engine.resubmit(
            instance_id=instance["id"], requester_user_id=3,
            form_data={"leader": 13, "revision": 3},
        )
        third_rejection = await engine.decide(
            task_id=second_resubmit["task_id"], actor_user_id=13,
            decision="reject", note="三次退回",
        )
        assert third_rejection["status"] == "admin_hold"
        assert third_rejection["rejection_count"] == 3
        assert cursor.instances[instance["id"]]["status"] == "admin_hold"
