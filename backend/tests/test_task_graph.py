import asyncio
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.task_graph import (
    _ensure_node,
    _resolve_online_owner,
    _set_dependency,
    analysis_graph_state,
    chain_should_archive,
    online_task_blocked,
    task_graph_preview,
)
from routers.task_graph import task_graph_access


class PreviewCursor:
    def __init__(self, rows, mappings=None):
        self.rows = rows
        self.mappings = mappings or []
        self.query = ""

    async def execute(self, query, params=None):
        self.query = " ".join(query.split())

    async def fetchall(self):
        return self.mappings if "_grid_members" in self.query else self.rows


class TaskGraphTests(unittest.TestCase):
    def test_unable_to_verify_creates_active_analysis_dependency(self):
        state = analysis_graph_state("全链条", {"核查结果": "无法核实", "研判": ""})
        self.assertEqual(state, {
            "online_status": "blocked",
            "analysis_status": "ready",
            "dependency_state": "active",
        })

    def test_analysis_completion_unlocks_original_task(self):
        state = analysis_graph_state("全链条", {"核查结果": "无法核实", "研判": "建议重新入户"})
        self.assertEqual(state, {
            "online_status": "ready",
            "analysis_status": "completed",
            "dependency_state": "satisfied",
        })

    def test_clearing_analysis_reopens_dependency(self):
        completed = analysis_graph_state("全链条", {"核查结果": "无法核实", "研判": "已研判"})
        reopened = analysis_graph_state("全链条", {"核查结果": "无法核实", "研判": ""}, existing=True)
        self.assertEqual(completed["dependency_state"], "satisfied")
        self.assertEqual(reopened["dependency_state"], "active")
        self.assertEqual(reopened["online_status"], "blocked")

    def test_final_result_cancels_unfinished_analysis_and_archives_only_terminal_chain(self):
        state = analysis_graph_state("全链条", {"核查结果": "已登记", "研判": ""}, existing=True)
        self.assertEqual(state["online_status"], "completed")
        self.assertEqual(state["analysis_status"], "cancelled")
        self.assertEqual(state["dependency_state"], "cancelled")
        self.assertTrue(chain_should_archive({"completed", "cancelled"}, "cancelled"))
        self.assertFalse(chain_should_archive({"ready", "completed"}, "satisfied"))
        self.assertFalse(chain_should_archive({"completed"}, "active"))

    def test_unrelated_task_does_not_create_graph_without_existing_chain(self):
        self.assertIsNone(analysis_graph_state("全链条", {"核查结果": "", "研判": ""}))
        state = analysis_graph_state("全链条", {"核查结果": "", "研判": ""}, existing=True)
        self.assertEqual(state["online_status"], "ready")
        self.assertEqual(state["analysis_status"], "cancelled")

    def test_existing_node_and_dependency_are_idempotent(self):
        node_cursor = AsyncMock()
        node_cursor.fetchone = AsyncMock(return_value=(7, "online:key", "ready", None, None, "user", "甲"))
        node = asyncio.run(_ensure_node(
            node_cursor,
            task_type="online_check",
            parser_type="全链条",
            row_key="row-key",
            owner_type="user",
            owner_ref="甲",
            status="ready",
            reason_code="mobile_task",
            actor_user_id=1,
            event_type="test",
        ))
        self.assertFalse(node["changed"])
        self.assertEqual(node_cursor.execute.await_count, 1)

        edge_cursor = AsyncMock()
        edge_cursor.fetchone = AsyncMock(return_value=(9, "active"))
        edge = asyncio.run(_set_dependency(
            edge_cursor,
            predecessor_id=2,
            successor_id=7,
            state="active",
            reason_code="analysis_before_followup",
            actor_user_id=1,
            event_type="test",
        ))
        self.assertFalse(edge["changed"])
        self.assertEqual(edge_cursor.execute.await_count, 1)

    def test_active_dependency_blocks_original_task_only_when_feature_enabled(self):
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(side_effect=[("1",), (1,)])
        self.assertTrue(asyncio.run(online_task_blocked(cursor, "全链条", "row-key")))
        self.assertEqual(cursor.execute.await_count, 2)

        disabled = AsyncMock()
        disabled.fetchone = AsyncMock(return_value=None)
        self.assertFalse(asyncio.run(online_task_blocked(disabled, "全链条", "row-key")))
        self.assertEqual(disabled.execute.await_count, 1)

    def test_owner_mapping_requires_exactly_one_linked_account(self):
        mapped = AsyncMock()
        mapped.fetchall = AsyncMock(return_value=[(18,)])
        self.assertEqual(asyncio.run(_resolve_online_owner(mapped, "网格员甲")), ("user", "18"))

        duplicate = AsyncMock()
        duplicate.fetchall = AsyncMock(return_value=[(18,), (19,)])
        self.assertEqual(asyncio.run(_resolve_online_owner(duplicate, "重名人员")), ("queue", "unmapped"))

        blank = AsyncMock()
        self.assertEqual(asyncio.run(_resolve_online_owner(blank, "")), ("queue", "unassigned"))
        self.assertEqual(blank.execute.await_count, 0)

    def test_same_task_has_owner_and_readonly_projections(self):
        person = task_graph_access(
            task_type="online_check", status="ready", view="person", history=False,
            owner_type="user", owner_ref="18", selected_owner_ref="18",
        )
        waiting_person = task_graph_access(
            task_type="online_check", status="blocked", view="person", history=False,
            owner_type="user", owner_ref="18", selected_owner_ref="18",
        )
        queue = task_graph_access(
            task_type="online_check", status="ready", view="queue", history=False,
            owner_type="user", owner_ref="18", selected_owner_ref="基础管控",
        )
        analysis_person = task_graph_access(
            task_type="analysis", status="ready", view="person", history=False,
            owner_type="queue", owner_ref="基础管控", selected_owner_ref="18",
        )
        analysis_queue = task_graph_access(
            task_type="analysis", status="ready", view="queue", history=False,
            owner_type="queue", owner_ref="基础管控", selected_owner_ref="基础管控",
        )
        history = task_graph_access(
            task_type="online_check", status="completed", view="person", history=True,
            owner_type="user", owner_ref="18", selected_owner_ref="18",
        )
        self.assertEqual(person, ("editable", "owned"))
        self.assertEqual(waiting_person, ("editable", "owned"))
        self.assertEqual(queue, ("readonly", "successor"))
        self.assertEqual(analysis_person, ("readonly", "predecessor"))
        self.assertEqual(analysis_queue, ("editable", "owned"))
        self.assertEqual(history, ("readonly", "successor"))

    def test_preview_counts_only_blocking_rows_and_missing_assignee_within_them(self):
        rows = [
            ("全链条", {"核查结果": "无法核实", "研判": "", "核查人": ""}),
            ("全链条", {"核查结果": "无法核实", "研判": "已填写", "核查人": "甲"}),
            ("全链条", {"核查结果": "已登记", "研判": "", "核查人": ""}),
            ("全链条", {"核查结果": "已登记", "研判": "历史研判", "核查人": "甲"}),
        ]
        result = asyncio.run(task_graph_preview(PreviewCursor(rows, [("甲", 1)])))
        self.assertEqual(result["projection_rows"], 4)
        self.assertEqual(result["unable_to_verify"], 2)
        self.assertEqual(result["analyzed"], 1)
        self.assertEqual(result["historical_analysis"], 1)
        self.assertEqual(result["eligible_chains"], 3)
        self.assertEqual(result["blank_inspector"], 1)
        self.assertEqual(result["unmatched_inspector"], 0)

    def test_schema_router_and_transaction_hooks_are_present(self):
        root = Path(__file__).resolve().parents[1]
        schema = (root / "services" / "domain_schema.py").read_text(encoding="utf-8")
        router = (root / "routers" / "task_graph.py").read_text(encoding="utf-8")
        query = (root / "routers" / "query.py").read_text(encoding="utf-8")
        source = (root / "services" / "online_source.py").read_text(encoding="utf-8")
        mobile = (root / "routers" / "mobile_tasks.py").read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS task_graph_nodes", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS task_graph_dependencies", schema)
        self.assertIn("CREATE TABLE IF NOT EXISTS task_graph_events", schema)
        self.assertNotIn("identity_number", schema[schema.index("task_graph_nodes"):schema.index("workflow_types")])
        self.assertIn('prefix="/api/task-graph"', router)
        self.assertIn('Depends(require_super_admin)', router)
        self.assertIn('@router.post("/search")', router)
        self.assertLess(query.index("await reconcile_online_task_graph("), query.index("await conn.commit()", query.index("async def queue_source_fields")))
        self.assertIn("await reconcile_projection_task_graph(cur, parser_type)", source)
        self.assertIn("dependency_blocked", mobile)
        self.assertNotIn("if dependency_blocked:\n                editable_fields = []", mobile)
        update_route = mobile[mobile.index("async def update_mobile_task("):mobile.index("@router.post(\"/{parser_type}/source-rows/{source_id}/resolve-sync-conflict\")")]
        self.assertNotIn("online_task_blocked", update_route)
        self.assertIn("网格员仍可继续核查并修改结果", mobile)


if __name__ == "__main__":
    unittest.main()
