import os
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from fastapi import HTTPException

from routers.mobile_tasks import (
    _source_in_community,
    _validate_assignment,
    require_flow_user,
)
from services.parsers import get_parser
from services.task_workflow import TASK_WORKFLOWS, task_state


class MobileTaskWorkflowTests(unittest.TestCase):
    def test_standard_workflow_uses_three_internal_states(self):
        self.assertEqual(task_state("全链条", {}), "unchecked")
        self.assertEqual(
            task_state("全链条", {"现住址": "长板一号"}),
            "checked",
        )
        self.assertEqual(
            task_state("全链条", {"核查结果": "已登记"}),
            "completed",
        )

    def test_suspect_return_uses_feedback_as_result(self):
        self.assertEqual(
            task_state("疑似返苏", {"核查结果": "已登记"}),
            "unchecked",
        )
        self.assertEqual(
            task_state("疑似返苏", {"核查反馈": "移交"}),
            "completed",
        )

    def test_unrevoked_only_accepts_defined_results(self):
        self.assertEqual(
            task_state("疑似未注销模型三", {"核查结果": "其他"}),
            "unchecked",
        )
        self.assertEqual(
            task_state("疑似未注销模型三", {"核查结果": "在吴"}),
            "completed",
        )

    def test_unverifiable_without_secondary_feedback_needs_review(self):
        workflow = TASK_WORKFLOWS["寄递业"]
        self.assertTrue(workflow.needs_review({"核查结果": "无法核实"}))
        self.assertFalse(workflow.needs_review({
            "核查结果": "无法核实",
            "二次反馈": "再次联系未果",
        }))

    def test_duplicate_or_conflict_needs_review(self):
        workflow = TASK_WORKFLOWS["全链条"]
        self.assertTrue(workflow.needs_review({}, source_count=2))
        self.assertTrue(workflow.needs_review({}, conflict=True))

    def test_card_summary_includes_identity_phone_and_fullchain_source(self):
        summary = TASK_WORKFLOWS["全链条"].summary({
            "姓名": "测试对象",
            "身份证号": "320000000000000000",
            "电话号码": "18800000000",
            "来源": "模型来源甲",
        })
        self.assertEqual(summary["identity_number"], "320000000000000000")
        self.assertEqual(summary["phone"], "18800000000")
        self.assertEqual(summary["source"], "模型来源甲")

    def test_delivery_card_uses_reference_identity_as_fallback(self):
        summary = TASK_WORKFLOWS["寄递业"].summary({
            "参考身份证号码": "320000000000000001",
        })
        self.assertEqual(summary["identity_number"], "320000000000000001")
        self.assertEqual(summary["source"], "")

    def test_each_mobile_workflow_defines_result_choices(self):
        for workflow in TASK_WORKFLOWS.values():
            with self.subTest(parser_type=workflow.parser_type):
                self.assertGreater(len(workflow.result_options), 0)
                self.assertEqual(len(workflow.result_options), len(set(workflow.result_options)))

    def test_only_flow_positions_with_one_community_are_allowed(self):
        user = {
            "member": {"name": "网格员甲", "position": "组员"},
            "community_names": ["长板"],
        }
        self.assertEqual(require_flow_user(user), ("网格员甲", "长板"))
        with self.assertRaises(HTTPException):
            require_flow_user({
                "member": {"name": "内勤甲", "position": "基础管控"},
                "community_names": [],
            })
        with self.assertRaises(HTTPException):
            require_flow_user({
                "member": {"name": "网格员乙", "position": "组长"},
                "community_names": [],
            })

    def test_duplicate_sources_never_expose_another_community(self):
        parser = get_parser("全链条")

        self.assertTrue(_source_in_community(
            parser, {"社区": "长板村"}, ["长板", "长板村"]
        ))
        self.assertFalse(_source_in_community(
            parser, {"社区": "冬梅"}, ["长板", "长板村"]
        ))


class MobileTaskAssignmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_leader_can_reassign_mobile_task(self):
        cursor = MagicMock()
        with self.assertRaises(HTTPException) as raised:
            await _validate_assignment(
                cursor,
                {"position": "组员", "community": "长板"},
                {"核查人": "组员甲"},
            )
        self.assertEqual(raised.exception.status_code, 403)

    async def test_leader_can_only_assign_active_member_in_same_community(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(7,))
        await _validate_assignment(
            cursor,
            {"position": "组长", "community": "长板"},
            {"核查人": "组员甲"},
        )
        self.assertEqual(
            cursor.execute.await_args.args[1],
            ("长板", "组员甲"),
        )

        cursor.fetchone = AsyncMock(return_value=None)
        with self.assertRaises(HTTPException) as raised:
            await _validate_assignment(
                cursor,
                {"position": "组长", "community": "长板"},
                {"核查人": "其他社区人员"},
            )
        self.assertEqual(raised.exception.status_code, 400)

    async def test_non_assignment_changes_skip_member_lookup(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        await _validate_assignment(
            cursor,
            {"position": "组员", "community": "长板"},
            {"现住址": "长板一号"},
        )
        cursor.execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
