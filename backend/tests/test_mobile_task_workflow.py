import os
import unittest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from fastapi import HTTPException

from routers.mobile_tasks import _source_in_community, require_flow_user
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


if __name__ == "__main__":
    unittest.main()
