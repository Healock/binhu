"""汇总表展示模式转换。

数据库和日报始终保留“未核查、已核查、已完成”三列原始口径。
两列模式只在输出时投影，网页和后续 XLSX 导出应共用本模块。
"""

from copy import deepcopy
from typing import Literal


ReportColumnMode = Literal["two", "three"]


def _count(value) -> int:
    """把数据库返回的计数安全转换为整数。"""
    if value in (None, ""):
        return 0
    return int(value)


def _project_table(table: dict) -> dict:
    columns = list(table.get("columns") or [])
    rows = list(table.get("data") or [])
    required = {"未核查", "已核查", "已完成"}
    if not required.issubset(columns):
        return deepcopy(table)

    projected_rows = []
    for source_row in rows:
        row = dict(source_row)
        row["未核查"] = _count(source_row.get("未核查")) + _count(
            source_row.get("已核查")
        )
        row["已核查"] = _count(source_row.get("已完成"))
        row.pop("已完成", None)
        projected_rows.append(row)

    return {
        **table,
        "columns": [column for column in columns if column != "已完成"],
        "data": projected_rows,
    }


def project_report_payload(payload: dict, mode: ReportColumnMode) -> dict:
    """按指定模式转换一份统计响应，不修改传入对象。"""
    result = deepcopy(payload)
    result["column_mode"] = mode
    if mode == "three" or not result.get("exists"):
        return result

    if "columns" in result and "data" in result:
        projected = _project_table(result)
        projected["column_mode"] = mode
        return projected

    for section in ("inspector", "community"):
        if isinstance(result.get(section), dict):
            result[section] = _project_table(result[section])
    return result
