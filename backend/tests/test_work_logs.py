import json
import os
from datetime import date, datetime
from io import BytesIO
from unittest.mock import AsyncMock, patch
import unittest
from zipfile import ZipFile

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from openpyxl import load_workbook

from fastapi import HTTPException

from routers.work_logs import (
    DraftCreate,
    DraftSave,
    _draft_payload,
    _missing_items,
    create_draft,
    router,
    save_draft,
)
from services.work_log_data import _model_three_snapshot, build_system_snapshot
from services.work_log_document import build_daily_document
from services.work_log_schema import (
    default_manual_values,
    get_schema,
    sanitize_values,
)


class CountCursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        self.params = params

    async def fetchone(self):
        return (0,)


class CountConnection:
    def cursor(self):
        return CountCursor()


class DraftCursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 0
        self.lastrowid = 1
        self.result = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.rowcount = 0
        if normalized.startswith("SELECT"):
            self.result = self.connection.row
            return
        if normalized.startswith("UPDATE _work_log_drafts SET manual_values"):
            owner_id = params[4]
            version = params[5]
            if (
                self.connection.row
                and self.connection.row[3] == owner_id
                and self.connection.row[9] == version
            ):
                row = list(self.connection.row)
                row[7] = params[0]
                row[8] = params[1]
                row[9] += 1
                self.connection.row = tuple(row)
                self.rowcount = 1

    async def fetchone(self):
        return self.result


class DraftConnection:
    def __init__(self, row):
        self.row = row

    def cursor(self):
        return DraftCursor(self)


def draft_payload(snapshot=None):
    snapshot = snapshot or {
        "business_date": "2026-07-14",
        "issue_date": "2026-07-15",
        "month": 7,
        "filename_prefix": "0714",
        "values": {},
        "sources": {
            "model_three": {
                "label": "疑似未注销模型三",
                "available": False,
                "message": "该日期没有可用数据",
            },
            "rental": {
                "label": "出租房走访",
                "available": False,
                "message": "该日期没有可用数据",
            },
            "self_owned": {
                "label": "自购房走访",
                "available": False,
                "message": "该日期没有可用数据",
            },
        },
    }
    return {
        "id": 1,
        "report_type": "daily",
        "business_date": "2026-07-14",
        "owner": {"id": 7, "username": "admin"},
        "can_edit": True,
        "template_version": "daily-v1",
        "system_snapshot": snapshot,
        "manual_values": default_manual_values(),
        "override_values": {},
        "version": 1,
        "last_export_at": None,
        "created_at": "2026-07-14T00:00:00",
        "updated_at": "2026-07-14T00:00:00",
    }


class WorkLogTests(unittest.IsolatedAsyncioTestCase):
    def sample_row(self):
        return (
            4, "daily", date(2026, 7, 14), 7, "admin", "daily-v1",
            json.dumps(draft_payload()["system_snapshot"], ensure_ascii=False),
            json.dumps(default_manual_values(), ensure_ascii=False),
            "{}", 1, None,
            datetime(2026, 7, 14), datetime(2026, 7, 14),
        )

    async def test_model_three_uses_fixed_two_column_projection(self):
        report = {
            "exists": True,
            "community": {
                "columns": [
                    "社区", "数据总数", "未核查", "已核查", "已完成",
                    "核查完成率", "无法见底数", "核查见底率",
                ],
                "data": [{
                    "社区": "长板",
                    "数据总数": 10,
                    "未核查": 2,
                    "已核查": 3,
                    "已完成": 5,
                    "核查完成率": 0.5,
                    "无法见底数": 1,
                    "核查见底率": 0.8,
                }],
            },
        }
        with patch(
            "services.work_log_data._builder.get_report",
            new=AsyncMock(return_value=report),
        ):
            result = await _model_three_snapshot(date(2026, 7, 14))

        self.assertTrue(result["available"])
        self.assertEqual(result["values"]["priority.model3_total"], 10)
        self.assertEqual(result["values"]["priority.model3_unchecked"], 5)
        self.assertEqual(result["values"]["priority.model3_checked"], 5)
        self.assertEqual(
            result["values"]["priority.model3_completion_rate"],
            50.0,
        )
        self.assertEqual(
            result["values"]["priority.model3_ground_rate"],
            80.0,
        )

    async def test_snapshot_dates_and_missing_source_are_explicit(self):
        with patch(
            "services.work_log_data._model_three_snapshot",
            new=AsyncMock(return_value={
                "available": False,
                "message": "该日期没有可用数据",
                "values": {},
            }),
        ):
            result = await build_system_snapshot(
                CountConnection(),
                date(2026, 7, 14),
            )

        self.assertEqual(result["business_date"], "2026-07-14")
        self.assertEqual(result["issue_date"], "2026-07-15")
        self.assertEqual(result["filename_prefix"], "0714")
        self.assertFalse(result["sources"]["rental"]["available"])
        self.assertNotIn("rental.visits", result["values"])

    def test_schema_sanitizes_unknown_and_wrong_source_fields(self):
        manual = sanitize_values(
            {
                "basic.total_population": 100,
                "rental.visits": 9,
                "unknown": "secret",
            },
            source="manual",
        )
        overrides = sanitize_values(
            {
                "rental.visits": 9,
                "basic.total_population": 100,
            },
            source="system",
        )
        self.assertEqual(manual, {"basic.total_population": 100.0})
        self.assertEqual(overrides, {"rental.visits": 9.0})

    def test_draft_row_reports_creator_editing_rights(self):
        row = list(self.sample_row())
        row[9] = 3
        row = tuple(row)
        own = _draft_payload(row, {"id": 7})
        other = _draft_payload(row, {"id": 8})
        self.assertTrue(own["can_edit"])
        self.assertFalse(other["can_edit"])

    async def test_existing_date_returns_same_draft_without_refreshing_data(self):
        connection = DraftConnection(self.sample_row())
        request = type(
            "Request",
            (),
            {"headers": {}, "client": None},
        )()
        with patch(
            "routers.work_logs.build_system_snapshot",
            new=AsyncMock(),
        ) as snapshot:
            result = await create_draft(
                DraftCreate(
                    report_type="daily",
                    business_date=date(2026, 7, 14),
                ),
                request=request,
                user={"id": 8, "username": "other"},
                conn=connection,
            )
        self.assertEqual(result["id"], 4)
        self.assertFalse(result["can_edit"])
        snapshot.assert_not_awaited()

    async def test_autosave_increments_version_and_rejects_stale_version(self):
        connection = DraftConnection(self.sample_row())
        saved = await save_draft(
            4,
            DraftSave(
                version=1,
                manual_values={"basic.total_population": 120},
                override_values={},
            ),
            user={"id": 7},
            conn=connection,
        )
        self.assertEqual(saved["version"], 2)
        self.assertEqual(
            saved["manual_values"]["basic.total_population"],
            120.0,
        )
        with self.assertRaises(HTTPException) as raised:
            await save_draft(
                4,
                DraftSave(
                    version=1,
                    manual_values={"basic.total_population": 121},
                    override_values={},
                ),
                user={"id": 7},
                conn=connection,
            )
        self.assertEqual(raised.exception.status_code, 409)

    def test_missing_items_explain_unavailable_system_data(self):
        missing = _missing_items(draft_payload())
        model_three = next(
            item for item in missing
            if item["field_id"] == "priority.model3_total"
        )
        self.assertEqual(model_three["reason"], "该日期没有可用数据")

    def test_every_work_log_route_requires_admin(self):
        for route in router.routes:
            dependency_names = {
                dependency.call.__name__
                for dependency in route.dependant.dependencies
                if dependency.call
            }
            self.assertIn(
                "require_admin",
                dependency_names,
                route.path,
            )

    def test_docx_has_twelve_embedded_xlsx_and_no_placeholders(self):
        draft = draft_payload()
        draft["manual_values"]["basic.total_population"] = 123
        values = {
            **draft["system_snapshot"]["values"],
            **draft["manual_values"],
        }
        content, filename = build_daily_document(
            draft,
            get_schema(),
            values,
        )
        self.assertEqual(
            filename,
            "0714日报滨湖新城派出所社区警务工作日志.docx",
        )
        with ZipFile(BytesIO(content)) as package:
            names = package.namelist()
            embeddings = sorted(
                name for name in names
                if name.startswith("word/embeddings/")
                and name.endswith(".xlsx")
            )
            previews = [
                name for name in names
                if name.startswith("word/media/")
            ]
            document_xml = package.read("word/document.xml")
            self.assertEqual(len(embeddings), 12)
            self.assertEqual(len(previews), 12)
            self.assertEqual(document_xml.count(b"OLEObject"), 12)
            self.assertNotIn(b"{{", document_xml)
            workbook = load_workbook(
                BytesIO(package.read(embeddings[0])),
                data_only=True,
            )
            self.assertEqual(workbook.active["B3"].value, "123")


if __name__ == "__main__":
    unittest.main()
