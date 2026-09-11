"""Exercise the address-confirm endpoint against transactional synthetic state."""

import copy
import os
import unittest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from fastapi import HTTPException

from routers.mobile_tasks import AddressMatchConfirm, confirm_mobile_task_address_match


class ConfirmationCursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None
        self.executions = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.executions.append((sql, params))
        state = self.conn.state
        if sql.startswith("SELECT projection.community"):
            self.row = (
                "测试社区", state["source_count"], state["conflict"],
                state["original_address"], 11, state["source_revision"],
                state["row_hash"], 101,
            ) if self.conn.visible else None
        elif sql.startswith("SELECT entry.id"):
            self.row = (params[0], "虚构小区", 9, "测试社区")
        elif sql.startswith("INSERT INTO _online_task_address_matches"):
            state["match"] = {"status": "confirmed", "confirmed_entry_id": params[4]}
        elif sql.startswith("UPDATE _online_source_rows SET revision="):
            assert params[1] == 11, "must update the locked source identity"
            state["source_revision"] = params[0]
        elif sql.startswith("UPDATE _local_source_records SET revision="):
            assert params[1:] == ("全链条", 101), "must update the same local task"
            state["local_revision"] = params[0]
        else:
            raise AssertionError(f"Unexpected endpoint SQL: {sql}")

    async def fetchone(self):
        return self.row


class ConfirmationConnection:
    """Transactions restore all writes; this does not simulate MySQL locking."""

    def __init__(self):
        self.state = {
            "source_revision": 5,
            "local_revision": 5,
            "source_count": 1,
            "conflict": False,
            "row_hash": "synthetic-content-hash",
            "original_address": "虚构测试地址",
            "match": {"status": "suggested", "confirmed_entry_id": None},
            "feedback": [],
            "summary_updates": [],
        }
        self.visible = True
        self.commits = 0
        self.rollbacks = 0
        self._cursor = ConfirmationCursor(self)

    def cursor(self):
        return self._cursor

    async def begin(self):
        self.before = copy.deepcopy(self.state)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1
        self.state = copy.deepcopy(self.before)


class AddressConfirmationRevisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = ConfirmationConnection()
        self.request = MagicMock(headers={}, client=None)
        self.user = {"id": 7, "permissions": ["online.task.manage"]}
        self.context = {"admin_mode": False, "position": "组长",
                        "community_values": ["测试社区"]}
        self.enqueue_failure = False

        async def feedback(cur, **kwargs):
            self.assertIs(cur, self.conn._cursor)
            self.conn.state["feedback"].append(kwargs["confirmed_entry_id"])

        async def enqueue(cur, **kwargs):
            self.assertIs(cur, self.conn._cursor)
            self.assertEqual(self.conn.commits, 0)
            self.conn.state["summary_updates"].append(kwargs)
            if self.enqueue_failure:
                raise RuntimeError("synthetic summary enqueue failure")

        async def matches(cur, parser_type, row_keys):
            return {row_keys[0]: copy.deepcopy(self.conn.state["match"])}

        self.feedback = AsyncMock(side_effect=feedback)
        self.enqueue = AsyncMock(side_effect=enqueue)
        self.rebuild = AsyncMock()
        self.audit = AsyncMock()
        for target, mock in {
            "routers.mobile_tasks._flow_context": AsyncMock(return_value=self.context),
            "routers.mobile_tasks.inspector_option_context": AsyncMock(return_value={
                "community_aliases": {"测试社区": "测试社区"},
            }),
            "routers.mobile_tasks.record_feedback_confirmation": self.feedback,
            "routers.mobile_tasks.rebuild_projection_rows": self.rebuild,
            "routers.mobile_tasks._address_matches_by_rows": AsyncMock(side_effect=matches),
            "routers.mobile_tasks.record_admin_audit": self.audit,
            "services.business_time.get_business_date": AsyncMock(return_value=date(2026, 9, 7)),
            "services.online_summary_updates.enqueue_online_summary_update": self.enqueue,
        }.items():
            self.enterContext(patch(target, mock))

    async def confirm(self, **overrides):
        fields = {"source_id": 11, "small_community_id": 22,
                  "expected_revision": 5, "expected_row_hash": "synthetic-content-hash"}
        fields.update(overrides)
        return await confirm_mobile_task_address_match(
            "全链条", "synthetic-row", AddressMatchConfirm(**fields),
            self.request, self.user, self.conn,
        )

    async def test_first_confirmation_advances_fence_and_stale_confirmation_cannot_overwrite(self):
        result = await self.confirm()

        self.assertEqual(result["address_match"]["confirmed_entry_id"], 22)
        self.assertEqual(self.conn.state["source_revision"], 6)
        self.assertEqual(self.conn.state["local_revision"], 6)
        self.assertEqual(self.conn.state["row_hash"], "synthetic-content-hash")
        self.assertEqual(self.conn.state["original_address"], "虚构测试地址")
        self.assertEqual(self.conn.commits, 1)
        self.assertEqual(self.conn.state["summary_updates"], [{
            "task_id": 101, "parser_type": "全链条", "row_key": "synthetic-row",
            "revision": 6, "business_date": date(2026, 9, 7),
            "operation_id": "address-confirm-11-6",
        }])
        confirm_sql = next(
            sql for sql, _ in self.conn._cursor.executions
            if sql.startswith("INSERT INTO _online_task_address_matches")
        )
        self.assertIn("manual_unmatched_reason=NULL", confirm_sql)
        self.assertIn("manual_unmatched_address_hmac=NULL", confirm_sql)
        self.rebuild.assert_awaited_once_with(self.conn._cursor, "全链条", ["synthetic-row"])
        self.assertIs(self.audit.await_args.kwargs["conn"], self.conn)
        committed = copy.deepcopy(self.conn.state)

        with self.assertRaises(HTTPException) as raised:
            await self.confirm(small_community_id=23)

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(self.conn.state, committed)
        self.assertEqual(self.conn.commits, 1)
        self.assertEqual(self.conn.rollbacks, 1)
        self.feedback.assert_awaited_once()
        self.enqueue.assert_awaited_once()
        self.audit.assert_awaited_once()

    async def test_enqueue_failure_rolls_back_confirmation_feedback_and_both_revisions(self):
        before = copy.deepcopy(self.conn.state)
        self.enqueue_failure = True

        with self.assertRaisesRegex(RuntimeError, "synthetic summary enqueue failure"):
            await self.confirm()

        self.feedback.assert_awaited_once()
        self.enqueue.assert_awaited_once()
        self.assertEqual(self.conn.state, before)
        self.assertEqual(self.conn.commits, 0)
        self.assertEqual(self.conn.rollbacks, 1)
        self.rebuild.assert_not_awaited()
        self.audit.assert_not_awaited()

    async def test_changed_source_or_content_is_rejected_before_confirmation_writes(self):
        before = copy.deepcopy(self.conn.state)
        for overrides in ({"source_id": 12}, {"expected_row_hash": "stale-hash"},
                          {"expected_revision": 4}):
            with self.subTest(overrides=overrides), self.assertRaises(HTTPException) as raised:
                await self.confirm(**overrides)
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(self.conn.state, before)
        self.feedback.assert_not_awaited()
        self.enqueue.assert_not_awaited()
        self.assertEqual(self.conn.commits, 0)

    async def test_duplicate_or_conflicting_source_is_rejected_before_confirmation_writes(self):
        for count, conflict in ((2, False), (1, True)):
            self.conn.state.update(source_count=count, conflict=conflict)
            before = copy.deepcopy(self.conn.state)
            with self.subTest(count=count, conflict=conflict), self.assertRaises(HTTPException) as raised:
                await self.confirm()
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(self.conn.state, before)
        self.feedback.assert_not_awaited()
        self.enqueue.assert_not_awaited()

    async def test_missing_scoped_task_preserves_state_and_queries_only_active_local_sources(self):
        self.conn.visible = False
        before = copy.deepcopy(self.conn.state)
        with self.assertRaises(HTTPException) as raised:
            await self.confirm()
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(self.conn.state, before)
        sql, params = self.conn._cursor.executions[0]
        for clause in ("source.archived_at IS NULL",
                       "source.source_kind IN ('local_table','local_dispatch','one_time_continuation_import')",
                       "projection.community IN (%s)", "FOR UPDATE"):
            self.assertIn(clause, sql)
        self.assertEqual(params, ("全链条", "synthetic-row", "测试社区"))
        self.feedback.assert_not_awaited()
        self.enqueue.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
