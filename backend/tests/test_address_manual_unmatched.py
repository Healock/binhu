"""Synthetic regression tests for the manual no-community annotation."""

import os
import unittest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import HTTPException
os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.address_match_feedback import (  # noqa: E402
    annotation_hmac,
    feedback_hmac,
    record_feedback_unmatched,
)
from routers.mobile_tasks import (  # noqa: E402
    AddressMatchManualUnmatched,
    mark_mobile_task_address_manual_unmatched,
    get_mobile_task_address_match_options,
)
from services.online_source import rebuild_projection_keys, source_row_hash  # noqa: E402


class _Cursor:
    def __init__(self, existing=None):
        self.existing = existing
        self.row = None
        self.statements = []

    async def execute(self, sql, params=()):
        self.statements.append((" ".join(sql.split()), tuple(params)))
        if "SELECT status, conflict_count" in sql:
            self.row = self.existing

    async def fetchone(self):
        return self.row


class _OptionsCursor:
    def __init__(self):
        self.row = None
        self.rows = []
        self.executions = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.executions.append((sql, tuple(params)))
        if sql.startswith("SELECT projection.community"):
            self.row = (
                "测试社区别名", 1, False, "虚构原始地址", 31, 5,
                "synthetic-row-hash", 7, "虚构小区", "manual_unmatched",
                "小区库缺失", "[]", 5, '{"现住址":"虚构现住址"}',
                "community_registry_missing",
            )
        elif sql.startswith("SELECT entry.id"):
            self.rows = [(7, "一号小区", 8, "测试社区", "虚构路 1 号")]
        elif sql.startswith("SELECT COUNT(*)"):
            self.row = (1,)
        elif sql.startswith("SELECT reason_code"):
            self.rows = [(
                "community_registry_missing",
                datetime(2026, 9, 8, tzinfo=timezone.utc),
                31,
                6,
            )]
        else:
            raise AssertionError(f"Unexpected options endpoint SQL: {sql}")

    async def fetchone(self):
        return self.row

    async def fetchall(self):
        return self.rows


class _OptionsConnection:
    def __init__(self):
        self.cursor_instance = _OptionsCursor()

    def cursor(self):
        return self.cursor_instance


class _CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, *args):
        return False


class _ManualCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executions = []
        self.rowcount = 1
        self.row = None

    async def execute(self, sql, params=()):
        compact = " ".join(sql.split())
        params = tuple(params)
        self.executions.append((compact, params))
        state = self.conn.state
        if compact.startswith("SELECT projection.community"):
            self.row = (
                state["community"], state["source_count"], state["conflict"],
                state["original_address"], state["source_id"],
                state["revision"], state["row_hash"], state["physical_row"],
                state["revision"], state["values_json"],
            ) if self.conn.visible and state["community"] == "测试社区" else None
        elif compact.startswith("INSERT INTO _online_task_address_matches"):
            state["match"] = {
                "status": "manual_unmatched",
                "reason_label": params[3],
                "reason_code": params[6],
                "annotation_hmac": params[7],
            }
        elif compact.startswith("UPDATE _online_source_rows SET revision="):
            state["revision"] = int(params[0])
        elif compact.startswith("UPDATE _local_source_records SET revision="):
            state["local_revision"] = int(params[0])
        else:
            raise AssertionError(f"Unexpected manual endpoint SQL: {compact}")

    async def fetchone(self):
        return self.row


class _ManualConnection:
    def __init__(self, *, community="测试社区"):
        self.state = {
            "community": community,
            "source_count": 1,
            "conflict": False,
            "original_address": "虚构原始地址",
            "current_address": "虚构现住址",
            "source_id": 31,
            "revision": 5,
            "local_revision": 5,
            "row_hash": "synthetic-row-hash",
            "physical_row": 101,
            "values_json": '{"原地址":"虚构原始地址","现住址":"虚构现住址","社区":"测试社区"}',
            "match": {"status": "suggested"},
            "summary_updates": [],
        }
        self.visible = True
        self.commits = 0
        self.rollbacks = 0
        self.before = None
        self.cursor_instance = _ManualCursor(self)

    def cursor(self):
        return _CursorContext(self.cursor_instance)

    async def begin(self):
        import copy
        self.before = copy.deepcopy(self.state)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        import copy
        self.rollbacks += 1
        self.state = copy.deepcopy(self.before)


def _manual_user(*permissions):
    return {
        "id": 7,
        "role": "member",
        "permissions": list(permissions or ("online.task.manage",)),
        "member": {"position": "组长", "name": "虚构组长"},
        "community_names": ["测试社区"],
    }


class _ReviewFenceCursor:
    """Small SQL fixture for two successive incremental projection rebuilds."""

    def __init__(self):
        values = {
            "社区": "测试社区",
            "地址": "新路123号",
            "现住址": "新路123号",
            "姓名": "虚构人员",
            "身份证号": "synthetic-id",
            "电话号码": "synthetic-phone",
        }
        self.values_json = __import__("json").dumps(values, ensure_ascii=False)
        self.old_annotation = annotation_hmac("旧地址", "旧现住址", "测试社区")
        self.status = "manual_unmatched"
        self.reason = "小区库缺失"
        self.executed = []
        self.address_rows = []
        self.projection_rows = []
        self.mode = ""

    async def execute(self, sql, params=()):
        compact = " ".join(sql.split())
        self.executed.append(compact)
        if compact.startswith("SELECT row_key, first_dispatch_at"):
            self.mode = "first_dispatch"
        elif compact.startswith("SELECT row_key, original_address"):
            self.mode = "stored_match"
        elif compact.startswith("SELECT entry.id, entry.name, entry.normalized_name"):
            self.mode = "entries"
        elif compact.startswith("SELECT source.id, source.row_key, source.values_json"):
            self.mode = "sources"
        elif compact.startswith("SELECT source_id, field_name, local_value"):
            self.mode = "local_changes"
        else:
            self.mode = "empty"

    async def fetchall(self):
        if self.mode == "first_dispatch":
            return [("synthetic-row", None)]
        if self.mode == "stored_match":
            return [(
                "synthetic-row", "旧地址", None, None, "", self.status, 0,
                "人工标注", self.reason, "[]", "rule-v3", None, None, None,
                "community_registry_missing", self.old_annotation, 7, None,
            )]
        if self.mode == "entries":
            return [(1, "新小区", "新小区", "新路123号", "[]", 10, "测试社区", 1)]
        if self.mode == "sources":
            return [(
                31, "synthetic-row", self.values_json, 2,
                source_row_hash(__import__("json").loads(self.values_json)),
            )]
        return []

    async def fetchone(self):
        return None

    async def executemany(self, sql, rows):
        compact = " ".join(sql.split())
        rows = list(rows)
        if "_online_task_address_matches" in compact:
            self.address_rows = rows
            self.status = str(rows[0][6])
            self.reason = str(rows[0][9])
        elif "_online_source_projection" in compact:
            self.projection_rows = rows


class ManualUnmatchedTests(unittest.IsolatedAsyncioTestCase):
    async def test_records_event_and_invalidates_existing_positive_memory(self):
        cursor = _Cursor(("active", 2))
        key = await record_feedback_unmatched(
            cursor,
            parser_type="全链条",
            row_key="synthetic-row",
            address="虚构路 1 号",
            community_name="测试社区",
            reason_code="community_registry_missing",
            recorded_by=7,
            source_id=31,
            source_revision=6,
        )

        self.assertEqual(
            key,
            annotation_hmac("虚构路 1 号", "", "测试社区"),
        )
        self.assertTrue(any("SET status='conflict'" in sql for sql, _ in cursor.statements))
        event = next(
            params for sql, params in cursor.statements
            if "INSERT INTO _online_task_address_unmatched_events" in sql
        )
        self.assertEqual(event[0:2], ("全链条", "synthetic-row"))
        self.assertEqual(event[3:], ("community_registry_missing", 7, 31, 6))

    async def test_empty_address_does_not_create_feedback_memory_but_records_event(self):
        cursor = _Cursor()
        key = await record_feedback_unmatched(
            cursor,
            parser_type="全链条",
            row_key="synthetic-row",
            address="",
            community_name="测试社区",
            reason_code="insufficient_address",
            recorded_by=7,
        )

        self.assertEqual(key, annotation_hmac("", "", "测试社区"))
        self.assertNotEqual(key, "")
        self.assertFalse(any("SELECT status, conflict_count" in sql for sql, _ in cursor.statements))
        self.assertTrue(any("INSERT INTO _online_task_address_unmatched_events" in sql for sql, _ in cursor.statements))

    def test_reason_contract_rejects_free_form_or_unknown_codes(self):
        valid = AddressMatchManualUnmatched(
            source_id=1,
            reason_code="outside_task_community",
            expected_revision=2,
            expected_row_hash="a" * 64,
        )
        self.assertEqual(valid.reason_code, "outside_task_community")
        with self.assertRaises(ValueError):
            AddressMatchManualUnmatched(
                source_id=1,
                reason_code="free_form",
                expected_revision=2,
                expected_row_hash="a" * 64,
            )

    def test_annotation_hmac_structurally_separates_colon_values(self):
        self.assertNotEqual(
            annotation_hmac("a", "b", "c"),
            annotation_hmac("a", "c", "b"),
        )

    async def test_options_are_readable_without_edit_permission_and_keep_manual_reason(self):
        conn = _OptionsConnection()
        user = {
            "id": 9,
            "permissions": ["online.raw.view"],
            "member": {"position": "组员", "name": "虚构组员"},
            "community_names": ["测试社区"],
        }
        context = {
            "admin_mode": False,
            "position": "组员",
            "community_values": ["测试社区"],
        }
        with patch("routers.mobile_tasks._flow_context", AsyncMock(return_value=context)), \
                patch("routers.mobile_tasks.formal_community", AsyncMock(return_value="测试社区")), \
                patch("routers.mobile_tasks.inspector_option_context", AsyncMock()) as inspector:
            result = await get_mobile_task_address_match_options(
                "全链条", "synthetic-row", keyword="", page=1, page_size=30,
                selected_id=None, user=user, conn=conn,
            )

        self.assertEqual(result["community"], "测试社区")
        self.assertEqual(result["items"][0]["name"], "一号小区")
        self.assertEqual(result["task"]["address_match_reason"], "小区库缺失")
        self.assertEqual(result["task"]["manual_unmatched_reason"], "community_registry_missing")
        self.assertEqual(result["manual_unmatched_reason"], "community_registry_missing")
        self.assertEqual(result["history"][0]["source_id"], 31)
        self.assertEqual(result["history"][0]["source_revision"], 6)
        self.assertFalse(result["capabilities"]["confirm"])
        self.assertFalse(result["capabilities"]["manual_unmatched"])
        inspector.assert_not_awaited()
        entry_queries = [
            params for sql, params in conn.cursor_instance.executions
            if sql.startswith("SELECT entry.id") and "WHERE entry.id=%s" not in sql
        ]
        self.assertEqual(entry_queries[0][0], "测试社区")


class ManualUnmatchedEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = _ManualConnection()
        self.request = MagicMock(headers={}, client=None)
        self.user = _manual_user()
        self.context = {
            "admin_mode": False,
            "position": "组长",
            "community_values": ["测试社区"],
        }
        self.feedback = AsyncMock()
        self.rebuild = AsyncMock()
        self.audit = AsyncMock()

        async def enqueue(cur, **kwargs):
            self.conn.state["summary_updates"].append(kwargs)

        self.enqueue = AsyncMock(side_effect=enqueue)
        self.matches = AsyncMock(return_value={
            "synthetic-row": {
                "status": "manual_unmatched",
                "reason": "小区库缺失",
            },
        })
        self.patches = [
            patch("routers.mobile_tasks._flow_context", AsyncMock(return_value=self.context)),
            patch("routers.mobile_tasks.inspector_option_context", AsyncMock(return_value={
                "community_aliases": {"测试社区": "测试社区"},
            })),
            patch("routers.mobile_tasks.record_feedback_unmatched", self.feedback),
            patch("routers.mobile_tasks.rebuild_projection_rows", self.rebuild),
            patch("routers.mobile_tasks._address_matches_by_rows", self.matches),
            patch("routers.mobile_tasks.record_admin_audit", self.audit),
            patch("services.business_time.get_business_date", AsyncMock(return_value=date(2026, 9, 8))),
            patch("services.online_summary_updates.enqueue_online_summary_update", self.enqueue),
        ]
        for item in self.patches:
            self.enterContext(item)

    async def call(self, *, conn=None, user=None, **overrides):
        fields = {
            "source_id": 31,
            "reason_code": "community_registry_missing",
            "expected_revision": 5,
            "expected_row_hash": "synthetic-row-hash",
        }
        fields.update(overrides)
        return await mark_mobile_task_address_manual_unmatched(
            "全链条", "synthetic-row", AddressMatchManualUnmatched(**fields),
            self.request, user or self.user, conn or self.conn,
        )

    async def test_success_increments_revision_and_uses_only_incremental_projection(self):
        result = await self.call()

        self.assertEqual(result["address_match"]["status"], "manual_unmatched")
        self.assertEqual(self.conn.state["revision"], 6)
        self.assertEqual(self.conn.state["local_revision"], 6)
        self.assertEqual(self.conn.commits, 1)
        self.assertEqual(self.conn.rollbacks, 0)
        self.rebuild.assert_awaited_once_with(
            self.conn.cursor_instance, "全链条", ["synthetic-row"],
        )
        self.assertEqual(self.conn.state["summary_updates"][0]["revision"], 6)
        self.feedback.assert_awaited_once()
        self.audit.assert_awaited_once()

    async def test_revision_or_hash_conflict_returns_409_without_writes(self):
        for overrides in (
            {"expected_revision": 4},
            {"expected_row_hash": "stale-row-hash"},
            {"source_id": 99},
        ):
            with self.subTest(overrides=overrides):
                conn = _ManualConnection()
                before = dict(conn.state)
                with self.assertRaises(HTTPException) as raised:
                    await self.call(conn=conn, **overrides)
                self.assertEqual(raised.exception.status_code, 409)
                self.assertEqual(conn.state, before)
                self.assertEqual(conn.commits, 0)
                self.assertEqual(conn.rollbacks, 1)
        self.feedback.assert_not_awaited()
        self.enqueue.assert_not_awaited()
        self.rebuild.assert_not_awaited()

    async def test_permission_and_cross_community_are_rejected(self):
        with self.assertRaises(HTTPException) as raised:
            await self.call(user=_manual_user("online.raw.view"))
        self.assertEqual(raised.exception.status_code, 403)

        cross_community = _ManualConnection(community="其他社区")
        with self.assertRaises(HTTPException) as raised:
            await self.call(conn=cross_community)
        self.assertEqual(raised.exception.status_code, 404)
        self.assertEqual(cross_community.commits, 0)
        self.assertEqual(cross_community.rollbacks, 1)

    async def test_enqueue_failure_rolls_back_negative_annotation_and_revision(self):
        before = __import__("copy").deepcopy(self.conn.state)
        self.enqueue.side_effect = RuntimeError("synthetic enqueue failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic enqueue failure"):
            await self.call()

        self.assertEqual(self.conn.state, before)
        self.assertEqual(self.conn.commits, 0)
        self.assertEqual(self.conn.rollbacks, 1)
        self.rebuild.assert_not_awaited()
        self.audit.assert_not_awaited()


class ManualReviewFenceProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_annotation_stays_review_required_on_later_rebuild(self):
        cursor = _ReviewFenceCursor()
        with patch("services.watch_matching.settings.REGISTRY_FEATURE_ENABLED", False):
            first = await rebuild_projection_keys(
                cursor, "全链条", ["synthetic-row"], reconcile_graph=False,
            )
            second = await rebuild_projection_keys(
                cursor, "全链条", ["synthetic-row"], reconcile_graph=False,
            )

        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 1)
        self.assertEqual(len(cursor.address_rows), 1)
        self.assertEqual(cursor.address_rows[0][6], "review_required")
        self.assertEqual(cursor.projection_rows[0][6], "review_required")
        self.assertEqual(cursor.projection_rows[0][9], "原始地址、现住址或任务社区已变化，需要重新核对")


if __name__ == "__main__":
    unittest.main()
