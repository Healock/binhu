import os
import unittest
from datetime import date
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services import unverifiable_review as review
from routers.mobile_tasks import (
    UnverifiableDecision,
    decide_mobile_task_unverifiable_review,
)


class _DecisionCursor:
    def __init__(self, *, flow_row=None, rowcount=1):
        self.flow_row = flow_row
        self.rowcount = rowcount
        self.executions = []
        self.lastrowid = 1

    async def execute(self, sql, params=()):
        self.executions.append((" ".join(str(sql).split()), tuple(params)))

    async def fetchone(self):
        return self.flow_row

    async def fetchall(self):
        return []


class _AuditCursor(_DecisionCursor):
    async def fetchall(self):
        return [
            ("全链条", "a" * 32, '{"核查结果":"无法核实","研判":"旧意见"}', 1, 0),
            ("交通涉警", "b" * 32, '{"核查结果":"已登记"}', 1, 0),
        ]


class _ReconcileCursor(_DecisionCursor):
    def __init__(self, flow_row):
        super().__init__()
        self.flow_row = flow_row

    async def fetchall(self):
        sql = self.executions[-1][0] if self.executions else ""
        return [self.flow_row] if "flow.review_due_date" in sql else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _ReconcileConnection:
    def __init__(self, cursor):
        self.cursor_value = cursor
        self.events = []

    def cursor(self):
        return self.cursor_value

    async def begin(self):
        self.events.append("begin")

    async def commit(self):
        self.events.append("commit")

    async def rollback(self):
        self.events.append("rollback")


class _AcquireContext:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *_args):
        return False


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _AcquireContext(self.conn)


class UnverifiableReviewTests(unittest.IsolatedAsyncioTestCase):
    def test_supported_businesses_exclude_model_three(self):
        for parser_type in (
            "全链条", "出租房屋核查", "寄递业", "疑似返苏", "苏州涉警", "交通涉警",
        ):
            self.assertTrue(review.supports_unverifiable_review(parser_type))
        self.assertFalse(review.supports_unverifiable_review("疑似未注销模型三"))

    def test_review_due_dates_are_fixed_by_stage(self):
        self.assertEqual(
            review.review_due_date(date(2026, 8, 27), review.INITIAL_PENDING),
            date(2026, 8, 29),
        )
        self.assertEqual(
            review.review_due_date(date(2026, 8, 27), review.DEEP_PENDING),
            date(2026, 8, 28),
        )
        with self.assertRaises(ValueError):
            review.review_due_date(date(2026, 8, 27), review.FINAL_UNVERIFIABLE)

    async def test_archive_without_review_flow_is_a_normal_terminal_case(self):
        cursor = AsyncMock()
        with patch.object(review, "_latest_flow", new=AsyncMock(return_value=None)), \
             patch.object(review, "_event", new=AsyncMock()) as event:
            await review.mark_flow_archived(cursor, "全链条", "a" * 32, 12)
        cursor.execute.assert_not_awaited()
        event.assert_not_awaited()

    async def test_final_and_resolved_review_flows_can_follow_source_archive(self):
        for state in (review.FINAL_UNVERIFIABLE, review.RESOLVED):
            with self.subTest(state=state):
                flow_row = (
                    5, "全链条", "a" * 32, 1, 7, 3, "b" * 64,
                    state, 4, None, "", "", 0, "", None,
                    None, None, None, None,
                )
                cursor = AsyncMock()
                with patch.object(review, "_latest_flow", new=AsyncMock(return_value=flow_row)), \
                     patch.object(review, "_event", new=AsyncMock()) as event:
                    await review.mark_flow_archived(cursor, "全链条", "a" * 32, 12)
                cursor.execute.assert_awaited_once()
                kwargs = event.await_args.kwargs
                self.assertEqual(kwargs["stage"], state)
                self.assertEqual(
                    kwargs["safe_reason_code"],
                    "resolved_source_archived" if state == review.RESOLVED else "",
                )

    async def test_archived_review_flow_is_idempotent(self):
        flow_row = (
            5, "全链条", "a" * 32, 1, 7, 3, "b" * 64,
            review.ARCHIVED, 4, None, "", "", 0, "", None,
            None, None, None, None,
        )
        cursor = AsyncMock()
        with patch.object(review, "_latest_flow", new=AsyncMock(return_value=flow_row)), \
             patch.object(review, "_event", new=AsyncMock()) as event:
            await review.mark_flow_archived(cursor, "全链条", "a" * 32, 12)
        cursor.execute.assert_not_awaited()
        event.assert_not_awaited()

    async def test_active_review_flow_blocks_archive_with_safe_code(self):
        for state in (
            review.INITIAL_PENDING,
            review.INITIAL_EXTENSION,
            review.DEEP_PENDING,
            review.DEEP_EXTENSION,
            review.SOURCE_EXCEPTION,
        ):
            with self.subTest(state=state):
                flow_row = (
                    5, "全链条", "a" * 32, 1, 7, 3, "b" * 64,
                    state, 4, None, "", "", 0, "", None,
                    None, None, None, None,
                )
                with patch.object(review, "_latest_flow", new=AsyncMock(return_value=flow_row)):
                    with self.assertRaisesRegex(RuntimeError, "review_flow_state_conflict"):
                        await review.mark_flow_archived(
                            AsyncMock(), "全链条", "a" * 32, 12
                        )

    async def test_prepare_decision_success_starts_initial_two_day_extension(self):
        flow = {
            "id": 7, "parser_type": "全链条", "row_key": "a" * 32,
            "state": review.INITIAL_PENDING, "flow_version": 3,
            "source_id": 9, "source_revision": 4, "source_row_hash": "h",
            "safe_reason_code": "",
        }
        source = {"id": 9, "row_key": "a" * 32, "revision": 4, "row_hash": "h"}
        values = {"核查结果": "无法核实", "截止日期": "2026-08-27"}
        with patch.object(review, "ensure_flow_for_values", new=AsyncMock(return_value=flow)), \
             patch.object(review, "get_business_date", new=AsyncMock(return_value=date(2026, 8, 27))):
            prepared = await review.prepare_decision(
                _DecisionCursor(), parser_type="全链条", source=source,
                current_values=values, stage=review.INITIAL_PENDING,
                outcome="success", opinion="已联系核查对象",
                expected_flow_version=3, expected_row_hash="h",
            )
        self.assertEqual(prepared["next_state"], review.INITIAL_EXTENSION)
        self.assertEqual(prepared["due_date"], date(2026, 8, 29))

    async def test_prepare_decision_failures_advance_to_next_stage_or_final(self):
        source = {"id": 9, "row_key": "a" * 32, "revision": 4, "row_hash": "h"}
        values = {"核查结果": "无法核实", "截止日期": "2026-08-27"}
        for stage, expected in (
            (review.INITIAL_PENDING, review.DEEP_PENDING),
            (review.DEEP_PENDING, review.FINAL_UNVERIFIABLE),
        ):
            flow = {
                "id": 7, "parser_type": "全链条", "row_key": "a" * 32,
                "state": stage, "flow_version": 3,
                "source_id": 9, "source_revision": 4,
                "source_row_hash": "h", "safe_reason_code": "",
            }
            with patch.object(review, "ensure_flow_for_values", new=AsyncMock(return_value=flow)):
                prepared = await review.prepare_decision(
                    _DecisionCursor(), parser_type="全链条", source=source,
                    current_values=values, stage=stage, outcome="failure",
                    opinion="暂未找到有效线索", expected_flow_version=3,
                    expected_row_hash="h",
                )
            self.assertEqual(prepared["next_state"], expected)
            self.assertIsNone(prepared["due_date"])

    async def test_prepare_decision_requires_opinion_and_current_flow_version(self):
        flow = {
            "id": 7, "parser_type": "全链条", "row_key": "a" * 32,
            "state": review.INITIAL_PENDING, "flow_version": 3,
            "source_id": 9, "source_revision": 4, "source_row_hash": "h",
            "safe_reason_code": "",
        }
        source = {"id": 9, "row_key": "a" * 32, "revision": 4, "row_hash": "h"}
        values = {"核查结果": "无法核实"}
        with patch.object(review, "ensure_flow_for_values", new=AsyncMock(return_value=flow)):
            with self.assertRaisesRegex(ValueError, "研判意见"):
                await review.prepare_decision(
                    _DecisionCursor(), parser_type="全链条", source=source,
                    current_values=values, stage=review.INITIAL_PENDING,
                    outcome="success", opinion=" ", expected_flow_version=3,
                    expected_row_hash="h",
                )
            with self.assertRaisesRegex(ValueError, "流程已经被其他人更新"):
                await review.prepare_decision(
                    _DecisionCursor(), parser_type="全链条", source=source,
                    current_values=values, stage=review.INITIAL_PENDING,
                    outcome="success", opinion="有效意见", expected_flow_version=2,
                    expected_row_hash="h",
                )

    async def test_prepare_decision_rejects_changed_source_snapshot(self):
        flow = {
            "id": 7, "parser_type": "全链条", "row_key": "a" * 32,
            "state": review.INITIAL_PENDING, "flow_version": 3,
            "source_id": 9, "source_revision": 3,
            "source_row_hash": "old-h", "safe_reason_code": "",
        }
        source = {
            "id": 9, "row_key": "a" * 32,
            "revision": 4, "row_hash": "new-h",
        }
        with patch.object(
            review, "ensure_flow_for_values", new=AsyncMock(return_value=flow)
        ):
            with self.assertRaisesRegex(ValueError, "来源版本已经变化"):
                await review.prepare_decision(
                    _DecisionCursor(), parser_type="全链条", source=source,
                    current_values={"核查结果": "无法核实"},
                    stage=review.INITIAL_PENDING, outcome="success",
                    opinion="继续核查", expected_flow_version=3,
                    expected_row_hash="new-h",
                )

    async def test_source_context_reconciliation_persists_exception(self):
        class SourceContextCursor(_DecisionCursor):
            async def fetchall(self):
                sql = self.executions[-1][0] if self.executions else ""
                if "projection.source_count,projection.conflict" not in sql:
                    return []
                return [(
                    7, review.INITIAL_PENDING, 9, 3, "old-h", "a" * 32,
                    9, 4, "new-h", "a" * 32, 1, 0,
                )]

        cursor = SourceContextCursor()
        paused = await review.reconcile_unverifiable_source_contexts(
            cursor, "全链条"
        )

        self.assertEqual(paused, 1)
        self.assertTrue(any(
            "safe_reason_code='source_context_changed'" in sql
            for sql, _ in cursor.executions
        ))
        self.assertTrue(any(
            "INSERT INTO _unverifiable_review_events" in sql
            for sql, _ in cursor.executions
        ))

    async def test_apply_decision_uses_optimistic_flow_version(self):
        flow_row = (
            7, "全链条", "a" * 32, 1, 9, 4, "h", review.DEEP_PENDING,
            4, "2026-08-29", "2026-08-27", "2026-08-27", 0, "", None,
            None, None, None, None,
        )
        cursor = _DecisionCursor(flow_row=flow_row)
        prepared = {
            "flow": {"id": 7, "parser_type": "全链条", "row_key": "a" * 32, "flow_version": 3},
            "next_state": review.DEEP_PENDING, "due_date": None,
            "previous_deadline": "2026-08-29", "stage": review.INITIAL_PENDING,
            "outcome": "failure", "opinion": "转深度研判",
        }
        result = await review.apply_decision(
            cursor, prepared=prepared,
            source={"row_hash": "new-h"}, revision=5, actor_user_id=12,
        )
        self.assertEqual(result["state"], review.DEEP_PENDING)
        self.assertTrue(any("WHERE id=%s AND flow_version=%s" in sql for sql, _ in cursor.executions))

    async def test_secondary_feedback_is_rejected_before_extension_period(self):
        flow = {
            "id": 7, "parser_type": "全链条", "row_key": "a" * 32,
            "state": review.INITIAL_PENDING, "flow_version": 1,
        }
        source = {"id": 9, "row_key": "a" * 32, "row_hash": "h"}
        with patch.object(
            review, "ensure_flow_for_values", new=AsyncMock(return_value=flow)
        ):
            with self.assertRaisesRegex(ValueError, "延时复核期间"):
                await review.record_task_save(
                    _DecisionCursor(),
                    parser_type="全链条",
                    source=source,
                    before={"核查结果": "无法核实", "二次反馈": ""},
                    after={"核查结果": "无法核实", "二次反馈": "提前反馈"},
                    changes={"二次反馈": "提前反馈"},
                    row_key_after="a" * 32,
                    revision=5,
                    actor_user_id=12,
                )

    async def test_successful_decision_clears_previous_stage_feedback(self):
        prepared = {
            "flow": {"id": 7},
            "next_state": review.INITIAL_EXTENSION,
            "due_date": date(2026, 8, 29),
            "summary": "初步研判成功：继续核查；复核截止 2026-08-29",
        }
        captured = {}

        async def queue_side_effect(**kwargs):
            captured["prepared_changes"] = await kwargs["transaction_prepare"](
                cur=_DecisionCursor(),
                source={"id": 9, "revision": 4, "row_key": "a" * 32, "row_hash": "h"},
                current_values={
                    "核查结果": "无法核实",
                    "截止日期": "2026-08-27",
                    "二次反馈": "上一阶段反馈",
                },
                changes={"研判": "继续核查"},
            )
            captured["record_unverifiable_save"] = kwargs["record_unverifiable_save"]
            return {"message": "queued"}

        user = {
            "id": 12,
            "role": "admin",
            "permissions": ["online.task.manage", "online.raw.edit"],
        }
        with patch(
            "routers.mobile_tasks._require_unverifiable_reviewer",
            return_value=user,
        ), patch(
            "routers.mobile_tasks.prepare_decision",
            new=AsyncMock(return_value=prepared),
        ), patch(
            "routers.mobile_tasks.queue_source_fields",
            new=AsyncMock(side_effect=queue_side_effect),
        ):
            result = await decide_mobile_task_unverifiable_review(
                "全链条",
                9,
                UnverifiableDecision(
                    stage=review.INITIAL_PENDING,
                    outcome="success",
                    opinion="继续核查",
                    flow_version=1,
                    expected_revision=4,
                    expected_row_hash="h",
                ),
                request=object(),
                user=user,
                conn=object(),
            )

        self.assertEqual(result["message"], "queued")
        self.assertEqual(captured["prepared_changes"]["二次反馈"], "")
        self.assertEqual(captured["prepared_changes"]["截止日期"], "2026-08-29")
        self.assertFalse(captured["record_unverifiable_save"])

    async def test_read_only_audit_only_returns_unverifiable_rows(self):
        cursor = _AuditCursor()
        missing = await review.audit_missing_unverifiable_flows(cursor)
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["parser_type"], "全链条")
        self.assertEqual(missing[0]["values"]["研判"], "旧意见")
        self.assertIn("NOT EXISTS", cursor.executions[0][0])

    def test_export_fields_keep_both_review_and_feedback_rounds(self):
        fields = review.review_export_fields([
            {"stage": review.INITIAL_PENDING, "action": "review_decision", "outcome": "success", "text": "初判意见", "actor_name": "甲", "created_at": "2026-08-27T09:00:00Z", "automatic": False},
            {"stage": review.INITIAL_EXTENSION, "action": "feedback_recorded", "outcome": "", "text": "第一次反馈", "actor_name": "乙", "created_at": "2026-08-29T09:00:00Z", "automatic": False},
            {"stage": review.INITIAL_EXTENSION, "action": "overdue_auto_transition", "outcome": review.DEEP_PENDING, "text": "", "actor_name": "", "created_at": "2026-08-30T00:01:00Z", "automatic": True},
            {"stage": review.DEEP_PENDING, "action": "review_decision", "outcome": "failure", "text": "深判意见", "actor_name": "丙", "created_at": "2026-08-30T10:00:00Z", "automatic": False},
        ])
        self.assertEqual(fields["initial_review"], "研判成功：初判意见")
        self.assertEqual(fields["first_feedback"], "第一次反馈")
        self.assertEqual(fields["deep_review"], "研判失败：深判意见")
        self.assertIn("逾期自动流转", fields["automatic_events"])

    async def test_mark_archived_is_idempotent_after_success(self):
        archived_flow = (
            7, "全链条", "a" * 32, 1, 9, 4, "h", review.ARCHIVED,
            4, None, "", "", 0, "", None, None, None, None, None,
        )
        cursor = _DecisionCursor()
        with patch.object(review, "_latest_flow", new=AsyncMock(return_value=archived_flow)), \
             patch.object(review, "_event", new=AsyncMock()) as event:
            await review.mark_flow_archived(cursor, "全链条", "a" * 32, 12)
        self.assertEqual(cursor.executions, [])
        event.assert_not_awaited()

    async def test_overdue_transition_records_real_feedback_state(self):
        for feedback_submitted, expected_reason in ((0, "no_feedback"), (1, "feedback_recorded")):
            with self.subTest(feedback_submitted=feedback_submitted):
                row_key = "a" * 32
                source_hash = "b" * 64
                flow_row = (
                    7, "全链条", row_key, review.INITIAL_EXTENSION,
                    3, 9, 4, source_hash, date(2026, 8, 29), feedback_submitted,
                    9, row_key, 4, source_hash, 20, 2, "sheet-1",
                    '{"核查结果":"无法核实"}', 1, 0,
                )
                cursor = _ReconcileCursor(flow_row)
                conn = _ReconcileConnection(cursor)
                launch = patch("services.online_local_writeback.launch_local_change_processing")
                with patch("database.db_manager.get_pool", return_value=_Pool(conn)), \
                     patch.object(review, "get_business_date", new=AsyncMock(return_value=date(2026, 8, 30))), \
                     patch.object(review, "_system_enqueue", new=AsyncMock(return_value=5)) as enqueue, \
                     patch("services.online_local_writeback.load_local_changes", new=AsyncMock(return_value={})), \
                     patch("services.online_local_writeback.overlay_local_values", return_value={"核查结果": "无法核实"}), \
                     launch:
                    advanced = await review.reconcile_unverifiable_once()

                self.assertEqual(advanced, 1)
                event_params = [
                    params for sql, params in cursor.executions
                    if "INSERT INTO _unverifiable_review_events" in sql
                ]
                self.assertEqual(event_params[-1][-1], expected_reason)
                self.assertTrue(any(
                    "flow.feedback_submitted" in sql
                    for sql, _ in cursor.executions
                ))
                self.assertEqual(
                    enqueue.await_args.kwargs["changes"],
                    {"研判": "初步延时已到期，自动进入深度研判", "二次反馈": ""},
                )


if __name__ == "__main__":
    unittest.main()
