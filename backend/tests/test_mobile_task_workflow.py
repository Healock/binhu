import os
import unittest
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from fastapi import HTTPException

from routers.mobile_tasks import (
    EMPTY_FILTER_VALUE,
    TaskSearch,
    _address_order,
    _multi_filter_condition,
    _priority_bucket,
    _scope_where,
    _source_in_community,
    _validate_assignment,
    is_flow_task_admin,
    is_flow_task_elevated,
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

    def test_unverifiable_always_needs_review_and_uses_analysis_stage(self):
        workflow = TASK_WORKFLOWS["寄递业"]
        self.assertTrue(workflow.needs_review({"核查结果": "无法核实"}))
        self.assertTrue(workflow.needs_review({
            "核查结果": "无法核实",
            "二次反馈": "再次联系未果",
        }))
        self.assertEqual(
            workflow.review_stage({"核查结果": "无法核实"}),
            "waiting_analysis",
        )
        self.assertEqual(
            workflow.review_stage({"核查结果": "无法核实", "研判": "已研判"}),
            "analyzed",
        )

    def test_unverifiable_is_checked_but_not_completed(self):
        self.assertEqual(
            task_state("全链条", {"核查结果": "无法核实"}),
            "checked",
        )
        self.assertEqual(
            task_state("疑似返苏", {"核查反馈": "无法核实"}),
            "checked",
        )

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

    def test_analyzed_card_summary_includes_analysis_result(self):
        summary = TASK_WORKFLOWS["寄递业"].summary({
            "核查结果": "无法核实",
            "研判": "无其他号码",
        })
        self.assertEqual(summary["analysis"], "无其他号码")

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

    def test_admin_permission_group_can_open_all_scope(self):
        user = {
            "role": "member",
            "permission_groups": [{"code": "admin"}],
        }
        self.assertTrue(is_flow_task_admin(user))
        where, params = _scope_where({
            "admin_mode": True,
            "community_values": None,
            "name": "管理员甲",
        }, "all")
        self.assertEqual(where, "1=1")
        self.assertEqual(params, [])

    def test_elevated_positions_can_open_managed_task_scope(self):
        for position in ("片长", "基础管控", "中队长", "所队领导"):
            with self.subTest(position=position):
                self.assertTrue(is_flow_task_elevated({
                    "role": "member",
                    "member": {"position": position},
                    "permission_groups": [],
                }))
        self.assertFalse(is_flow_task_elevated({
            "role": "member",
            "member": {"position": "组员"},
            "permission_groups": [],
        }))

    def test_flow_position_cannot_expand_to_all_scope(self):
        with self.assertRaises(HTTPException) as raised:
            _scope_where({
                "admin_mode": False,
                "community_values": ["长板", "长板村"],
                "name": "网格员甲",
            }, "all")
        self.assertEqual(raised.exception.status_code, 403)

    def test_task_search_accepts_multiselect_and_empty_values(self):
        request = TaskSearch(
            communities=["长板", EMPTY_FILTER_VALUE],
            inspectors=["网格员甲"],
            status="checked",
            review_stage="analyzed",
            priority="source_exception",
            sort="updated_asc",
        )
        self.assertEqual(request.status, "checked")
        self.assertEqual(request.communities, ["长板", EMPTY_FILTER_VALUE])
        self.assertEqual(request.sort, "updated_asc")

    def test_multiselect_where_contains_only_requested_values_and_empty_bucket(self):
        where, params = _multi_filter_condition(
            "community", ["长板", EMPTY_FILTER_VALUE, "长板"]
        )
        self.assertIn("projection.community IN (%s)", where)
        self.assertIn("TRIM(COALESCE(projection.community, ''))=''", where)
        self.assertEqual(params, ["长板"])

    def test_priority_buckets_follow_default_order_and_completed_is_last(self):
        analyzed = {"核查结果": "无法核实", "研判": "无其他号码"}
        waiting = {"核查结果": "无法核实"}
        self.assertEqual(
            _priority_bucket("全链条", analyzed, 2, True, True, "checked"),
            "analyzed",
        )
        self.assertEqual(
            _priority_bucket("全链条", {}, 1, False, True, "unchecked"),
            "pending_sync",
        )
        self.assertEqual(
            _priority_bucket("全链条", waiting, 1, False, False, "checked"),
            "waiting_analysis",
        )
        self.assertEqual(
            _priority_bucket("全链条", {"核查结果": "已登记"}, 1, False, True, "completed"),
            "completed",
        )

    def test_default_address_order_uses_business_address_fields_and_empty_last(self):
        sql = _address_order("全链条")
        self.assertIn("现住址", sql)
        self.assertIn("地址", sql)
        self.assertIn("REGEXP_REPLACE", sql)
        self.assertIn("CASE WHEN", sql)


class MobileTaskAssignmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_assignment_uses_global_row_permission_validation(self):
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        await _validate_assignment(
            cursor,
            {"admin_mode": True, "position": "管理员", "community": "全所"},
            {"核查人": "网格员甲"},
        )
        cursor.execute.assert_not_awaited()

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
