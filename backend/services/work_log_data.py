"""工作日志日报的系统数据快照。"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from services.report_view import project_report_payload
from services.stats_calculator import DailyReportBuilder
from services.visit_summary import (
    VISIT_CATEGORY_RENTAL,
    VISIT_CATEGORY_SELF_OWNED,
    get_visit_summary,
)


MODEL_THREE_TYPE = "疑似未注销模型三"
_builder = DailyReportBuilder()


def _number(value: Any) -> int:
    return int(value or 0)


def _decimal(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _percent(value: Any) -> float:
    return float(
        (Decimal(str(value or 0)) * Decimal(100)).quantize(
            Decimal("0.1"),
            rounding=ROUND_HALF_UP,
        )
    )


def _format_ranking(
    rows: list[dict],
    *,
    metric: str,
    denominator: str,
    percent: bool = False,
) -> str:
    active = [
        row
        for row in rows
        if _number(row.get(denominator)) > 0
    ]
    active.sort(
        key=lambda row: (
            -(float(row.get(metric) or 0)),
            str(row.get("社区") or ""),
        )
    )
    ranked = []
    for index, row in enumerate(active, start=1):
        raw_value = float(row.get(metric) or 0)
        value = f"{_percent(raw_value):g}%" if percent else f"{raw_value:g}"
        ranked.append(f"{index}. {row.get('社区') or '未分配社区'} {value}")
    inactive = sorted(
        str(row.get("社区") or "未分配社区")
        for row in rows
        if _number(row.get(denominator)) <= 0
    )
    if inactive:
        ranked.append(f"未开展：{'、'.join(inactive)}")
    return "；".join(ranked) or "暂无开展社区"


async def _model_three_snapshot(business_date: date) -> dict:
    result = await _builder.get_report(
        business_date.isoformat(),
        MODEL_THREE_TYPE,
    )
    if not result.get("exists"):
        return {
            "available": False,
            "message": result.get("message") or "该日期没有可用数据",
            "values": {},
        }
    projected = project_report_payload(result, "two")
    table = projected.get("community") or {}
    rows = table.get("data") or []
    summary = table.get("summary") or {}
    return {
        "available": True,
        "message": "",
        "values": {
            "priority.model3_total": _number(summary.get("数据总数")),
            "priority.model3_unchecked": _number(summary.get("未核查")),
            "priority.model3_checked": _number(summary.get("已核查")),
            "priority.model3_completion_rate": _percent(
                summary.get("核查完成率")
            ),
            "priority.model3_unable": _number(summary.get("无法见底数")),
            "priority.model3_ground_rate": _percent(
                summary.get("核查见底率")
            ),
            "priority.model3_ranking": (
                "完成率："
                + _format_ranking(
                    rows,
                    metric="核查完成率",
                    denominator="数据总数",
                    percent=True,
                )
                + "\n见底率："
                + _format_ranking(
                    rows,
                    metric="核查见底率",
                    denominator="已核查",
                    percent=True,
                )
            ),
        },
    }


def _visit_values(prefix: str, result: dict) -> dict:
    overview = result["overview"]
    community = result["community"]
    total = community["summary"]
    return {
        f"{prefix}.visits": _number(overview.get("visit_records")),
        f"{prefix}.added": _number(overview.get("added_count")),
        f"{prefix}.changed": _number(overview.get("changed_count")),
        f"{prefix}.cancelled": _number(overview.get("cancelled_count")),
        f"{prefix}.total_changes": _number(overview.get("total_changes")),
        f"{prefix}.person_avg_visits": _decimal(
            total.get("人均日走访户数")
        ),
        f"{prefix}.person_avg_changes": _decimal(
            total.get("人均日变动数")
        ),
        f"{prefix}.household_avg_changes": _decimal(
            total.get("户均变动数")
        ),
        f"{prefix}.rated": _number(overview.get("rated_records")),
        f"{prefix}.rating_rate": _percent(overview.get("rating_rate")),
        f"{prefix}.ranking": _format_ranking(
            community.get("data") or [],
            metric="走访户数",
            denominator="走访户数",
        ),
    }


async def _visit_snapshots(conn, business_date: date) -> dict:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM t_visit_details WHERE `业务日期`=%s",
            (business_date,),
        )
        row = await cur.fetchone()
    if not row or _number(row[0]) == 0:
        unavailable = {
            "available": False,
            "message": "该日期没有可用数据",
            "values": {},
        }
        return {
            "rental": unavailable,
            "self_owned": dict(unavailable),
        }

    rental = await get_visit_summary(
        conn,
        business_date,
        business_date,
        category=VISIT_CATEGORY_RENTAL,
    )
    self_owned = await get_visit_summary(
        conn,
        business_date,
        business_date,
        category=VISIT_CATEGORY_SELF_OWNED,
    )
    result = {}
    for key, prefix, payload in (
        ("rental", "rental", rental),
        ("self_owned", "self_owned", self_owned),
    ):
        attendance = payload.get("attendance") or {}
        message = ""
        if not attendance.get("complete", True):
            message = "该日期出勤资料不完整，人均值无法自动计算"
        result[key] = {
            "available": True,
            "message": message,
            "values": _visit_values(prefix, payload),
        }
    return result


async def build_system_snapshot(conn, business_date: date) -> dict:
    """读取一次系统数据，返回可长期保存的普通 JSON 对象。"""
    model_three = await _model_three_snapshot(business_date)
    visits = await _visit_snapshots(conn, business_date)
    values = {
        **model_three["values"],
        **visits["rental"]["values"],
        **visits["self_owned"]["values"],
    }
    issue_date = business_date + timedelta(days=1)
    sources = {
        "model_three": {
            "label": "疑似未注销模型三",
            "available": model_three["available"],
            "message": model_three["message"],
        },
        "rental": {
            "label": "出租房走访",
            "available": visits["rental"]["available"],
            "message": visits["rental"]["message"],
        },
        "self_owned": {
            "label": "自购房走访",
            "available": visits["self_owned"]["available"],
            "message": visits["self_owned"]["message"],
        },
    }
    return {
        "business_date": business_date.isoformat(),
        "issue_date": issue_date.isoformat(),
        "month": business_date.month,
        "filename_prefix": business_date.strftime("%m%d"),
        "values": values,
        "sources": sources,
    }
