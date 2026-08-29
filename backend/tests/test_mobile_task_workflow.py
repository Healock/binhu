import asyncio
import inspect
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from fastapi import HTTPException

from routers.mobile_tasks import (
    AnalysisTaskSearch,
    MAX_BULK_ASSIGNMENT_TASKS,
    MAX_BULK_ASSIGNMENT_CHUNK,
    BulkAssignmentRequest,
    EMPTY_FILTER_VALUE,
    InlineEditorRequest,
    TaskSearch,
    TaskBatchUpdate,
    _address_order,
    _analysis_order,
    _assignment_candidate,
    _analysis_stage_condition,
    _analysis_task_where,
    _balanced_assignment_plan,
    _bulk_assignment_result,
    _identity_order,
    _multi_filter_condition,
    _original_address_order,
    _priority_bucket,
    _review_stage_condition,
    _flow_context,
    _registration_update_hooks,
    _mobile_task_inline_editors_data,
    get_mobile_task_residence_detail,
    _scope_where,
    _source_in_community,
    _task_photo_fetched_rows,
    _task_photo_results,
    _task_filter_options,
    _task_order,
    _validate_assignment,
    claim_mobile_task,
    is_flow_task_admin,
    is_flow_task_elevated,
    require_flow_user,
)
from services.parsers import get_parser
from services.task_workflow import TASK_WORKFLOWS, task_state


class FilterOptionsCursor:
    def __init__(self):
        self.rows = []
        self.executions = []

    async def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split())
        self.executions.append((normalized, list(params)))
        if "SELECT projection.community, COUNT(*)" in normalized:
            self.rows = [("冬梅", 12), ("长板", 8)]
        elif "SELECT projection.inspector, COUNT(*)" in normalized:
            self.rows = [("网格员甲", 7), ("", 5)]
        else:
            self.rows = []

    async def fetchall(self):
        return self.rows


class MobileTaskWorkflowTests(unittest.TestCase):
    def test_analysis_search_only_counts_review_stages_across_businesses(self):
        data = AnalysisTaskSearch(
            parser_types=["全链条", "疑似未注销模型三"],
            review_stage="all",
            communities=["长板"],
        )
        where, params = _analysis_task_where(
            {"admin_mode": True, "community_values": None},
            data,
        )

        self.assertIn("projection.parser_type IN (%s,%s)", where)
        self.assertIn("projection.community IN (%s)", where)
        self.assertIn("LIKE '%%无法核实%%'", where)
        self.assertEqual(params[-1], "长板")
        stage_where, stage_params = _analysis_stage_condition(
            ["全链条"],
            "waiting_analysis",
        )
        self.assertIn("projection.parser_type=%s", stage_where)
        self.assertEqual(stage_params, ["全链条"])

    def test_inline_editor_request_limits_current_page(self):
        request = InlineEditorRequest(row_keys=["row-1", "row-2"])
        self.assertEqual(request.row_keys, ["row-1", "row-2"])
        with self.assertRaises(ValueError):
            InlineEditorRequest(row_keys=[])
        with self.assertRaises(ValueError):
            InlineEditorRequest(row_keys=[str(index) for index in range(51)])

    def test_inline_editors_deduplicate_rows_and_skip_photo_queries(self):
        detail = {
            "task": {"conflict": False},
            "sources": [{"id": 7}],
        }
        detail_mock = AsyncMock(return_value=detail)
        with patch(
            "routers.mobile_tasks._mobile_task_detail_data",
            detail_mock,
        ):
            result = asyncio.run(_mobile_task_inline_editors_data(
                "全链条",
                ["row-1", "row-1", "row-2"],
                {},
                object(),
            ))
        self.assertEqual(set(result["items"]), {"row-1", "row-2"})
        self.assertTrue(result["items"]["row-1"]["available"])
        self.assertEqual(detail_mock.await_count, 2)
        for call in detail_mock.await_args_list:
            self.assertFalse(call.kwargs["include_photo_requests"])

    def test_task_summary_keeps_current_and_original_addresses(self):
        workflow = TASK_WORKFLOWS["全链条"]
        summary = workflow.summary({"现住址": "新住址", "地址": "原地址"})
        self.assertEqual(summary["address"], "新住址")
        self.assertEqual(summary["current_address"], "新住址")
        self.assertEqual(summary["original_address"], "原地址")

        original_only = workflow.summary({"地址": "原地址"})
        self.assertEqual(original_only["address"], "原地址")
        self.assertEqual(original_only["current_address"], "")
        self.assertEqual(original_only["original_address"], "原地址")

    def test_task_summary_exposes_deadline_without_falling_back_to_dispatch_date(self):
        workflow = TASK_WORKFLOWS["全链条"]
        summary = workflow.summary({"截止日期": "8.10", "下发日期": "8.01"})
        self.assertEqual(summary["deadline"], "8.10")
        self.assertEqual(summary["date"], "8.10")

        fallback = workflow.summary({"下发日期": "8.01"})
        self.assertEqual(fallback["deadline"], "")
        self.assertEqual(fallback["date"], "8.01")

    def test_task_summary_exposes_grid_worker_follow_up_fields(self):
        workflow = TASK_WORKFLOWS["全链条"]
        summary = workflow.summary({
            "二次反馈": "已联系后重新登记",
            "登记情况": "已注销",
        })

        self.assertEqual(summary["secondary_feedback"], "已联系后重新登记")
        self.assertEqual(summary["registration_status"], "已注销")

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
        self.assertEqual(
            task_state("全链条", {"核查结果": "待登记"}),
            "checked",
        )
        self.assertEqual(
            task_state(
                "全链条",
                {"核查结果": "待登记"},
                registration_status="legacy_completed",
            ),
            "completed",
        )

    def test_fullchain_result_choices_include_pending_registration(self):
        self.assertIn("待登记", TASK_WORKFLOWS["全链条"].result_options)
        self.assertIn("移交（所内）", TASK_WORKFLOWS["全链条"].result_options)
        self.assertIn("移交（所外）", TASK_WORKFLOWS["全链条"].result_options)
        self.assertIn("移交", TASK_WORKFLOWS["全链条"].result_options)

    def test_suspect_return_uses_feedback_as_result(self):
        self.assertIn("离苏", TASK_WORKFLOWS["疑似返苏"].result_options)
        self.assertIn("无需登记", TASK_WORKFLOWS["疑似返苏"].result_options)
        self.assertNotIn(
            "无需登记，原因写备注",
            TASK_WORKFLOWS["疑似返苏"].result_options,
        )
        self.assertIn("移交，移交哪个社区写备注", TASK_WORKFLOWS["疑似返苏"].result_options)
        self.assertEqual(
            task_state("疑似返苏", {"核查结果": "已登记"}),
            "unchecked",
        )
        self.assertEqual(
            task_state("疑似返苏", {"核查反馈": "移交"}),
            "completed",
        )

    def test_suspect_return_merges_short_and_long_no_registration_result(self):
        from services.parsers.suspect_return import SuspectReturnParser

        parser = SuspectReturnParser()
        previous = {"身份证号码": "1", "联系号码": "2", "核查反馈": "无需登记"}
        incoming = {
            "身份证号码": "1",
            "联系号码": "2",
            "核查反馈": "无需登记，原因写备注",
        }
        merged = parser.merge_duplicate_row(previous, incoming)
        self.assertIsNotNone(merged)
        self.assertEqual(merged["核查反馈"], "无需登记，原因写备注")

    def test_suspect_return_keeps_real_result_conflicts(self):
        from services.parsers.suspect_return import SuspectReturnParser

        parser = SuspectReturnParser()
        self.assertIsNone(parser.merge_duplicate_row(
            {"身份证号码": "1", "联系号码": "2", "核查反馈": "无需登记"},
            {"身份证号码": "1", "联系号码": "2", "核查反馈": "离苏"},
        ))

    def test_unrevoked_only_accepts_defined_results(self):
        self.assertEqual(
            task_state("疑似未注销模型三", {"核查结果": "其他"}),
            "unchecked",
        )
        self.assertEqual(
            task_state("疑似未注销模型三", {"核查结果": "在吴"}),
            "completed",
        )
        self.assertEqual(
            task_state("疑似未注销模型三", {"核查结果": "离吴"}),
            "completed",
        )
        self.assertEqual(
            task_state("疑似未注销模型三", {"核查结果": "近期返吴"}),
            "completed",
        )
        self.assertEqual(
            task_state("疑似未注销模型三", {"核查结果": "近期反吴"}),
            "completed",
        )
        self.assertEqual(
            task_state("疑似未注销模型三", {"核查结果": "非本辖区"}),
            "completed",
        )
        self.assertEqual(
            TASK_WORKFLOWS["疑似未注销模型三"].result_options,
            ("近期返吴", "离吴", "在吴", "非本辖区"),
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

    def test_analysis_filter_excludes_workflows_without_unverifiable_result(self):
        self.assertEqual(
            _review_stage_condition("疑似未注销模型三", "waiting_analysis"),
            "1=0",
        )
        self.assertEqual(
            _review_stage_condition("疑似未注销模型三", "analyzed"),
            "1=0",
        )
        self.assertEqual(
            _review_stage_condition("疑似未注销模型三", "all"),
            "1=1",
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
        for position in ("片长", "基础管控", "中队长", "社区民警", "所队领导"):
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

    def test_task_manage_permission_uses_full_station_scope_only_in_task_workbench(self):
        context = asyncio.run(_flow_context(None, {
            "permissions": ["online.task.manage"],
            "member": {"name": "民警甲", "position": "社区民警"},
            "community_names": ["冬梅"],
            "data_scope": "own_department",
        }))
        self.assertTrue(context["admin_mode"])
        self.assertIsNone(context["community_values"])

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

    def test_task_search_accepts_address_and_identity_sorting(self):
        self.assertEqual(TaskSearch(sort="address_asc").sort, "address_asc")
        self.assertEqual(TaskSearch(sort="identity_asc").sort, "identity_asc")

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

    def test_identity_order_uses_business_identity_fields_and_empty_last(self):
        sql = _identity_order("疑似返苏")
        self.assertIn("身份证号码", sql)
        self.assertIn("REGEXP_REPLACE", sql)
        self.assertIn("CASE WHEN", sql)

    def test_explicit_address_sort_matches_the_visible_original_address_column(self):
        sql = _original_address_order("全链条")
        self.assertIn("地址", sql)
        self.assertNotIn("现住址", sql)
        self.assertTrue(_task_order("全链条", "address_asc").endswith(
            "projection.row_key"
        ))

    def test_identity_sort_uses_visible_address_as_stable_tiebreaker(self):
        sql = _task_order("出租房屋核查", "identity_asc")
        self.assertIn("身份证号", sql)
        self.assertIn("房屋地址", sql)
        self.assertNotIn("现住址", sql)

    def test_analysis_field_sort_uses_each_business_contract(self):
        address_sql = _analysis_order(AnalysisTaskSearch(
            parser_types=["全链条", "疑似返苏"],
            sort="address_asc",
        ))
        identity_sql = _analysis_order(AnalysisTaskSearch(
            parser_types=["全链条", "疑似返苏"],
            sort="identity_asc",
        ))
        self.assertIn("projection.parser_type='全链条'", address_sql)
        self.assertNotIn("现住址", address_sql)
        self.assertIn("地址", address_sql)
        self.assertIn("高频抓拍小区", address_sql)
        self.assertIn("身份证号", identity_sql)
        self.assertIn("身份证号码", identity_sql)
        self.assertTrue(identity_sql.endswith("projection.row_key"))


class MobileTaskFilterOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_inspector_options_and_counts_follow_selected_community(self):
        cursor = FilterOptionsCursor()
        result = await _task_filter_options(
            cursor,
            "全链条",
            {
                "admin_mode": True,
                "community_values": None,
                "name": "管理员甲",
                "position": "管理员",
            },
            "all",
            {"permissions": []},
            communities=["冬梅"],
        )

        self.assertEqual(
            result["communities"],
            [
                {"value": "冬梅", "label": "冬梅", "count": 12},
                {"value": "长板", "label": "长板", "count": 8},
            ],
        )
        self.assertEqual(
            result["inspectors"],
            [
                {"value": "网格员甲", "label": "网格员甲", "count": 7},
                {
                    "value": EMPTY_FILTER_VALUE,
                    "label": "未分配核查人",
                    "count": 5,
                },
            ],
        )
        community_query, community_params = cursor.executions[0]
        inspector_query, inspector_params = cursor.executions[1]
        self.assertNotIn("projection.community IN", community_query)
        self.assertEqual(community_params, ["全链条"])
        self.assertIn("projection.community IN (%s)", inspector_query)
        self.assertEqual(inspector_params, ["全链条", "冬梅"])


class MobileTaskAssignmentTests(unittest.IsolatedAsyncioTestCase):
    def test_assignment_candidate_only_exposes_source_and_address(self):
        candidate = _assignment_candidate(
            "全链条",
            "row-key",
            "长板社区",
            {
                "来源": "公安下发",
                "地址": "测试路1号",
                "现住址": "测试路2号",
                "姓名": "测试人员",
                "身份证号": "110101199001010015",
                "电话号码": "13800000000",
            },
        )
        self.assertEqual(
            candidate,
            {
                "row_key": "row-key",
                "community": "长板社区",
                "source": "公安下发",
                "address": "测试路2号",
            },
        )

    def test_assignment_workbench_route_is_read_only_candidate_endpoint(self):
        from routers.mobile_tasks import router

        route = next(
            item for item in router.routes
            if item.path == "/api/mobile-tasks/{parser_type}/assignment-workbench"
        )
        self.assertEqual(route.methods, {"GET"})

    def test_assignment_workbench_query_is_address_sorted_and_bounded(self):
        from routers.mobile_tasks import get_mobile_task_assignment_workbench

        source = inspect.getsource(get_mobile_task_assignment_workbench)
        self.assertIn("ORDER BY {_address_order(parser_type)}", source)
        self.assertIn("SELECT COUNT(*)", source)
        self.assertIn("available_total", source)
        self.assertIn("LIMIT %s", source)
        self.assertIn("MAX_BULK_ASSIGNMENT_TASKS", source)
        self.assertIn("GROUP BY projection.community, projection.inspector", source)
        self.assertIn('"inspector_counts_by_community"', source)
        self.assertEqual(MAX_BULK_ASSIGNMENT_TASKS, 2000)

    def test_bulk_assignment_requires_bounded_chunks(self):
        request = BulkAssignmentRequest(
            row_keys=[f"row-{index}" for index in range(MAX_BULK_ASSIGNMENT_CHUNK)],
            mode="balanced",
            balanced_total=MAX_BULK_ASSIGNMENT_TASKS,
        )
        self.assertEqual(len(request.row_keys), MAX_BULK_ASSIGNMENT_CHUNK)
        with self.assertRaises(ValueError):
            BulkAssignmentRequest(
                row_keys=[
                    f"row-{index}"
                    for index in range(MAX_BULK_ASSIGNMENT_CHUNK + 1)
                ],
                mode="balanced",
            )

    def test_balanced_assignment_keeps_counts_within_one(self):
        plan, counts = _balanced_assignment_plan(
            ["a", "b", "c", "d", "e"],
            ["组员甲", "组员乙", "组员丙"],
        )
        self.assertEqual(list(plan), ["a", "b", "c", "d", "e"])
        self.assertEqual(counts, {"组员甲": 2, "组员乙": 2, "组员丙": 1})

    def test_balanced_assignment_is_stable_across_retried_chunks(self):
        keys = [f"row-{index}" for index in range(11)]
        inspectors = ["组员甲", "组员乙", "组员丙"]
        full_plan, _ = _balanced_assignment_plan(keys, inspectors)
        chunk_plan: dict[str, str] = {}
        for offset in range(0, len(keys), 4):
            plan, _ = _balanced_assignment_plan(
                keys[offset:offset + 4],
                inspectors,
                total_count=len(keys),
                start_index=offset,
            )
            chunk_plan.update(plan)

        self.assertEqual(chunk_plan, full_plan)

    def test_bulk_assignment_outcomes_are_mutually_exclusive(self):
        result = _bulk_assignment_result(
            updated=17,
            skipped=[{"row_key": "skipped-a", "reason": "已有核查人"}],
            failures=[
                {"row_key": "failed-a", "reason": "腾讯回写校验失败"},
                {"row_key": "failed-b", "reason": "任务已变化，请刷新后重试"},
            ],
            inspector="",
            mode="balanced",
            assignment_counts={"组员甲": 9, "组员乙": 8},
        )

        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["updated"] + result["skipped"] + result["failed"], 20)
        self.assertEqual(len(result["details"]), result["skipped"])
        self.assertEqual(len(result["failed_details"]), result["failed"])

    def test_local_bulk_assignment_uses_one_projection_rebuild(self):
        from routers.mobile_tasks import bulk_assign_mobile_tasks

        source = inspect.getsource(bulk_assign_mobile_tasks)
        local_branch = source.split("if local_data_source_enabled():", 2)[-1]
        self.assertIn('action="bulk_assign_local"', local_branch)
        self.assertIn("rebuild=False", local_branch)
        self.assertIn("await rebuild_projection(cur, parser_type)", local_branch)
        self.assertIn("SAVEPOINT bulk_assign_task", local_branch)
        self.assertIn('item["conflict"] = active_count > 1', source)

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


class MobileTaskRegistrationUpdateTests(unittest.IsolatedAsyncioTestCase):
    def _data(self, changes):
        return TaskBatchUpdate(changes=changes, expected_revision=7)

    async def test_existing_registered_task_can_edit_another_field(self):
        prepare, callback, registration_mode = _registration_update_hooks(
            "全链条",
            self._data({"核查人": "组员甲"}),
            {"id": 7},
        )
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        with patch(
            "routers.mobile_tasks.registration_links_by_rows",
            AsyncMock(return_value={}),
        ):
            extra = await prepare(
                cur=cursor,
                source={"id": 11, "row_key": "row-key"},
                current_values={"核查结果": "已登记", "核查人": "组员乙"},
                changes={"核查人": "组员甲"},
            )
            await callback(
                cur=cursor,
                source={"id": 11, "row_hash": "a" * 64},
                before={},
                after={"核查结果": "已登记", "核查人": "组员甲"},
                row_key_before="row-key",
                row_key_after="row-key",
                revision=8,
            )
        self.assertTrue(registration_mode)
        self.assertEqual(extra, {})
        cursor.execute.assert_not_awaited()

    async def test_direct_transition_to_registered_is_still_blocked(self):
        prepare, _, _ = _registration_update_hooks(
            "全链条",
            self._data({"核查结果": "已登记"}),
            {"id": 7},
        )
        with self.assertRaises(HTTPException) as raised:
            await prepare(
                cur=MagicMock(),
                source={"id": 11, "row_key": "row-key"},
                current_values={"核查结果": "待登记"},
                changes={"核查结果": "已登记"},
            )
        self.assertEqual(raised.exception.status_code, 403)

    async def test_legacy_pending_task_can_edit_without_selecting_property(self):
        prepare, callback, _ = _registration_update_hooks(
            "全链条",
            self._data({"核查人": "组员甲"}),
            {"id": 7},
        )
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        with patch(
            "routers.mobile_tasks.registration_links_by_rows",
            AsyncMock(return_value={
                "row-key": {
                    "status": "legacy_completed",
                    "property_id": None,
                    "source_id": 11,
                }
            }),
        ):
            extra = await prepare(
                cur=cursor,
                source={"id": 11, "row_key": "row-key"},
                current_values={"核查结果": "待登记", "核查人": "组员乙"},
                changes={"核查人": "组员甲"},
            )
            await callback(
                cur=cursor,
                source={"id": 11, "row_hash": "a" * 64},
                before={},
                after={"核查结果": "待登记", "核查人": "组员甲"},
                row_key_before="row-key",
                row_key_after="row-key",
                revision=8,
            )
        self.assertEqual(extra, {})
        cursor.execute.assert_not_awaited()

    async def test_linked_pending_edit_advances_expected_local_revision(self):
        prepare, callback, _ = _registration_update_hooks(
            "全链条",
            self._data({"核查人": "组员甲"}),
            {"id": 7},
        )
        cursor = MagicMock()
        cursor.execute = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=("identity", "长板"))
        existing = {
            "status": "matched_once",
            "property_id": 23,
            "source_id": 11,
        }
        with (
            patch(
                "routers.mobile_tasks.registration_links_by_rows",
                AsyncMock(return_value={"row-key": existing}),
            ),
            patch(
                "routers.mobile_tasks.refresh_registration_source_context_after_writeback",
                AsyncMock(return_value=True),
            ) as refresh_mock,
        ):
            await prepare(
                cur=cursor,
                source={"id": 11, "row_key": "row-key"},
                current_values={"核查结果": "待登记", "核查人": "组员乙"},
                changes={"核查人": "组员甲"},
            )
            await callback(
                cur=cursor,
                source={
                    "id": 11,
                    "revision": 7,
                    "row_hash": "a" * 64,
                },
                before={},
                after={"核查结果": "待登记", "核查人": "组员甲"},
                row_key_before="row-key",
                row_key_after="row-key",
                revision=8,
            )

        refresh_mock.assert_awaited_once_with(
            cursor,
            parser_type="全链条",
            source_id=11,
            previous_revision=7,
            previous_row_hash="a" * 64,
            current_revision=8,
            current_row_hash="a" * 64,
        )

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
        self.assertIn(
            "member.position IN ('组长', '组员')",
            cursor.execute.await_args.args[0],
        )

        cursor.fetchone = AsyncMock(return_value=(8,))
        await _validate_assignment(
            cursor,
            {"position": "组长", "community": "长板"},
            {"核查人": "组长甲"},
        )
        self.assertEqual(
            cursor.execute.await_args.args[1],
            ("长板", "组长甲"),
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

    async def test_group_member_claims_unassigned_task_and_saves_edit_together(self):
        queued = {
            "values": {"核查人": "组员甲", "核查结果": "已登记"},
            "row_key": "row-1",
            "revision": 8,
            "pending_sync": True,
        }
        request = MagicMock()
        request.headers = {}
        request.client = None
        user = {
            "id": 7,
            "username": "member-a",
            "permissions": ["online.raw.edit"],
            "member": {"name": "组员甲", "position": "组员"},
            "community_names": ["长板"],
        }
        queue_mock = AsyncMock(return_value=queued)
        with (
            patch(
                "routers.mobile_tasks._flow_context",
                AsyncMock(return_value={
                    "name": "组员甲",
                    "position": "组员",
                    "community": "长板",
                    "community_values": ["长板"],
                    "admin_mode": False,
                }),
            ),
            patch("routers.mobile_tasks.queue_source_fields", queue_mock),
            patch("routers.mobile_tasks.record_admin_audit", AsyncMock()),
        ):
            result = await claim_mobile_task(
                "全链条",
                12,
                TaskBatchUpdate(
                    changes={"核查结果": "已登记"},
                    base_values={"核查结果": ""},
                    expected_revision=7,
                ),
                request,
                user,
                object(),
            )

        kwargs = queue_mock.await_args.kwargs
        self.assertEqual(
            kwargs["changes"],
            {"核查人": "组员甲", "核查结果": "已登记"},
        )
        self.assertEqual(
            kwargs["base_values"],
            {"核查人": "", "核查结果": ""},
        )
        kwargs["current_values_validator"]({"核查人": ""})
        with self.assertRaises(HTTPException) as raised:
            kwargs["current_values_validator"]({"核查人": "其他组员"})
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(result["message"], "已领取任务并保存到本地任务池")

    async def test_only_group_member_can_self_claim_task(self):
        request = MagicMock()
        request.headers = {}
        request.client = None
        with patch(
            "routers.mobile_tasks._flow_context",
            AsyncMock(return_value={
                "name": "组长甲",
                "position": "组长",
                "community": "长板",
                "community_values": ["长板"],
                "admin_mode": False,
            }),
        ):
            with self.assertRaises(HTTPException) as raised:
                await claim_mobile_task(
                    "全链条",
                    12,
                    TaskBatchUpdate(
                        changes={"核查结果": "已登记"},
                        expected_revision=7,
                    ),
                    request,
                    {
                        "permissions": ["online.raw.edit"],
                        "member": {"name": "组长甲", "position": "组长"},
                    },
                    object(),
                )
        self.assertEqual(raised.exception.status_code, 403)


class MobileTaskResidenceDetailTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_task_reads_detail_from_the_single_authorized_source(self):
        task_detail = {
            "task": {"conflict": False},
            "residence_status": {"state": "registered"},
            "sources": [{
                "source_available": True,
                "values": {"身份证号": "fixture-identity", "社区": "冬梅"},
            }],
        }
        projected = {
            "state": "registered",
            "registered_address": "虚构地址",
            "photo_data_url": "",
        }
        with patch(
            "routers.mobile_tasks._mobile_task_detail_data",
            new=AsyncMock(return_value=task_detail),
        ) as detail_mock, patch(
            "routers.mobile_tasks.residence_detail_for_values",
            new=AsyncMock(return_value=projected),
        ) as residence_mock:
            result = await get_mobile_task_residence_detail(
                "流口指令核查",
                "row-key",
                user={"id": 7},
                conn=object(),
            )

        self.assertEqual(result, projected)
        self.assertFalse(detail_mock.await_args.kwargs["include_photo_requests"])
        self.assertEqual(
            residence_mock.await_args.args[2],
            {"身份证号": "fixture-identity", "社区": "冬梅"},
        )

    async def test_non_registered_or_ambiguous_task_never_queries_person_detail(self):
        residence_mock = AsyncMock()
        with patch(
            "routers.mobile_tasks.residence_detail_for_values",
            residence_mock,
        ):
            for detail in (
                {
                    "task": {"conflict": False},
                    "residence_status": {"state": "first_registration"},
                    "sources": [{"source_available": True, "values": {}}],
                },
                {
                    "task": {"conflict": True},
                    "residence_status": {"state": "registered"},
                    "sources": [
                        {"source_available": True, "values": {}},
                        {"source_available": True, "values": {}},
                    ],
                },
            ):
                with self.subTest(detail=detail), patch(
                    "routers.mobile_tasks._mobile_task_detail_data",
                    new=AsyncMock(return_value=detail),
                ):
                    with self.assertRaises(HTTPException) as raised:
                        await get_mobile_task_residence_detail(
                            "流口指令核查",
                            "row-key",
                            user={"id": 7},
                            conn=object(),
                        )
                    self.assertEqual(raised.exception.status_code, 409)
        residence_mock.assert_not_awaited()


class PhotoResultCursor:
    def __init__(self, allowed_ticket_ids=(17,)):
        self.last_sql = ""
        self.executed = []
        self.allowed_ticket_ids = set(allowed_ticket_ids)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=()):
        self.last_sql = " ".join(str(sql).split())
        self.params = params
        self.executed.append((self.last_sql, params))

    async def fetchall(self):
        if self.last_sql.startswith("SELECT DISTINCT detail.source_row_key"):
            return [("row-key-1", 17), ("row-key-2", 18)]
        if self.last_sql.startswith("SELECT order_row.id, order_row.ticket_no"):
            return [(17, "PHOTO-17"), (18, "PHOTO-18")]
        if self.last_sql.startswith("SELECT file_id, original_name"):
            return [("file-17", "照片.jpg", "image/jpeg", 1024)]
        return []

    async def fetchone(self):
        if self.last_sql.startswith("SELECT requester_user_id"):
            ticket_id = int(self.params[0])
            if ticket_id in self.allowed_ticket_ids:
                return (5, None, "", "completed", 2)
            return (6, None, "", "completed", 2)
        if self.last_sql.startswith("SELECT 1 FROM work_order_events"):
            return None
        return None


class PhotoResultPool:
    def __init__(self, cursor):
        self.connection = MagicMock()
        self.acquire = MagicMock()
        connection_context = MagicMock()
        connection_context.__aenter__ = AsyncMock(return_value=self.connection)
        connection_context.__aexit__ = AsyncMock(return_value=None)
        self.acquire.return_value = connection_context
        cursor_context = MagicMock()
        cursor_context.__aenter__ = AsyncMock(return_value=cursor)
        cursor_context.__aexit__ = AsyncMock(return_value=None)
        self.connection.cursor.return_value = cursor_context


class MobileTaskPhotoResultTests(unittest.IsolatedAsyncioTestCase):
    async def test_batch_photo_status_only_returns_accessible_rows(self):
        cursor = PhotoResultCursor(allowed_ticket_ids=(17,))
        pool = PhotoResultPool(cursor)
        user = {"id": 5, "permissions": ["workflow.attachment.view"]}

        with patch("routers.mobile_tasks.settings.WORKFLOW_FEATURE_ENABLED", True), \
             patch("routers.mobile_tasks.db_manager.get_pool", return_value=pool):
            result = await _task_photo_fetched_rows(
                user,
                "全链条",
                ["row-key-1", "row-key-2"],
            )

        self.assertEqual(result, {"row-key-1"})
        self.assertIn("detail.source_row_key IN (%s,%s)", cursor.executed[0][0])

    async def test_batch_photo_status_requires_attachment_permission(self):
        with patch("routers.mobile_tasks.settings.WORKFLOW_FEATURE_ENABLED", True), \
             patch("routers.mobile_tasks.db_manager.get_pool") as get_pool:
            result = await _task_photo_fetched_rows(
                {"id": 5, "permissions": ["online.raw.view"]},
                "全链条",
                ["row-key-1"],
            )
        self.assertEqual(result, set())
        get_pool.assert_not_called()

    async def test_only_returns_attachments_from_exact_accessible_task_link(self):
        cursor = PhotoResultCursor(allowed_ticket_ids=(17,))
        pool = PhotoResultPool(cursor)
        user = {"id": 5, "permissions": ["workflow.attachment.view"]}

        with patch("routers.mobile_tasks.settings.WORKFLOW_FEATURE_ENABLED", True), \
             patch("routers.mobile_tasks.db_manager.get_pool", return_value=pool):
            result = await _task_photo_results(user, "全链条", "row-key-1")

        self.assertEqual([item["ticket_id"] for item in result], [17])
        self.assertEqual(result[0]["attachments"][0]["file_id"], "file-17")
        self.assertEqual(cursor.executed[0][1], ("全链条", "row-key-1"))
        self.assertTrue(any(
            "work_order_attachments" in sql for sql, _ in cursor.executed
        ))

    async def test_workflow_disabled_returns_no_photo_projection(self):
        with patch("routers.mobile_tasks.settings.WORKFLOW_FEATURE_ENABLED", False):
            result = await _task_photo_results({"id": 5}, "全链条", "row-key-1")
        self.assertEqual(result, [])

    async def test_user_without_attachment_permission_gets_no_projection(self):
        with patch("routers.mobile_tasks.settings.WORKFLOW_FEATURE_ENABLED", True), \
             patch("routers.mobile_tasks.db_manager.get_pool") as get_pool:
            result = await _task_photo_results(
                {"id": 5, "permissions": ["online.raw.view"]},
                "全链条",
                "row-key-1",
            )
        self.assertEqual(result, [])
        get_pool.assert_not_called()


if __name__ == "__main__":
    unittest.main()
