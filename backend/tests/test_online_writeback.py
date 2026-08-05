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
    _managed_column_metadata,
    _row_values_match,
    new_row_required_fields,
    update_source_cell,
    update_source_fields,
)
from services.online_edit_permissions import (
    can_manage_rows,
    editable_fields_for_row,
    effective_edit_communities,
    effective_view_communities,
    validate_row_change,
    validate_row_changes,
)
from services.online_source import (
    cleanup_expired_writeback_audit,
    rebuild_projection,
    source_row_hash,
)
from services.parsers import get_parser
from services.permissions import (
    ONLINE_RAW_EDIT,
    ONLINE_RAW_ROW_MANAGE,
    ONLINE_RAW_VIEW,
)
from services.txdocs_client import TxDocsClient


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
        if compact.startswith("SELECT row_key, values_json"):
            self.mode = "sources"
        elif compact.startswith("SELECT row_key_before"):
            self.mode = "pending"
        else:
            self.mode = "write"

    async def fetchall(self):
        if self.mode == "sources":
            return list(self.source_rows)
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
        columns = ["核查人", "核查结果", "研判", "二次反馈"]

        unlocked = editable_fields_for_row(user, columns, {"核查结果": "无法核实"})
        locked = editable_fields_for_row(user, columns, {"核查结果": "移交"})

        self.assertIn("二次反馈", unlocked)
        self.assertNotIn("二次反馈", locked)
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
        self.assertEqual(projection[5], "completed")
        self.assertEqual(projection[6], 2)
        self.assertEqual(projection[7], 0)
        self.assertEqual(projection[9], "pending")
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

        self.assertEqual(cursor.many_rows[0][6], 2)
        self.assertEqual(cursor.many_rows[0][7], 1)

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

        self.assertEqual(metadata["核查结果"]["options"], [
            {"id": "result-1", "text": "已登记"},
            {"id": "result-2", "text": "无法核实"},
        ])

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
            ["近期反吴", "离吴", "在吴"],
        )


if __name__ == "__main__":
    unittest.main()
