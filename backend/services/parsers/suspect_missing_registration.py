"""疑似漏登记 - 在线数据查询解析器。"""

from __future__ import annotations

import re

from .base import BaseParser


_MOBILE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def extract_mobile_numbers(value: object) -> str:
    """只保留联系方式中的手机号，不保存时间戳或其他自由文本。"""
    matches = _MOBILE_RE.findall(str(value or ""))
    return "、".join(dict.fromkeys(matches))


class SuspectMissingRegistrationParser(BaseParser):
    parser_type = "疑似漏登记"
    table_name = "t_suspect_missing_registration"
    COLUMNS = [
        "下发日期", "截止日期", "社区", "姓名", "身份证号",
        "联系方式", "地址", "核查人", "登记情况", "现住址",
        "核查结果", "备注", "研判", "二次反馈",
    ]
    # G 列是业务确认的原始地址列；保留一个常见的“核查人靠前”布局，
    # 由表头匹配选择，不把物理列号写入业务键或本地来源。
    SOURCE_COLUMN_LAYOUTS = (
        tuple(COLUMNS),
        (
            "下发日期", "截止日期", "核查人", "社区", "姓名",
            "身份证号", "联系方式", "地址", "登记情况", "现住址",
            "核查结果", "备注", "研判", "二次反馈",
        ),
        (
            "下发日期", "截止日期", "社区", "姓名", "身份证号",
            "联系方式", "地址，小区名", "核查人", "登记情况", "现住址",
            "核查结果", "备注", "研判", "二次反馈",
        ),
    )
    MOBILE_EDITABLE_FIELDS = ("登记情况", "现住址", "核查结果", "备注", "研判", "二次反馈")

    def get_business_key(self) -> list[str]:
        return ["身份证号", "下发日期"]

    def normalize_source_row(self, row: dict) -> dict[str, str]:
        normalized = super().normalize_source_row(row)
        # 只从联系方式中提取手机号；即使来源单元格包含“时间 + 手机号”
        # 或其他说明文字，也不会进入 values_json、业务表或来源记录。
        normalized["地址"] = str(
            row.get("地址") or row.get("地址，小区名") or ""
        ).strip()
        normalized["联系方式"] = extract_mobile_numbers(
            row.get("联系方式", row.get("手机号", row.get("手机号码", "")))
        )
        return normalized
