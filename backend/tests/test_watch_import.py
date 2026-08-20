from __future__ import annotations

from io import BytesIO

from openpyxl import Workbook

from services.watch_import import parse_watch_workbook, summarize_watch_rows, valid_identity


VALID_IDENTITY = "11010519491231002X"
OTHER_IDENTITY = "110105194912310038"


def _xlsx(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_parser_supports_headered_workbook_and_normalizes_dates():
    content = _xlsx([
        ["姓名", "身份证号码", "手机号", "派出所", "核查时间(非必填)", "备注"],
        ["甲", VALID_IDENTITY.lower(), "13800138000", "滨湖新城派出所", "2026/8/1 09:30", "通勤"],
    ])
    rows = parse_watch_workbook(content, "header.xlsx", category_code="通勤人员")
    assert len(rows) == 1
    assert rows[0].identity_number == VALID_IDENTITY
    assert rows[0].occurred_at == "2026-08-01 09:30:00"
    assert rows[0].organization == "滨湖新城派出所"
    assert rows[0].blocking_issue == ""


def test_parser_supports_two_column_no_header_workbook():
    content = _xlsx([["乙", OTHER_IDENTITY], ["丙", VALID_IDENTITY]])
    rows = parse_watch_workbook(content, "two-columns.xlsx", category_code="通勤人员")
    assert [row.name for row in rows] == ["乙", "丙"]
    assert all(row.blocking_issue == "" for row in rows)


def test_summary_deduplicates_people_but_keeps_conflict_as_blocking():
    content = _xlsx([
        ["姓名", "身份证", "手机号"],
        ["甲", VALID_IDENTITY, "13800138000"],
        ["甲", VALID_IDENTITY, "13900139000"],
        ["乙", OTHER_IDENTITY, ""],
    ])
    summary = summarize_watch_rows(parse_watch_workbook(content, "duplicate.xlsx", category_code="通勤人员"))
    assert summary["total_rows"] == 3
    assert summary["unique_people"] == 2
    assert summary["duplicate_rows"] == 1
    assert summary["phone_conflict_groups"] == 1
    assert summary["blocking_count"] == 1
    assert summary["can_confirm"] is False


def test_invalid_identity_is_classified_without_guessing():
    content = _xlsx([
        ["姓名", "身份证"],
        ["甲", "11010519491331002X"],
        ["乙", ""],
    ])
    rows = parse_watch_workbook(content, "invalid.xlsx", category_code="通勤人员")
    summary = summarize_watch_rows(rows)
    assert valid_identity(VALID_IDENTITY)
    assert not valid_identity("11010519491331002Z")
    assert summary["missing_identity_count"] == 1
    assert summary["invalid_identity_count"] == 1
    assert summary["blocking_count"] == 2


def test_summary_shape_does_not_log_or_return_raw_rows():
    content = _xlsx([["姓名", "身份证"], ["甲", VALID_IDENTITY]])
    summary = summarize_watch_rows(parse_watch_workbook(content, "safe.xlsx", category_code="通勤人员"))
    safe = {key: value for key, value in summary.items() if key != "people"}
    assert safe["unique_people"] == 1
    assert "姓名" not in str(safe)
    assert VALID_IDENTITY not in str(safe)
