import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers import mobile_tasks
from services.parsers import get_parser


class _Cursor:
    def __init__(self, *, projection_rows=(), source_rows=(), cancel_rows=()):
        self.projection_rows = list(projection_rows)
        self.source_rows = list(source_rows)
        self.cancel_rows = list(cancel_rows)
        self.last_sql = ""
        self.executions = []
        self.rowcount = 1
        self.lastrowid = 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=()):
        self.last_sql = " ".join(str(sql).split())
        self.executions.append((self.last_sql, params))

    async def fetchall(self):
        if "SELECT projection.row_key" in self.last_sql:
            return self.projection_rows
        if "source.row_hash, source.values_json, source.sheet_id" in self.last_sql:
            return self.source_rows
        if "projection.task_state, projection.inspector" in self.last_sql:
            return self.cancel_rows
        return []


class _Connection:
    def __init__(self, cursor):
        self.cursor_obj = cursor
        self.begin = AsyncMock()
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    def cursor(self):
        return self.cursor_obj


def _projection_rows(*keys):
    return [
        (key, "长板", "", "unchecked", False, 1, "confirmed", 7, "小区A")
        for key in keys
    ]


def _source_rows(*keys, start_id=99, revision=3):
    return [
        (
            start_id + index,
            key,
            revision,
            123 + index,
            "row-hash",
            {"核查人": "", "姓名": "测试人员"},
            "local:全链条",
        )
        for index, key in enumerate(keys)
    ]


def _cancel_rows(*keys, start_id=99, revision=3):
    return [
        (
            start_id + index,
            key,
            revision,
            123 + index,
            {"核查人": "组员甲", "姓名": "测试人员"},
            "unchecked",
            "组员甲",
            1,
            False,
        )
        for index, key in enumerate(keys)
    ]


class MobileTaskAssignmentEventTests(unittest.IsolatedAsyncioTestCase):
    def _common_patches(self, *, apply_return=None, apply_side_effect=None):
        apply = AsyncMock(return_value=apply_return, side_effect=apply_side_effect)
        enqueue = AsyncMock()
        patches = [
            patch.object(mobile_tasks, "local_data_source_enabled", return_value=True),
            patch.object(mobile_tasks, "_require_task_edit_user", side_effect=lambda user: user),
            patch.object(
                mobile_tasks,
                "_flow_context",
                new=AsyncMock(return_value={"admin_mode": True, "community_values": None}),
            ),
            patch.object(mobile_tasks, "_can_assign_tasks", return_value=True),
            patch.object(
                mobile_tasks,
                "inspector_option_context",
                new=AsyncMock(
                    return_value={
                        "community_aliases": {"长板": "长板"},
                        "inspectors_by_community": {"长板": ["组员甲"]},
                    }
                ),
            ),
            patch.object(mobile_tasks, "apply_local_system_changes", new=apply),
            patch.object(mobile_tasks, "capture_first_assignment", new=AsyncMock()),
            patch.object(mobile_tasks, "record_admin_audit", new=AsyncMock()),
            patch.object(mobile_tasks, "request_audit_fields", return_value={}),
            patch.object(mobile_tasks, "enqueue_task_event", new=enqueue, create=True),
        ]
        return patches, apply, enqueue

    async def test_bulk_assignment_emits_assigned_events_with_local_task_identity(self):
        cursor = _Cursor(
            projection_rows=_projection_rows("row-1", "row-2"),
            source_rows=_source_rows("row-1", "row-2"),
        )
        conn = _Connection(cursor)
        patches, apply, enqueue = self._common_patches(
            apply_side_effect=[
                (1, 4, {"核查人": "组员甲"}, "row-1"),
                (2, 4, {"核查人": "组员甲"}, "row-2"),
            ]
        )
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])

        result = await mobile_tasks.bulk_assign_mobile_tasks(
            "全链条",
            mobile_tasks.BulkAssignmentRequest(
                row_keys=["row-1", "row-2"], inspector="组员甲", mode="single"
            ),
            MagicMock(),
            {"id": 7, "username": "管理员"},
            conn,
        )

        self.assertEqual(result["updated"], 2)
        self.assertEqual(enqueue.await_count, 2)
        calls = [item.kwargs for item in enqueue.await_args_list]
        self.assertEqual({item["event_type"] for item in calls}, {"online.task.assigned"})
        self.assertEqual({item["aggregate_type"] for item in calls}, {"online_task"})
        self.assertEqual({item["revision"] for item in calls}, {4})
        self.assertEqual({item["source_id"] for item in calls}, {99, 100})
        self.assertEqual(
            {item["task_id"] for item in calls},
            {f"{get_parser('全链条').table_name}:123", f"{get_parser('全链条').table_name}:124"},
        )
        self.assertEqual(len({item["operation_id"] for item in calls}), 1)
        self.assertEqual(calls[0]["changed_fields"], ["inspector"])
        self.assertEqual(calls[0]["audiences"], ["authenticated"])
        self.assertEqual(apply.await_count, 2)

    async def test_bulk_unassignment_emits_assigned_event_with_new_revision(self):
        cursor = _Cursor(cancel_rows=_cancel_rows("row-1"))
        conn = _Connection(cursor)
        patches, apply, enqueue = self._common_patches(
            apply_return=(3, 4, {"核查人": ""}, "row-1")
        )
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])

        result = await mobile_tasks.cancel_mobile_task_assignments(
            "全链条",
            mobile_tasks.CancelAssignmentsRequest(),
            MagicMock(),
            {"id": 7, "username": "管理员"},
            conn,
        )

        self.assertEqual(result["updated"], 1)
        enqueue.assert_awaited_once()
        event = enqueue.await_args.kwargs
        self.assertEqual(event["event_type"], "online.task.assigned")
        self.assertEqual(event["aggregate_id"], "全链条:row-1")
        self.assertEqual(event["revision"], 4)
        self.assertEqual(event["source_id"], 99)
        self.assertEqual(event["task_id"], f"{get_parser('全链条').table_name}:123")
        self.assertEqual(event["changed_fields"], ["inspector"])
        self.assertEqual(event["operation_id"].count("-"), 4)
        apply.assert_awaited_once()

    async def test_assignment_event_failure_rolls_back_whole_batch(self):
        cursor = _Cursor(projection_rows=_projection_rows("row-1"), source_rows=_source_rows("row-1"))
        conn = _Connection(cursor)
        patches, _, enqueue = self._common_patches(
            apply_return=(1, 4, {"核查人": "组员甲"}, "row-1")
        )
        enqueue.side_effect = RuntimeError("outbox unavailable")
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])

        with self.assertRaisesRegex(RuntimeError, "outbox unavailable"):
            await mobile_tasks.bulk_assign_mobile_tasks(
                "全链条",
                mobile_tasks.BulkAssignmentRequest(
                    row_keys=["row-1"], inspector="组员甲", mode="single"
                ),
                MagicMock(),
                {"id": 7, "username": "管理员"},
                conn,
            )

        conn.rollback.assert_awaited_once()
        conn.commit.assert_not_awaited()

    async def test_unassignment_event_contract_failure_rolls_back_transaction(self):
        cursor = _Cursor(cancel_rows=_cancel_rows("row-1"))
        conn = _Connection(cursor)
        patches, _, enqueue = self._common_patches(
            apply_return=(3, 4, {"核查人": ""}, "row-1")
        )
        enqueue.side_effect = ValueError("invalid task event contract")
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])

        with self.assertRaisesRegex(ValueError, "invalid task event contract"):
            await mobile_tasks.cancel_mobile_task_assignments(
                "全链条",
                mobile_tasks.CancelAssignmentsRequest(),
                MagicMock(),
                {"id": 7, "username": "管理员"},
                conn,
            )

        conn.rollback.assert_awaited_once()
        conn.commit.assert_not_awaited()

    async def test_stale_revision_does_not_emit_assignment_event(self):
        cursor = _Cursor(projection_rows=_projection_rows("row-1"), source_rows=_source_rows("row-1"))
        conn = _Connection(cursor)
        patches, _, enqueue = self._common_patches(
            apply_side_effect=ValueError("本地来源版本已变化")
        )
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(patches)])

        result = await mobile_tasks.bulk_assign_mobile_tasks(
            "全链条",
            mobile_tasks.BulkAssignmentRequest(
                row_keys=["row-1"], inspector="组员甲", mode="single"
            ),
            MagicMock(),
            {"id": 7, "username": "管理员"},
            conn,
        )

        self.assertEqual(result["failed"], 1)
        enqueue.assert_not_awaited()
        conn.commit.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
