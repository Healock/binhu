from __future__ import annotations

import io
import os
from datetime import date

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

import pytest
from openpyxl import Workbook

from services.police_dispatch import PoliceWorkbookError
from services.police_import_profiles import parse_profile


def workbook_bytes(rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "已处理"
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


COMMUNITIES = [{"id": 1, "name": "长板社区", "enabled": True}]
BUSINESS_DATE = date(2026, 8, 25)


def test_internal_police_uses_case_number_and_does_not_copy_summary_rows():
    result = parse_profile(
        "police_internal_processed",
        workbook_bytes([
            ["接警编号", "社区", "简要警情及处理结果", "未登记", "已登记", "他所"],
            ["12345678901234567890", "长板社区", "虚构警情", 3, 4, 5],
            ["", "长板社区", "缺少接警编号", 1, 0, 0],
        ]),
        "internal.xlsx",
        BUSINESS_DATE,
        COMMUNITIES,
    )

    assert result["counts"]["total"] == 2
    assert result["counts"]["missing_key"] == 1
    assert result["rows"][0]["standard_values"]["序号"] == "12345678901234567890"
    assert "未登记" not in result["rows"][0]["standard_values"]
    assert result["rows"][0]["validation_issues"] == []


def test_suzhou_police_supports_case_number_and_short_dates():
    result = parse_profile(
        "police_suzhou_processed",
        workbook_bytes([
            ["任务有效期", "身份证号码", "联系号码", "姓名", "社区", "疑似现住址", "接警编号"],
            ["8.24", "32050020000101001X", "13800000000", "虚构人员", "长板社区", "虚构地址", "CASE-001"],
        ]),
        "suzhou.xlsx",
        BUSINESS_DATE,
        COMMUNITIES,
    )

    row = result["rows"][0]
    assert result["counts"]["importable"] == 1
    assert row["standard_values"]["截止日期"] == "2026-08-24"
    assert row["standard_values"]["接警编号"] == "CASE-001"
    assert row["business_key_hmac"]


@pytest.mark.parametrize(
    ("profile", "headers", "values", "expected"),
    [
        (
            "delivery_processed",
            ["身份证号码", "手机号码", "参考姓名", "姓名", "所属社区"],
            ["32050020000101001X", "13800000000", "参考人员", "虚构人员", "长板社区"],
            ("参考姓名", "参考人员"),
        ),
        (
            "suspect_return_processed",
            ["身份证号码", "联系号码", "高频抓拍小区", "姓名", "业务分类"],
            ["32050020000101001X", "13800000000", "长板社区", "虚构人员", "长板社区"],
            ("高频抓拍小区", "长板社区"),
        ),
    ],
)
def test_other_business_adapters_map_standard_fields(profile, headers, values, expected):
    result = parse_profile(profile, workbook_bytes([headers, values]), f"{profile}.xlsx", BUSINESS_DATE, COMMUNITIES)
    row = result["rows"][0]
    assert result["counts"]["importable"] == 1
    assert row["standard_values"][expected[0]] == expected[1]
    assert row["business_key_hmac"]


def test_rental_entry_is_explicitly_disabled():
    with pytest.raises(PoliceWorkbookError, match="等待真实已处理文件样本"):
        parse_profile(
            "rental_processed",
            workbook_bytes([["姓名"], ["虚构人员"]]),
            "rental.xlsx",
            BUSINESS_DATE,
            COMMUNITIES,
        )


def test_unknown_header_is_rejected_instead_of_guessing_business_type():
    with pytest.raises(PoliceWorkbookError, match="表头与所选业务类型不匹配"):
        parse_profile(
            "police_suzhou_processed",
            workbook_bytes([["随机列", "另一列"], ["a", "b"]]),
            "wrong.xlsx",
            BUSINESS_DATE,
            COMMUNITIES,
        )
