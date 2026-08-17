import json
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException, Request

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from routers.grid_members import _replace_area_leaders
from routers.query import (
    CellUpdate,
    _looks_like_automatic_text_coercion,
    _managed_column_metadata,
    _row_values_match,
    _source_data_version,
    new_row_required_fields,
    update_source_cell,
    update_source_fields,
)
from services.online_edit_permissions import (
    can_manage_rows,
    editable_fields_for_row,
    effective_edit_communities,
    effective_view_communities,
    inspector_option_context,
    validate_row_change,
    validate_row_changes,
)
from services.online_source import (
    cleanup_expired_writeback_audit,
    match_source_cache_rows,
    rebuild_projection,
    source_row_hash,
)
from services.online_local_writeback import (
    SourceRowRelocatedError,
    _retry_error_details,
    local_sync_state,
    overlay_local_values,
    split_remote_changes,
    validate_conflict_source_identity,
    validate_remote_source_identity,
    writeback_cell_metadata,
)
from services.parsers import get_parser
from services.permissions import (
    ONLINE_RAW_EDIT,
    ONLINE_RAW_ROW_MANAGE,
    ONLINE_RAW_VIEW,
)
from services.txdocs_client import TxDocsAPIError, TxDocsClient


def make_user(position, *, communities=None, view_scope="own_department", permissions=None):
    permissions = permissions or [ONLINE_RAW_VIEW, ONLINE_RAW_EDIT]
    departments = [
        {"type": "community", "community_name": name}
        for name in (communities or [])
    ]
    return {
        "id": 7,
        "username": "tester",
        "role": "member",
        "member": {"id": 17, "position": position},
        "community_names": communities or [],
        "departments": departments,
        "permissions": permissions,
        "data_scope": view_scope,
        "permission_scopes": {
            ONLINE_RAW_VIEW: view_scope,
            ONLINE_RAW_EDIT: "all",
        },
        "permission_groups": [],
    }


class SqlAwareCursor:
    def __init__(self, *, areas=None, formal=None):
        self.areas = areas or []
        self.formal = formal or {}
        self.one = None
        self.rows = []
        self.calls = []
        self.many_calls = []
        self.rowcount = 0

    async def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        self.calls.append((compact, params))
        self.one = None
        self.rows = []
        if "FROM _area_leader_links" in compact:
            self.rows = [(name,) for name in self.areas]
        elif "FROM _communities AS community" in compact and "UNION" in compact:
            value = str((params or [""])[0])
            formal = self.formal.get(value)
            self.one = (formal,) if formal else None
        elif "WHERE position='片长' AND id IN" in compact:
            self.rows = [(value,) for value in (params or [])]
        elif "DELETE FROM _online_writeback_audit" in compact:
            self.rowcount = 3

    async def executemany(self, sql, rows):
        self.many_calls.append((" ".join(sql.split()), list(rows)))

    async def fetchone(self):
        return self.one

    async def fetchall(self):
        return list(self.rows)


class ProjectionCursor:
    def __init__(self, source_rows, pending_rows=None):
        self.source_rows = source_rows
        self.pending_rows = pending_rows or []
        self.mode = ""
        self.many_rows = []

    async def execute(self, sql, params=None):
        del params
        compact = " ".join(sql.split())
        if compact.startswith("SELECT id, row_key, values_json"):
            self.mode = "sources"
        elif compact.startswith("SELECT source_id, field_name, local_value"):
            self.mode = "local_changes"
        elif compact.startswith("SELECT row_key_before"):
            self.mode = "pending"
        else:
            self.mode = "write"

    async def fetchall(self):
        if self.mode == "sources":
            return [
                (index, row_key, values_json)
                for index, (row_key, values_json) in enumerate(self.source_rows, 1)
            ]
        if self.mode == "pending":
            return list(self.pending_rows)
        return []

    async def executemany(self, sql, rows):
        del sql
        self.many_rows = list(rows)


class ManagedMetadataCursor:
    def __init__(self, cached_metadata=None):
        self.mode = ""
        self.cached_metadata = cached_metadata

    async def execute(self, sql, params=None):
        del params
        compact = " ".join(sql.split())
        if "FROM _communities AS community" in compact:
            self.mode = "communities"
        elif "FROM _grid_members AS member" in compact:
            self.mode = "members"
        elif "FROM _online_source_rows" in compact:
            self.mode = "cached_metadata"

    async def fetchall(self):
        if self.mode == "communities":
            return [("长板",), ("龙河",)]
        if self.mode == "members":
            return [("网格员甲",), ("网格员乙",)]
        if self.mode == "cached_metadata" and self.cached_metadata:
            return [(json.dumps(self.cached_metadata, ensure_ascii=False),)]
        return []


class CursorContext:
    def __init__(self, cursor):
        self.cursor = cursor

    async def __aenter__(self):
        return self.cursor

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return CursorContext(self._cursor)


class ConflictCursor(SqlAwareCursor):
    def __init__(self, source_values):
        super().__init__()
        self.source_values = source_values

    async def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        self.calls.append((compact, params))
        if "config_key='online_writeback_enabled'" in compact:
            self.one = ("1",)
        elif "FROM _online_source_rows AS source" in compact:
            self.one = (
                1, 2, "sheet", 10, "row-key", source_row_hash(self.source_values),
                json.dumps(self.source_values, ensure_ascii=False), "{}", 1,
                "测试来源", "file", "sheet", 1, 1,
            )
        elif "GET_LOCK" in compact or "RELEASE_LOCK" in compact:
            self.one = (1,)
        else:
            self.one = None


class BatchUpdateCursor(ConflictCursor):
    def __init__(self, source_values):
        super().__init__(source_values)
        self.lastrowid = 31

    async def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        if "FROM _communities AS community" in compact and "UNION" in compact:
            self.calls.append((compact, params))
            self.one = ("长板",)
            return
        await super().execute(sql, params)


class OnlineWritebackTests(unittest.IsolatedAsyncioTestCase):
    def test_blank_rental_result_select_uses_business_write_options(self):
        metadata = writeback_cell_metadata(
            "出租房屋核查",
            "核查结果",
            {
                "type": "select",
                "options": [{"id": "", "text": "", "color": None}],
                "multiple": False,
            },
        )

        self.assertEqual(
            [item["text"] for item in metadata["write_options"]],
            [
                "已登记",
                "离苏",
                "常口",
                "无需登记，原因写备注",
                "移交，移交哪个社区写备注",
                "无法核实",
            ],
        )
        client = TxDocsClient("client", "token", "user")
        for result in ("已登记", "常口"):
            with self.subTest(result=result):
                request = client.build_update_cell_request(
                    "sheet", 47, 9, result, metadata, "核查结果"
                )
                cell = request["updateRangeRequest"]["gridData"]["rows"][0][
                    "values"
                ][0]
                self.assertEqual(cell["cellValue"], {"text": result})

    def test_blank_qmf_result_select_uses_business_write_options(self):
        metadata = writeback_cell_metadata(
            "疑似未注销模型三",
            "核查结果",
            {
                "type": "select",
                "options": [{"id": "", "text": "", "color": None}],
                "multiple": False,
            },
        )

        self.assertEqual(
            [item["text"] for item in metadata["write_options"]],
            ["近期返吴", "离吴", "在吴"],
        )
        client = TxDocsClient("client", "token", "user")
        for result in ("近期返吴", "离吴", "在吴"):
            with self.subTest(result=result):
                request = client.build_update_cell_request(
                    "sheet", 8, 6, result, metadata, "核查结果"
                )
                cell = request["updateRangeRequest"]["gridData"]["rows"][0][
                    "values"
                ][0]
                self.assertEqual(cell["cellValue"], {"text": result})

    def test_result_writeback_keeps_real_options_and_adds_business_fallbacks(self):
        metadata = writeback_cell_metadata(
            "出租房屋核查",
            "核查结果",
            {
                "type": "select",
                "options": [{"id": "result-1", "text": "已登记"}],
            },
        )

        self.assertEqual(
            metadata["write_options"][0],
            {"id": "result-1", "text": "已登记"},
        )
        self.assertIn(
            {"id": "常口", "text": "常口"},
            metadata["write_options"],
        )

    def test_non_result_select_does_not_receive_result_options(self):
        metadata = writeback_cell_metadata(
            "出租房屋核查",
            "核查人",
            {"type": "select", "options": [{"id": "", "text": ""}]},
        )

        self.assertNotIn("write_options", metadata)

    def test_write_validation_retry_has_safe_specific_error(self):
        self.assertEqual(
            _retry_error_details(ValueError("无效的下拉选项: 测试值")),
            ("write_validation_failed", "腾讯写回参数校验未通过"),
        )
        self.assertEqual(
            _retry_error_details(TxDocsAPIError("额度已用完", code="400011")),
            ("txdocs_api_failed", "腾讯接口暂未完成写回"),
        )
        self.assertEqual(
            _retry_error_details(SourceRowRelocatedError("moved")),
            ("source_relocated", "腾讯来源行位置已变化，等待同步重新定位"),
        )

    def test_cache_refresh_tracks_a_business_row_when_its_physical_row_moves(self):
        existing = [
            {"id": 7, "sheet_id": "sheet", "physical_row": 10,
             "row_key": "person-a", "has_local_changes": True},
            {"id": 8, "sheet_id": "sheet", "physical_row": 11,
             "row_key": "person-b", "has_local_changes": False},
        ]
        incoming = [
            {"sheet_id": "sheet", "physical_row": 11, "row_key": "person-a"},
            {"sheet_id": "sheet", "physical_row": 12, "row_key": "person-b"},
        ]

        matched, inserted, removed = match_source_cache_rows(existing, incoming)

        self.assertEqual(
            [(old["id"], new["physical_row"]) for old, new in matched],
            [(7, 11), (8, 12)],
        )
        self.assertEqual(inserted, [])
        self.assertEqual(removed, [])

    def test_pending_row_is_not_reused_for_an_unrelated_tencent_row(self):
        existing = [{
            "id": 7, "sheet_id": "sheet", "physical_row": 10,
            "row_key": "person-a", "has_local_changes": True,
        }]
        incoming = [{
            "sheet_id": "sheet", "physical_row": 10, "row_key": "person-b",
        }]

        matched, inserted, removed = match_source_cache_rows(existing, incoming)

        self.assertEqual(matched, [])
        self.assertEqual(inserted, incoming)
        self.assertEqual(removed, existing)

    def test_pending_business_key_repairs_a_source_id_corrupted_by_stale_row_read(self):
        existing = [
            {
                "id": 7,
                "sheet_id": "sheet",
                "physical_row": 212,
                "row_key": "other-person",
                "expected_row_key": "person-a",
                "has_local_changes": True,
            },
            {
                "id": 8,
                "sheet_id": "sheet",
                "physical_row": 193,
                "row_key": "person-a",
                "has_local_changes": False,
            },
        ]
        incoming = [
            {"sheet_id": "sheet", "physical_row": 193, "row_key": "person-a"},
            {"sheet_id": "sheet", "physical_row": 212, "row_key": "other-person"},
        ]

        matched, inserted, removed = match_source_cache_rows(existing, incoming)

        self.assertEqual(
            [
                (old["id"], new["row_key"], new["physical_row"])
                for old, new in matched
            ],
            [(7, "person-a", 193)],
        )
        self.assertEqual(inserted, [incoming[1]])
        self.assertEqual(removed, [existing[1]])

    def test_row_identity_change_blocks_all_field_level_merges(self):
        parser = get_parser("全链条")
        expected = {column: "" for column in parser.COLUMNS}
        expected.update({
            "下发日期": "08-17",
            "社区": "冬梅",
            "身份证号": "410000000000000001",
            "电话号码": "18800000001",
        })
        unrelated = {**expected, "身份证号": "410000000000000002"}
        changes = [
            {
                "row_key": parser.make_row_key(expected),
                "field_name": "现住址",
                "base_value": "",
                "local_value": "平台地址",
            },
            {
                "row_key": parser.make_row_key(expected),
                "field_name": "核查结果",
                "base_value": "",
                "local_value": "移交",
            },
        ]

        with self.assertRaises(SourceRowRelocatedError):
            validate_remote_source_identity(parser, changes, unrelated)

        validate_remote_source_identity(parser, changes, expected)

    def test_platform_conflict_resolution_rejects_a_stale_source_id(self):
        with self.assertRaisesRegex(ValueError, "等待同步重新定位"):
            validate_conflict_source_identity("other-person", ["person-a"])

        validate_conflict_source_identity("person-a", ["person-a"])

    def test_remote_same_field_change_conflicts_but_other_fields_can_merge(self):
        changes = [{
            "field_name": "核查结果",
            "base_value": "无法核实",
            "local_value": "已登记",
        }]

        safe, conflicts = split_remote_changes(
            {"核查结果": "无法核实", "现住址": "腾讯新地址"}, changes
        )
        self.assertEqual(len(safe), 1)
        self.assertEqual(conflicts, [])

        safe, conflicts = split_remote_changes(
            {"核查结果": "已注销", "现住址": "腾讯新地址"}, changes
        )
        self.assertEqual(safe, [])
        self.assertEqual(conflicts[0]["remote_value"], "已注销")

    def test_conflict_keeps_platform_value_visible_and_has_highest_sync_priority(self):
        changes = [
            {"field_name": "现住址", "local_value": "平台地址", "status": "retry"},
            {"field_name": "核查结果", "local_value": "已登记", "status": "conflict"},
        ]

        effective = overlay_local_values(
            {"现住址": "腾讯地址", "核查结果": "无法核实"}, changes
        )

        self.assertEqual(effective["现住址"], "平台地址")
        self.assertEqual(effective["核查结果"], "已登记")
        self.assertEqual(local_sync_state(changes), "conflict")

    async def test_source_data_version_contains_no_business_content(self):
        cursor = AsyncMock()
        cursor.fetchone.return_value = (12, 19, None)
        version = await _source_data_version(cursor, "全链条")
        self.assertEqual(version, "12:19:")
        self.assertEqual(cursor.execute.await_args.args[1], ("全链条",))

    def test_detects_known_text_number_coercion_patterns(self):
        self.assertTrue(_looks_like_automatic_text_coercion(
            "身份证号",
            "32052519911016025X",
            "320525199110160260",
            "text",
        ))
        self.assertTrue(_looks_like_automatic_text_coercion(
            "手机号",
            "1380013800013900138000",
            "1380013800013900137984",
            "text",
        ))
        self.assertTrue(_looks_like_automatic_text_coercion(
            "下发日期",
            "7.30",
            "7.3",
            "text",
        ))
        self.assertFalse(_looks_like_automatic_text_coercion(
            "身份证号",
            "32052519911016025X",
            "320525199110160260",
            "number",
        ))

    async def test_suspicious_automatic_coercion_is_rejected_before_audit(self):
        parser = get_parser("全链条")
        cached = {column: "" for column in parser.COLUMNS}
        cached.update({
            "社区": "长板",
            "身份证号": "32052519911016025X",
            "电话号码": "13800138000",
            "下发日期": "7.30",
        })
        cursor = BatchUpdateCursor(cached)
        conn = FakeConnection(cursor)
        client = AsyncMock()
        client.read_source_row.return_value = {
            "values": cached,
            "cell_meta": {
                column: {"type": "text", "write_type": "text"}
                for column in parser.COLUMNS
            },
        }
        request = Request({
            "type": "http", "method": "PATCH", "path": "/", "headers": [],
            "scheme": "http", "server": ("test", 80), "client": ("127.0.0.1", 1),
        })
        metadata = {
            column: {"type": "text", "write_type": "text"}
            for column in parser.COLUMNS
        }

        with patch("routers.query._oauth_client", AsyncMock(return_value=client)), \
             patch("routers.query.resolve_source_columns", AsyncMock(return_value=parser.COLUMNS)), \
             patch("routers.query.inspector_option_context", AsyncMock(return_value={})), \
             patch("routers.query._managed_column_metadata", AsyncMock(return_value=metadata)), \
             patch("routers.query.validate_row_changes", AsyncMock()), \
             patch("routers.query._insert_writeback_audit", AsyncMock()) as insert_audit:
            with self.assertRaises(HTTPException) as raised:
                await update_source_fields(
                    parser_type="全链条",
                    source_id=1,
                    changes={"身份证号": "320525199110160260"},
                    expected_revision=1,
                    request=request,
                    user=make_user("超级管理员", view_scope="all"),
                    conn=conn,
                )

        self.assertEqual(raised.exception.status_code, 400)
        insert_audit.assert_not_awaited()
        client.batch_update.assert_not_awaited()

    async def test_view_scope_can_expand_but_edit_scope_stays_with_position(self):
        user = make_user("组员", communities=["长板"], view_scope="all")

        self.assertIsNone(effective_view_communities(user))
        self.assertEqual(
            await effective_edit_communities(SqlAwareCursor(), user),
            ["长板"],
        )

    async def test_area_leader_views_all_but_only_edits_assigned_areas(self):
        user = make_user("片长", view_scope="own_department")

        self.assertIsNone(effective_view_communities(user))
        self.assertEqual(
            await effective_edit_communities(
                SqlAwareCursor(areas=["长板", "龙河"]), user
            ),
            ["长板", "龙河"],
        )

    async def test_area_leader_cannot_move_row_to_another_area(self):
        parser = get_parser("全链条")
        user = make_user("片长")
        before = {column: "" for column in parser.COLUMNS}
        before.update({"社区": "长板", "身份证号": "1", "电话号码": "2", "下发日期": "3"})
        after = {**before, "社区": "冬梅"}
        cursor = SqlAwareCursor(
            areas=["长板", "龙河"],
            formal={"长板": "长板", "冬梅": "冬梅"},
        )

        with self.assertRaises(PermissionError):
            await validate_row_change(cursor, user, parser, before, after, "社区")

    def test_secondary_feedback_unlocks_only_for_unverifiable_result(self):
        user = make_user("组员", communities=["长板"])
        columns = ["核查人", "登记情况", "核查结果", "研判", "二次反馈"]

        unlocked = editable_fields_for_row(user, columns, {"核查结果": "无法核实"})
        locked = editable_fields_for_row(user, columns, {"核查结果": "移交"})

        self.assertIn("二次反馈", unlocked)
        self.assertNotIn("二次反馈", locked)
        self.assertNotIn("登记情况", unlocked)
        self.assertNotIn("研判", unlocked)

    async def test_batch_validation_can_save_result_and_secondary_together(self):
        user = make_user("组员", communities=["长板"])
        parser = get_parser("全链条")
        before = {column: "" for column in parser.COLUMNS}
        before.update({"社区": "长板", "身份证号": "1", "电话号码": "2", "下发日期": "3"})
        after = {**before, "核查结果": "无法核实", "二次反馈": "再次联系未果"}

        await validate_row_changes(
            SqlAwareCursor(formal={"长板": "长板"}),
            user,
            parser,
            before,
            after,
            ["核查结果", "二次反馈"],
        )

    async def test_secondary_feedback_is_preserved_when_result_becomes_final(self):
        user = make_user("组员", communities=["长板"])
        parser = get_parser("全链条")
        before = {column: "" for column in parser.COLUMNS}
        before.update({
            "社区": "长板",
            "身份证号": "1",
            "电话号码": "2",
            "下发日期": "3",
            "核查结果": "无法核实",
        })
        after = {
            **before,
            "核查结果": "已登记",
            "二次反馈": "重新联系后确认可以登记",
        }

        await validate_row_changes(
            SqlAwareCursor(formal={"长板": "长板"}),
            user,
            parser,
            before,
            after,
            ["核查结果", "二次反馈"],
        )

    async def test_secondary_feedback_cannot_be_changed_after_final_result(self):
        user = make_user("组员", communities=["长板"])
        parser = get_parser("全链条")
        before = {column: "" for column in parser.COLUMNS}
        before.update({
            "社区": "长板",
            "身份证号": "1",
            "电话号码": "2",
            "下发日期": "3",
            "核查结果": "已登记",
            "二次反馈": "历史反馈",
        })

        with self.assertRaises(PermissionError):
            await validate_row_changes(
                SqlAwareCursor(formal={"长板": "长板"}),
                user,
                parser,
                before,
                {**before, "二次反馈": "篡改历史反馈"},
                ["二次反馈"],
            )

    async def test_model_three_mobile_remark_is_editable_without_changing_result(self):
        user = make_user("组员", communities=["长板"])
        parser = get_parser("疑似未注销模型三")
        before = {column: "" for column in parser.COLUMNS}
        before.update({"下发社区": "长板", "身份证号": "1", "联系方式": "2"})
        after = {**before, "备注": "已联系，待补充材料"}

        await validate_row_changes(
            SqlAwareCursor(formal={"长板": "长板"}),
            user,
            parser,
            before,
            after,
            ["备注"],
        )

    async def test_assignment_options_only_include_active_group_members(self):
        cursor = SqlAwareCursor()
        await inspector_option_context(
            cursor,
            make_user("基础管控"),
            assignment_only=True,
        )
        self.assertTrue(any(
            "member.position IN ('组长', '组员')" in sql
            for sql, _ in cursor.calls
        ))

    async def test_batch_validation_cannot_unlock_cross_community_row(self):
        user = make_user("组员", communities=["长板"])
        parser = get_parser("全链条")
        before = {column: "" for column in parser.COLUMNS}
        before.update({"社区": "冬梅", "身份证号": "1", "电话号码": "2", "下发日期": "3"})
        after = {**before, "核查结果": "无法核实", "二次反馈": "已联系"}

        with self.assertRaises(PermissionError):
            await validate_row_changes(
                SqlAwareCursor(formal={"冬梅": "冬梅"}),
                user,
                parser,
                before,
                after,
                ["核查结果", "二次反馈"],
            )

    def test_row_management_requires_global_position_and_permission(self):
        permissions = [ONLINE_RAW_VIEW, ONLINE_RAW_EDIT, ONLINE_RAW_ROW_MANAGE]
        self.assertFalse(can_manage_rows(make_user("片长", permissions=permissions)))
        self.assertTrue(can_manage_rows(make_user("基础管控", permissions=permissions)))

    def test_new_rows_require_every_business_key(self):
        parser = get_parser("全链条")
        row = {column: "" for column in parser.COLUMNS}
        row.update({"社区": "长板", "身份证号": "1"})

        with self.assertRaisesRegex(ValueError, "电话号码"):
            parser.validate_new_row(row)

    def test_query_exposes_new_row_required_fields_in_sheet_order(self):
        parser = get_parser("全链条")

        self.assertEqual(
            new_row_required_fields(parser),
            ["下发日期", "社区", "身份证号", "电话号码"],
        )

    def test_existing_incomplete_key_can_be_repaired_one_cell_at_a_time(self):
        parser = get_parser("出租房屋核查")
        row = {column: "" for column in parser.COLUMNS}
        row["身份证号"] = "320000000000000000"

        parser.validate_existing_row_key(row)

    async def test_rental_projection_merges_duplicates_and_marks_pending(self):
        parser = get_parser("出租房屋核查")
        first = {column: "" for column in parser.COLUMNS}
        first.update({"社区": "长板", "身份证号": "1", "手机号码": "2", "现住址": "旧址"})
        second = {**first, "现住址": "新址", "核查结果": "移交"}
        cursor = ProjectionCursor(
            [("same-key", json.dumps(first, ensure_ascii=False)),
             ("same-key", json.dumps(second, ensure_ascii=False))],
            [(None, "same-key")],
        )

        await rebuild_projection(cursor, "出租房屋核查")

        self.assertEqual(len(cursor.many_rows), 1)
        projection = cursor.many_rows[0]
        # identity_hmac and first_dispatch_at were added before the workflow
        # fields in the projection tuple.
        self.assertEqual(projection[7], "completed")
        self.assertEqual(projection[8], 2)
        self.assertEqual(projection[9], 0)
        self.assertEqual(projection[11], "pending")
        self.assertEqual(json.loads(projection[2])["现住址"], "新址")

    async def test_non_mergeable_duplicates_are_exposed_as_conflict(self):
        parser = get_parser("全链条")
        first = {column: "" for column in parser.COLUMNS}
        first.update({"社区": "长板", "身份证号": "1", "电话号码": "2", "下发日期": "3"})
        second = {**first, "现住址": "不同"}
        cursor = ProjectionCursor([
            ("same-key", json.dumps(first, ensure_ascii=False)),
            ("same-key", json.dumps(second, ensure_ascii=False)),
        ])

        await rebuild_projection(cursor, "全链条")

        self.assertEqual(cursor.many_rows[0][8], 2)
        self.assertEqual(cursor.many_rows[0][9], 1)

    async def test_fullchain_projection_retains_registration_value(self):
        parser = get_parser("全链条")
        values = {column: "" for column in parser.COLUMNS}
        values.update({
            "社区": "长板",
            "身份证号": "1",
            "电话号码": "2",
            "下发日期": "3",
            "登记情况": "已登记",
        })
        cursor = ProjectionCursor([
            ("row-key", json.dumps(values, ensure_ascii=False)),
        ])

        await rebuild_projection(cursor, "全链条")

        projection_values = json.loads(cursor.many_rows[0][2])
        self.assertEqual(projection_values["登记情况"], "已登记")
        self.assertIn("已登记", cursor.many_rows[0][10])

    async def test_external_change_returns_409_and_refreshes_cache(self):
        parser = get_parser("全链条")
        cached = {column: "" for column in parser.COLUMNS}
        cached.update({"社区": "长板", "身份证号": "1", "电话号码": "2", "下发日期": "3"})
        external = {**cached, "现住址": "他人刚刚修改"}
        cursor = ConflictCursor(cached)
        conn = FakeConnection(cursor)
        client = AsyncMock()
        client.resolve_column_layout.return_value = parser.source_column_layouts()[0]
        client.read_source_row.return_value = {
            "values": external,
            "cell_meta": {},
        }
        request = Request({
            "type": "http", "method": "PATCH", "path": "/", "headers": [],
            "scheme": "http", "server": ("test", 80), "client": ("127.0.0.1", 1),
        })

        with patch("routers.query._oauth_client", AsyncMock(return_value=client)), \
             patch("routers.query._refresh_spreadsheet", AsyncMock()) as refresh:
            with self.assertRaises(HTTPException) as raised:
                await update_source_cell(
                    "全链条", 1,
                    CellUpdate(column="现住址", value="我的修改", expected_revision=1),
                    request, make_user("组员", communities=["长板"]), conn,
                )

        self.assertEqual(raised.exception.status_code, 409)
        refresh.assert_awaited_once()
        client.close.assert_awaited_once()

    async def test_mobile_batch_update_uses_one_request_and_one_cache_revision(self):
        parser = get_parser("全链条")
        cached = {column: "" for column in parser.COLUMNS}
        cached.update({
            "社区": "长板",
            "身份证号": "1",
            "电话号码": "2",
            "下发日期": "3",
        })
        verified = {
            **cached,
            "现住址": "长板一号",
            "核查结果": "无法核实",
            "二次反馈": "再次联系未果",
        }
        cursor = BatchUpdateCursor(cached)
        conn = FakeConnection(cursor)
        client = AsyncMock()
        client.resolve_column_layout.return_value = parser.source_column_layouts()[0]
        client.read_source_row.side_effect = [
            {"values": cached, "cell_meta": {}},
            {"values": verified, "cell_meta": {}},
        ]
        client.build_update_cell_request = Mock(
            side_effect=lambda *args: {"column": args[2], "value": args[3]}
        )
        request = Request({
            "type": "http", "method": "PATCH", "path": "/", "headers": [],
            "scheme": "http", "server": ("test", 80), "client": ("127.0.0.1", 1),
        })

        with patch("routers.query._oauth_client", AsyncMock(return_value=client)), \
             patch("routers.query._insert_writeback_audit", AsyncMock(return_value=31)), \
             patch("routers.query._update_writeback_audit", AsyncMock()), \
             patch("routers.query.update_cached_source_row", AsyncMock(return_value=("row-key", 2))) as cache_update, \
             patch("routers.query.record_admin_audit", AsyncMock()):
            result = await update_source_fields(
                parser_type="全链条",
                source_id=1,
                changes={
                    "现住址": "长板一号",
                    "核查结果": "无法核实",
                    "二次反馈": "再次联系未果",
                },
                expected_revision=1,
                request=request,
                user=make_user("组员", communities=["长板"]),
                conn=conn,
            )

        self.assertTrue(result["pending_sync"])
        self.assertEqual(len(client.batch_update.await_args.args[1]), 3)
        self.assertEqual(
            [request["column"] for request in client.batch_update.await_args.args[1]],
            [11, 12, 14],
        )
        cache_update.assert_awaited_once()

    async def test_area_accepts_multiple_leaders(self):
        cursor = SqlAwareCursor()

        await _replace_area_leaders(cursor, 8, [11, 12])

        self.assertEqual(cursor.many_calls[0][1], [(8, 11), (8, 12)])

    async def test_audit_cleanup_uses_ninety_day_cutoff(self):
        cursor = SqlAwareCursor()

        deleted = await cleanup_expired_writeback_audit(cursor)

        self.assertEqual(deleted, 3)
        self.assertIn("INTERVAL 90 DAY", cursor.calls[-1][0])

    def test_delete_dimension_uses_official_one_based_row_range(self):
        client = TxDocsClient("client", "token", "user")

        self.assertEqual(
            client.build_delete_row_request("sheet", 12)["deleteDimensionRequest"],
            {"sheetId": "sheet", "dimension": "ROW", "startIndex": 12, "endIndex": 13},
        )

    def test_new_row_verification_compares_numeric_display_equivalently(self):
        columns = ["下发时间", "姓名"]
        self.assertTrue(_row_values_match(
            {"下发时间": "7.30", "姓名": "对象"},
            {"下发时间": "7.3", "姓名": "对象"},
            {"下发时间": {"type": "number"}, "姓名": {"type": "text"}},
            columns,
        ))

    async def test_query_uses_managed_community_and_member_select_options(self):
        parser = get_parser("全链条")

        metadata = await _managed_column_metadata(
            ManagedMetadataCursor(),
            parser,
            {"核查结果": {"type": "select", "options": [{"id": "1", "text": "移交"}]}},
        )

        self.assertEqual(
            metadata[parser.COMMUNITY_COLUMN],
            {
                "type": "select",
                "multiple": False,
                "options": [
                    {"id": "长板", "text": "长板"},
                    {"id": "龙河", "text": "龙河"},
                ],
                "write_type": "text",
                "write_multiple": False,
                "write_options": [],
            },
        )
        self.assertEqual(
            metadata["核查人"]["options"],
            [
                {"id": "网格员甲", "text": "网格员甲"},
                {"id": "网格员乙", "text": "网格员乙"},
            ],
        )
        self.assertEqual(metadata["核查结果"]["type"], "select")

    async def test_managed_text_inspector_keeps_text_physical_write(self):
        parser = get_parser("全链条")
        metadata = await _managed_column_metadata(
            ManagedMetadataCursor(),
            parser,
            {"核查人": {"type": "text"}},
        )

        self.assertEqual(metadata["核查人"]["type"], "select")
        self.assertEqual(metadata["核查人"]["write_type"], "text")
        request = TxDocsClient("client", "token", "user").build_update_cell_request(
            "sheet", 8, 4, "网格员甲", metadata["核查人"]
        )
        cell = request["updateRangeRequest"]["gridData"]["rows"][0]["values"][0]
        self.assertEqual(cell["cellValue"], {"text": "网格员甲"})

    async def test_real_select_inspector_validates_option_then_writes_text(self):
        parser = get_parser("全链条")
        metadata = await _managed_column_metadata(
            ManagedMetadataCursor(),
            parser,
            {"核查人": {
                "type": "select",
                "multiple": False,
                "options": [{"id": "member-1", "text": "网格员甲"}],
            }},
        )

        self.assertEqual(metadata["核查人"]["write_type"], "select")
        request = TxDocsClient("client", "token", "user").build_update_cell_request(
            "sheet", 8, 4, "网格员甲", metadata["核查人"]
        )
        cell = request["updateRangeRequest"]["gridData"]["rows"][0]["values"][0]
        self.assertEqual(cell["cellValue"], {"text": "网格员甲"})
        with self.assertRaisesRegex(ValueError, "无效的下拉选项"):
            TxDocsClient("client", "token", "user").build_update_cell_request(
                "sheet", 8, 4, "不存在的人", metadata["核查人"]
            )

    def test_date_number_metadata_is_written_as_text_to_preserve_trailing_zero(self):
        request = TxDocsClient("client", "token", "user").build_update_cell_request(
            "sheet", 8, 4, "8.10", {"type": "number"}, column_name="下发日期"
        )
        cell = request["updateRangeRequest"]["gridData"]["rows"][0]["values"][0]
        self.assertEqual(cell["cellValue"], {"text": "8.10"})

    def test_identity_and_phone_number_metadata_are_always_written_as_text(self):
        client = TxDocsClient("client", "token", "user")
        cases = [
            ("身份证号", "320525199110160258"),
            ("参考身份证号码", "320525199110160251"),
            ("手机号码", "13800138000"),
            ("联系方式", "13800138000"),
        ]
        for column_name, value in cases:
            with self.subTest(column_name=column_name):
                request = client.build_update_cell_request(
                    "sheet", 8, 4, value, {"type": "number"}, column_name=column_name
                )
                cell = request["updateRangeRequest"]["gridData"]["rows"][0]["values"][0]
                self.assertEqual(cell["cellValue"], {"text": value})

    def test_regular_number_metadata_still_uses_number(self):
        request = TxDocsClient("client", "token", "user").build_update_cell_request(
            "sheet", 8, 4, "8.10", {"type": "number"}, column_name="数量"
        )
        cell = request["updateRangeRequest"]["gridData"]["rows"][0]["values"][0]
        self.assertEqual(cell["cellValue"], {"number": 8.1})

    async def test_blank_result_options_reuse_cached_tencent_option_ids(self):
        parser = get_parser("全链条")
        cursor = ManagedMetadataCursor({
            "核查结果": {
                "type": "select",
                "options": [
                    {"id": "result-1", "text": "已登记"},
                    {"id": "result-2", "text": "无法核实"},
                ],
            },
        })

        metadata = await _managed_column_metadata(
            cursor,
            parser,
            {"核查结果": {
                "type": "select",
                "options": [{"id": "", "text": ""}],
            }},
        )

        self.assertEqual(
            [item["text"] for item in metadata["核查结果"]["options"]],
            ["已登记", "无法核实", "待登记", "移交", "无需登记", "离苏"],
        )
        self.assertEqual(
            metadata["核查结果"]["write_options"],
            [
                {"id": "result-1", "text": "已登记"},
                {"id": "result-2", "text": "无法核实"},
                {"id": "待登记", "text": "待登记"},
                {"id": "移交", "text": "移交"},
                {"id": "无需登记", "text": "无需登记"},
                {"id": "离苏", "text": "离苏"},
            ],
        )
        request = TxDocsClient("client", "token", "user").build_update_cell_request(
            "sheet", 8, 4, "待登记", metadata["核查结果"]
        )
        self.assertEqual(
            request["updateRangeRequest"]["gridData"]["rows"][0]["values"][0]
            ["cellValue"],
            {"text": "待登记"},
        )

    async def test_result_options_have_business_fallback_without_cached_metadata(self):
        parser = get_parser("疑似未注销模型三")

        metadata = await _managed_column_metadata(
            ManagedMetadataCursor(),
            parser,
            {"核查结果": {
                "type": "select",
                "options": [{"id": "", "text": ""}],
            }},
        )

        self.assertEqual(
            [item["text"] for item in metadata["核查结果"]["options"]],
            ["近期返吴", "离吴", "在吴"],
        )


    async def test_business_fallback_options_validate_select_text_writeback(self):
        parser = get_parser("\u5168\u94fe\u6761")
        result_field = "\u6838\u67e5\u7ed3\u679c"
        metadata = await _managed_column_metadata(
            ManagedMetadataCursor(),
            parser,
            {result_field: {
                "type": "select",
                "options": [{"id": "", "text": ""}],
            }},
        )

        self.assertEqual(
            metadata[result_field]["write_options"],
            metadata[result_field]["options"],
        )
        first_option = metadata[result_field]["options"][0]
        request = TxDocsClient("client", "token", "user").build_update_cell_request(
            "sheet", 8, 4, first_option["text"], metadata[result_field]
        )
        self.assertEqual(
            request["updateRangeRequest"]["gridData"]["rows"][0]["values"][0]
            ["cellValue"],
            {"text": first_option["text"]},
        )


if __name__ == "__main__":
    unittest.main()
