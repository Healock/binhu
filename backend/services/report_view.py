"""汇总表展示模式转换。

数据库和日报始终保留“未核查、已核查、已完成”三列原始口径。
两列模式只在输出时投影，网页和后续 XLSX 导出应共用本模块。
"""

from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal


ReportColumnMode = Literal["two", "three"]
LEGACY_DAILY_AVERAGE_COLUMN = "当日人均核查数"
DAILY_AVERAGE_COLUMN = "每日人均核查数"
SUM_COLUMNS = (
    "数据总数",
    "未核查",
    "已核查",
    "已完成",
    "无法见底数",
    "网格员人数",
    "在岗人日",
)


def _count(value) -> int:
    """把数据库返回的计数安全转换为整数。"""
    if value in (None, ""):
        return 0
    return int(value)


def _ratio(numerator: int, denominator: int) -> float:
    """按数据库相同的两位小数口径计算比率。"""
    if denominator <= 0:
        return 0.0
    value = (Decimal(numerator) / Decimal(denominator)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return float(value)


def _build_summary(table: dict) -> dict:
    """计算一张汇总表的总计，不把总计混入普通数据行。"""
    columns = list(table.get("columns") or [])
    rows = list(table.get("data") or [])
    summary = {}
    label_written = False

    for column in columns:
        if column in {"社区", "姓名"}:
            summary[column] = "总计" if not label_written else ""
            label_written = True
        elif column in SUM_COLUMNS:
            summary[column] = sum(_count(row.get(column)) for row in rows)

    total = summary.get("数据总数", 0)
    completed = summary.get("已完成", 0)
    unable = summary.get("无法见底数", 0)
    person_days = summary.get(
        "在岗人日",
        summary.get("网格员人数", 0),
    )

    if "核查完成率" in columns:
        summary["核查完成率"] = _ratio(completed, total)
    if "核查见底率" in columns:
        summary["核查见底率"] = _ratio(completed, completed + unable)
    if DAILY_AVERAGE_COLUMN in columns:
        if any(row.get(DAILY_AVERAGE_COLUMN) is None for row in rows):
            summary[DAILY_AVERAGE_COLUMN] = None
        else:
            workload = sum(
                Decimal(str(row.get(DAILY_AVERAGE_COLUMN) or 0))
                * Decimal(_count(row.get("在岗人日", row.get("网格员人数", 0))))
                for row in rows
            )
            summary[DAILY_AVERAGE_COLUMN] = _ratio(workload, person_days)

    return summary


def _with_summary(table: dict) -> dict:
    return {**table, "summary": _build_summary(table)}


def _rename_daily_average(table: dict) -> dict:
    """把旧数据库列名转换成稳定的页面名称，不修改持久化表。"""
    columns = list(table.get("columns") or [])
    if LEGACY_DAILY_AVERAGE_COLUMN not in columns:
        return table
    renamed_columns = []
    for column in columns:
        target = (
            DAILY_AVERAGE_COLUMN
            if column == LEGACY_DAILY_AVERAGE_COLUMN
            else column
        )
        if target not in renamed_columns:
            renamed_columns.append(target)

    renamed_rows = []
    for source_row in table.get("data") or []:
        row = dict(source_row)
        if LEGACY_DAILY_AVERAGE_COLUMN in row:
            row.setdefault(
                DAILY_AVERAGE_COLUMN,
                row[LEGACY_DAILY_AVERAGE_COLUMN],
            )
            row.pop(LEGACY_DAILY_AVERAGE_COLUMN, None)
        renamed_rows.append(row)
    return {**table, "columns": renamed_columns, "data": renamed_rows}


def _prepare_table(table: dict, mode: ReportColumnMode) -> dict:
    prepared = _with_summary(_rename_daily_average(table))
    if mode == "two":
        prepared = _project_table(prepared)
    return prepared


def _project_row(source_row: dict) -> dict:
    row = dict(source_row)
    row["未核查"] = _count(source_row.get("未核查")) + _count(
        source_row.get("已核查")
    )
    row["已核查"] = _count(source_row.get("已完成"))
    row.pop("已完成", None)
    return row


def _project_table(table: dict) -> dict:
    columns = list(table.get("columns") or [])
    rows = list(table.get("data") or [])
    required = {"未核查", "已核查", "已完成"}
    if not required.issubset(columns):
        return deepcopy(table)

    projected = {
        **table,
        "columns": [column for column in columns if column != "已完成"],
        "data": [_project_row(row) for row in rows],
    }
    if isinstance(table.get("summary"), dict):
        projected["summary"] = _project_row(table["summary"])
    return projected


def project_report_payload(payload: dict, mode: ReportColumnMode) -> dict:
    """按指定模式转换一份统计响应，不修改传入对象。"""
    result = deepcopy(payload)
    result["column_mode"] = mode
    if not result.get("exists"):
        return result

    for section in ("inspector", "community"):
        if isinstance(result.get(section), dict):
            result[section] = _prepare_table(result[section], mode)

    if "columns" in result and "data" in result:
        flat_table = _prepare_table(
            {
                "columns": result["columns"],
                "data": result["data"],
            },
            mode,
        )
        for key in ("columns", "data", "summary"):
            result[key] = flat_table[key]

    return result
