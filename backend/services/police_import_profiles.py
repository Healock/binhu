"""已处理业务文件的显式适配器注册中心。

适配器只在命中明确表头指纹后解析；位置回退仅用于指纹已确认、但个别列
没有标题的历史文件，避免把任意 Excel 错认成业务数据。
"""

from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from typing import Any, Callable

from config import settings
from services.police_dispatch import (
    IDENTITY_PATTERN,
    PoliceWorkbookError,
    business_key_hmac,
    community_resolver,
    normalize_identity,
    normalize_space,
    read_workbook_rows,
    resolve_community,
    stable_json,
)


ADAPTER_VERSION = "2026-08-25.2"


@dataclass(frozen=True, slots=True)
class ImportProfile:
    key: str
    business_type: str
    label: str
    police_subtype: str
    target_parser: str
    enabled: bool
    description: str
    example_fields: tuple[str, ...]
    parse: Callable[[bytes, str, date, list[dict[str, Any]]], dict[str, Any]] | None


def _header(value: Any) -> str:
    return re.sub(r"[\s：:]", "", str(value or "")).lower()


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return normalize_space(value)


def _date_value(value: Any, business_date: date) -> str:
    text = _cell(value)
    if not text:
        return ""
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text[:10], pattern).date().isoformat()
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})", text)
    if match:
        month, day = map(int, match.groups())
        try:
            return date(business_date.year, month, day).isoformat()
        except ValueError:
            return text
    return text


def _find_sheet(
    content: bytes,
    filename: str,
    required_headers: tuple[tuple[str, ...], ...],
) -> tuple[str, list[list[str]], int, dict[str, int]]:
    best = None
    for sheet_name, rows in read_workbook_rows(content, filename):
        for row_index, row in enumerate(rows[:40]):
            normalized = [_header(value) for value in row]
            mapping: dict[str, int] = {}
            matched = True
            for aliases in required_headers:
                index = next((i for i, value in enumerate(normalized) if value in {_header(a) for a in aliases}), None)
                if index is None:
                    matched = False
                    break
                mapping[aliases[0]] = index
            if matched and (best is None or len(mapping) > len(best[3])):
                best = (sheet_name, rows, row_index, mapping)
    if best is None:
        raise PoliceWorkbookError("文件表头与所选业务类型不匹配")
    return best


def _raw_values(headers: list[str], row: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, value in enumerate(row):
        name = normalize_space(headers[index] if index < len(headers) else "") or f"第{index + 1}列"
        if name in values:
            name = f"{name}_{index + 1}"
        values[name] = _cell(value)
    return values


def _mask(value: str, leading: int, trailing: int) -> str:
    text = normalize_space(value)
    if len(text) <= leading + trailing:
        return "*" * len(text)
    return f"{text[:leading]}{'*' * (len(text) - leading - trailing)}{text[-trailing:]}"


def _finalize(
    *,
    sheet_name: str,
    rows: list[dict[str, Any]],
    communities: list[dict[str, Any]],
    business_key_fields: tuple[str, ...],
) -> dict[str, Any]:
    resolver = community_resolver(item for item in communities if item.get("enabled", True))
    seen: dict[str, int] = {}
    counts = {
        "total": len(rows), "importable": 0, "missing_key": 0, "duplicate": 0,
        "identity_invalid": 0, "community_invalid": 0, "conflict": 0,
    }
    distribution: dict[int, dict[str, Any]] = {}
    for item in rows:
        issues: list[dict[str, str]] = list(item.pop("validation_issues", []))
        counts["conflict"] += sum(issue.get("type") == "date_conflict" for issue in issues)
        row_key_fields = tuple(item.pop("business_key_fields", business_key_fields))
        key_values = [
            str(item["standard_values"].get(field, "") or "")
            for field in row_key_fields
        ]
        if not all(normalize_space(value) for value in key_values):
            issues.append({"field": "业务主键", "type": "missing_key", "value": "缺少必要主键字段"})
            counts["missing_key"] += 1
            business_hmac = ""
        else:
            business_hmac = business_key_hmac(key_values, row_key_fields)
            if business_hmac in seen:
                issues.append({"field": "业务主键", "type": "duplicate", "value": f"与第 {seen[business_hmac]} 行重复"})
                counts["duplicate"] += 1
            else:
                seen[business_hmac] = int(item["source_row"])
        identity = normalize_identity(item.get("identity_number", ""))
        if identity and not IDENTITY_PATTERN.fullmatch(identity):
            issues.append({"field": "身份证号", "type": "identity_invalid", "value": _mask(identity, 6, 4)})
            counts["identity_invalid"] += 1
        community = resolve_community(item.get("community_name", ""), resolver)
        if not community:
            issues.append({"field": "社区", "type": "community_invalid", "value": normalize_space(item.get("community_name", ""))[:80]})
            counts["community_invalid"] += 1
        item["business_key_hmac"] = business_hmac
        item["validation_issues"] = issues
        item["suggested_action"] = "dispatch" if not issues else "manual"
        item["suggested_community_id"] = int(community["id"]) if community else None
        item["suggestion_reason"] = "格式校验通过，等待用户审核后发布" if not issues else "；".join(issue["value"] for issue in issues)
        item["allocation_mode"] = "matched" if not issues else "conflict"
        if not issues:
            counts["importable"] += 1
            cid = int(community["id"])
            distribution.setdefault(cid, {"community_id": cid, "community_name": str(community["name"]), "count": 0})["count"] += 1
    preview_rows = [{
        "source_row": item["source_row"],
        "person_name": item.get("person_name", ""),
        "identity_number": _mask(item.get("identity_number", ""), 6, 4),
        "phone": _mask(item.get("phone", ""), 3, 4),
        "community_name": item.get("community_name", ""),
        "business_key": _mask(
            " / ".join(
                str(item["standard_values"].get(field, ""))
                for field in tuple(item.get("business_key_fields") or business_key_fields)
            ),
            3,
            3,
        ),
        "result": "importable" if not item["validation_issues"] else "problem",
        "issues": item["validation_issues"],
    } for item in rows[:100]]
    return {
        "sheet_name": sheet_name,
        "rows": rows,
        "counts": counts,
        "community_distribution": list(distribution.values()),
        "preview_rows": preview_rows,
        "rows_truncated": len(rows) > 100,
    }


def _parse_rental(content: bytes, filename: str, business_date: date, communities: list[dict[str, Any]]) -> dict[str, Any]:
    sheet, rows, header_index, columns = _find_sheet(
        content,
        filename,
        (
            ("社区", "所属社区", "业务分类"),
            ("承租人姓名", "姓名"),
            ("承租人身份证号码", "身份证号码", "身份证号"),
            ("承租人联系号码", "联系号码", "手机号码", "手机号"),
            ("租赁房屋地址", "房屋地址", "地址"),
        ),
    )
    headers = rows[header_index]
    result = []
    for number, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        def value(column: str) -> str:
            target = columns.get(column)
            return _cell(row[target]) if target is not None and target < len(row) else ""

        identity = normalize_identity(value("承租人身份证号码"))
        phone = value("承租人联系号码")
        name = value("承租人姓名")
        address = value("租赁房屋地址")
        if not any((identity, phone, name, address)):
            continue

        first_date = _date_value(_cell(row[0]) if row else "", business_date)
        second_date = _date_value(_cell(row[1]) if len(row) > 1 else "", business_date)
        issues: list[dict[str, str]] = []
        dates_conflict = bool(first_date and second_date and first_date != second_date)
        if dates_conflict:
            issues.append({
                "field": "业务日期",
                "type": "date_conflict",
                "value": "前两列日期不一致，已使用上传时确认的业务日期，请人工核对",
            })
        if not name:
            issues.append({"field": "承租人姓名", "type": "missing_required", "value": "缺少承租人姓名"})
        if not address:
            issues.append({"field": "租赁房屋地址", "type": "missing_required", "value": "缺少租赁房屋地址"})

        dispatch_date = business_date.isoformat() if dates_conflict else (first_date or second_date or business_date.isoformat())
        deadline = business_date.isoformat() if dates_conflict else (second_date or first_date or business_date.isoformat())
        community = value("社区")
        values = {
            "下发时间": dispatch_date,
            "截止时间": deadline,
            "核查人": "",
            "社区": community,
            "姓名": name,
            "身份证号": identity,
            "手机号码": phone,
            "房屋地址": address,
            "现住址": "",
            "核查结果": "",
            "入住方式": "",
            "研判": "",
            "二次反馈": "",
        }
        result.append({
            "source_row": number,
            "source_name": "出租房屋核查",
            "community_name": community,
            "person_name": name,
            "identity_number": identity,
            "phone": phone,
            "original_address": address,
            "created_time": dispatch_date,
            "transfer_note": "",
            "raw_values": _raw_values(headers, row),
            "standard_values": values,
            "validation_issues": issues,
        })
    if not result:
        raise PoliceWorkbookError("出租房屋核查文件中没有可导入的数据")
    return _finalize(
        sheet_name=sheet,
        rows=result,
        communities=communities,
        business_key_fields=("身份证号", "手机号码"),
    )


def _parse_internal(content: bytes, filename: str, business_date: date, communities: list[dict[str, Any]]) -> dict[str, Any]:
    sheet, rows, header_index, columns = _find_sheet(content, filename, (("接警编号",), ("简要警情及处理结果",)))
    headers = rows[header_index]
    result = []
    for number, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        if not any(_cell(value) for value in row):
            continue
        case_no = _cell(row[columns["接警编号"]]) if columns["接警编号"] < len(row) else ""
        detail = _cell(row[columns["简要警情及处理结果"]]) if columns["简要警情及处理结果"] < len(row) else ""
        community = _cell(row[1]) if len(row) > 1 else ""  # 指纹命中后允许空表头位置回退。
        if not any((case_no, community, detail)):
            continue
        values = {
            "序号": case_no, "日期": business_date.isoformat(), "社区": community,
            "简要警情及处理结果": detail, "是否开户": "", "现住址": "",
            "房屋属性": "", "居住时间": "", "房东信息": "", "二房东信息": "",
            "备注": "", "房东是否处罚": "",
        }
        result.append({
            "source_row": number, "source_name": "所内涉警", "community_name": community,
            "person_name": "", "identity_number": "", "phone": "", "original_address": detail,
            "created_time": business_date.isoformat(), "transfer_note": "",
            "raw_values": _raw_values(headers, row), "standard_values": values,
        })
    if not result:
        raise PoliceWorkbookError("所内涉警文件中没有可导入的数据")
    return _finalize(sheet_name=sheet, rows=result, communities=communities, business_key_fields=("序号",))


def _parse_suzhou(content: bytes, filename: str, business_date: date, communities: list[dict[str, Any]]) -> dict[str, Any]:
    sheet, rows, header_index, columns = _find_sheet(content, filename, (("任务有效期", "截止日期"), ("身份证号码", "身份证号"), ("联系号码", "手机号码")))
    headers = rows[header_index]
    normalized = {_header(value): index for index, value in enumerate(headers) if _header(value)}
    def idx(*names: str) -> int | None:
        return next((normalized[_header(name)] for name in names if _header(name) in normalized), None)
    result = []
    for number, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        def value(index: int | None, fallback: int | None = None) -> str:
            target = index if index is not None else fallback
            return _cell(row[target]) if target is not None and target < len(row) else ""
        identity = normalize_identity(value(idx("身份证号码", "身份证号"), 5))
        phone = value(idx("联系号码", "手机号码"), 6)
        name = value(idx("姓名"), 4)
        if not any((identity, phone, name)):
            continue
        dispatch_date = _date_value(value(idx("下发日期"), 0), business_date) or business_date.isoformat()
        case_no = value(idx("接警编号", "jingqingbianhao"))
        community = value(idx("社区", "所属社区", "批次号"), 3)
        values = {
            "下发日期": dispatch_date,
            "截止日期": _date_value(value(idx("任务有效期", "截止日期"), 1), business_date),
            "核查人": value(idx("指派民警", "核查人"), 2), "社区": community, "姓名": name,
            "身份证号": identity, "联系号码": phone,
            "疑似现住址": value(idx("疑似现住址", "现住址"), 7), "接警编号": case_no,
            "出警日期": value(idx("出警日期")), "出警类别": value(idx("出警类别")),
            "出警内容": value(idx("出警内容", "接警信息")), "出警单位": value(idx("出警单位")),
            "参考派出所": value(idx("参考派出所")), "现住址": "", "核查结果": "",
            "研判": "", "二次反馈": "",
        }
        result.append({
            "source_row": number, "source_name": "苏州涉警", "community_name": community,
            "person_name": name, "identity_number": identity, "phone": phone,
            "original_address": values["疑似现住址"], "created_time": dispatch_date,
            "transfer_note": values["出警内容"], "raw_values": _raw_values(headers, row),
            "standard_values": values, "business_key_fields": ("接警编号",) if case_no else ("身份证号", "联系号码", "下发日期"),
        })
    if not result:
        raise PoliceWorkbookError("苏州涉警文件中没有可导入的数据")
    return _finalize(
        sheet_name=sheet,
        rows=result,
        communities=communities,
        business_key_fields=("身份证号", "联系号码", "下发日期"),
    )


def _parse_traffic(content: bytes, filename: str, business_date: date, communities: list[dict[str, Any]]) -> dict[str, Any]:
    sheet, rows, header_index, columns = _find_sheet(
        content,
        filename,
        (
            ("业务分类",),
            ("姓名",),
            ("身份证号码", "身份证号"),
            ("手机号码", "联系号码", "手机号"),
            ("地址1", "地址"),
        ),
    )
    headers = rows[header_index]
    result = []
    for number, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        def value(column: str, fallback: int | None = None) -> str:
            target = columns.get(column, fallback)
            return _cell(row[target]) if target is not None and target < len(row) else ""

        identity = normalize_identity(value("身份证号码", 5))
        phone = value("手机号码", 6)
        name = value("姓名", 4)
        if not any((identity, phone, name)):
            continue

        first_date = _date_value(_cell(row[0]) if row else "", business_date)
        second_date = _date_value(_cell(row[1]) if len(row) > 1 else "", business_date)
        issues: list[dict[str, str]] = []
        dates_conflict = bool(first_date and second_date and first_date != second_date)
        if dates_conflict:
            issues.append({
                "field": "业务日期",
                "type": "date_conflict",
                "value": "前两列日期不一致，已使用上传时确认的业务日期，请人工核对",
            })
        dispatch_date = business_date.isoformat() if dates_conflict else (first_date or second_date or business_date.isoformat())
        deadline = business_date.isoformat() if dates_conflict else (second_date or first_date or business_date.isoformat())
        community = value("业务分类", 3)
        address = value("地址1", 7)
        values = {
            "下发日期": dispatch_date,
            "截止日期": deadline,
            "核查人": "",
            "社区": community,
            "姓名": name,
            "身份证号": identity,
            "联系号码": phone,
            "地址1": address,
            "现住址": "",
            "核查结果": "",
            "研判": "",
            "二次反馈": "",
        }
        result.append({
            "source_row": number,
            "source_name": "交通涉警",
            "community_name": community,
            "person_name": name,
            "identity_number": identity,
            "phone": phone,
            "original_address": address,
            "created_time": dispatch_date,
            "transfer_note": "",
            "raw_values": _raw_values(headers, row),
            "standard_values": values,
            "validation_issues": issues,
        })
    if not result:
        raise PoliceWorkbookError("交通涉警文件中没有可导入的数据")
    return _finalize(
        sheet_name=sheet,
        rows=result,
        communities=communities,
        business_key_fields=("身份证号", "联系号码", "下发日期"),
    )


def _parse_delivery(content: bytes, filename: str, business_date: date, communities: list[dict[str, Any]]) -> dict[str, Any]:
    sheet, rows, header_index, _ = _find_sheet(content, filename, (("身份证号码", "身份证号"), ("手机号码", "手机号"), ("参考姓名",)))
    headers = rows[header_index]
    normalized = {_header(value): index for index, value in enumerate(headers) if _header(value)}
    def at(row: list[str], *names: str, fallback: int | None = None) -> str:
        index = next((normalized[_header(name)] for name in names if _header(name) in normalized), fallback)
        return _cell(row[index]) if index is not None and index < len(row) else ""
    result = []
    for number, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        identity = normalize_identity(at(row, "身份证号码", "身份证号", fallback=4))
        phone = at(row, "手机号码", "手机号", fallback=6)
        if not any((identity, phone, at(row, "姓名", fallback=3))):
            continue
        values = {
            "下发时间": _date_value(at(row, "下发时间", "下发日期", fallback=0), business_date) or business_date.isoformat(),
            "截止时间": _date_value(at(row, "截止时间", "截止日期", fallback=1), business_date),
            "核查人": at(row, "核查人", fallback=2), "姓名": at(row, "姓名", fallback=3),
            "身份证号": identity, "地址1": at(row, "地址1", "地址", fallback=5),
            "手机号码": phone, "社区": at(row, "所属社区", "社区", fallback=7),
            "参考姓名": at(row, "参考姓名", fallback=8), "参考身份证号码": normalize_identity(at(row, "参考身份证号码", fallback=9)),
            "现住址": "", "核查结果": "", "研判": "", "二次反馈": "",
        }
        result.append({"source_row": number, "source_name": "寄递业", "community_name": values["社区"], "person_name": values["姓名"], "identity_number": identity, "phone": phone, "original_address": values["地址1"], "created_time": values["下发时间"], "transfer_note": "", "raw_values": _raw_values(headers, row), "standard_values": values})
    if not result:
        raise PoliceWorkbookError("寄递业文件中没有可导入的数据")
    return _finalize(sheet_name=sheet, rows=result, communities=communities, business_key_fields=("身份证号", "手机号码"))


def _parse_return(content: bytes, filename: str, business_date: date, communities: list[dict[str, Any]]) -> dict[str, Any]:
    sheet, rows, header_index, _ = _find_sheet(content, filename, (("身份证号码", "身份证号"), ("联系号码", "手机号"), ("高频抓拍小区",)))
    headers = rows[header_index]
    normalized = {_header(value): index for index, value in enumerate(headers) if _header(value)}
    def at(row: list[str], *names: str, fallback: int | None = None) -> str:
        index = next((normalized[_header(name)] for name in names if _header(name) in normalized), fallback)
        return _cell(row[index]) if index is not None and index < len(row) else ""
    result = []
    for number, row in enumerate(rows[header_index + 1:], start=header_index + 2):
        identity = normalize_identity(at(row, "身份证号码", "身份证号", fallback=5))
        phone = at(row, "联系号码", "手机号", fallback=6)
        if not any((identity, phone, at(row, "姓名", fallback=4))):
            continue
        values = {
            "下发日期": _date_value(at(row, "下发日期", fallback=0), business_date) or business_date.isoformat(),
            "截止日期": _date_value(at(row, "截止日期", fallback=1), business_date),
            "核查人": at(row, "核查人", fallback=2), "社区": at(row, "业务分类", "社区", fallback=3),
            "姓名": at(row, "姓名", fallback=4), "身份证号码": identity, "联系号码": phone,
            "高频抓拍小区": at(row, "高频抓拍小区", fallback=7), "现住址": "", "核查反馈": "",
            "研判": "", "二次核查结果": "",
        }
        result.append({"source_row": number, "source_name": "疑似返苏", "community_name": values["社区"], "person_name": values["姓名"], "identity_number": identity, "phone": phone, "original_address": values["高频抓拍小区"], "created_time": values["下发日期"], "transfer_note": "", "raw_values": _raw_values(headers, row), "standard_values": values})
    if not result:
        raise PoliceWorkbookError("疑似返苏文件中没有可导入的数据")
    return _finalize(sheet_name=sheet, rows=result, communities=communities, business_key_fields=("身份证号码", "联系号码"))


PROFILES: dict[str, ImportProfile] = {
    "fullchain_raw": ImportProfile("fullchain_raw", "fullchain", "全链条原始数据", "", "全链条", True, "待基础管控审核的全链条原始文件", ("姓名", "身份证号", "地址"), None),
    "fullchain_processed": ImportProfile("fullchain_processed", "fullchain", "全链条已处理数据", "", "全链条", True, "已包含社区和登记情况的全链条文件", ("社区", "登记情况", "姓名", "身份证号", "手机号", "地址"), None),
    "rental_processed": ImportProfile("rental_processed", "rental", "出租房屋核查", "", "出租房屋核查", True, "双短日期列的承租人任务；进入现有出租房屋核查任务池", ("前两列短日期", "社区", "承租人姓名", "承租人身份证号码", "承租人联系号码", "租赁房屋地址"), _parse_rental),
    "police_internal_processed": ImportProfile("police_internal_processed", "police", "所内涉警", "internal", "涉警统计", True, "按接警编号导入，仅进入审核、发布和在线查询", ("接警编号", "社区", "简要警情及处理结果"), _parse_internal),
    "police_suzhou_processed": ImportProfile("police_suzhou_processed", "police", "苏州涉警", "suzhou", "苏州涉警", True, "人员型涉警任务；未配置腾讯表时可导入但不可发布", ("任务有效期", "身份证号码", "联系号码"), _parse_suzhou),
    "police_traffic_processed": ImportProfile("police_traffic_processed", "police", "交通涉警", "traffic", "交通涉警", True, "双短日期列的人员型涉警任务；未配置腾讯表时可导入但不可发布", ("前两列短日期", "业务分类（社区）", "姓名", "身份证号码", "手机号码", "地址1"), _parse_traffic),
    "delivery_processed": ImportProfile("delivery_processed", "delivery", "寄递业", "", "寄递业", True, "身份证号和手机号作为业务主键", ("身份证号码", "手机号码", "参考姓名"), _parse_delivery),
    "suspect_return_processed": ImportProfile("suspect_return_processed", "suspect_return", "疑似返苏", "", "疑似返苏", True, "身份证号码和联系号码作为业务主键", ("身份证号码", "联系号码", "高频抓拍小区"), _parse_return),
}


def profile_payload(profile: ImportProfile) -> dict[str, Any]:
    return {"key": profile.key, "business_type": profile.business_type, "label": profile.label, "police_subtype": profile.police_subtype, "target_parser": profile.target_parser, "enabled": profile.enabled, "description": profile.description, "example_fields": list(profile.example_fields), "adapter_version": ADAPTER_VERSION}


def parse_profile(profile_key: str, content: bytes, filename: str, business_date: date, communities: list[dict[str, Any]]) -> dict[str, Any]:
    profile = PROFILES.get(profile_key)
    if not profile:
        raise PoliceWorkbookError("未知导入类型")
    if not profile.enabled:
        raise PoliceWorkbookError(profile.description)
    if profile.parse is None:
        raise PoliceWorkbookError("该入口使用兼容全链条接口")
    result = profile.parse(content, filename, business_date, communities)
    result.update({"profile": profile, "adapter_version": ADAPTER_VERSION})
    return result


def preview_token(*, user_id: int, file_sha256: str, profile_key: str, business_date: date, row_count: int, sheet_name: str, issued_at: int | None = None) -> str:
    timestamp = int(datetime.now().timestamp()) if issued_at is None else issued_at
    payload = stable_json({"user_id": user_id, "file_sha256": file_sha256, "profile": profile_key, "business_date": business_date.isoformat(), "adapter_version": ADAPTER_VERSION, "row_count": row_count, "sheet_name": sheet_name, "issued_at": timestamp})
    signature = hmac.new(settings.registry_hmac_key.encode(), payload.encode(), sha256).hexdigest()
    return f"{timestamp}.{signature}"


def verify_preview_token(token: str, **kwargs: Any) -> bool:
    try:
        timestamp_text, signature = token.split(".", 1)
        timestamp = int(timestamp_text)
    except (TypeError, ValueError):
        return False
    now = int(datetime.now().timestamp())
    if timestamp > now + 60 or now - timestamp > 30 * 60:
        return False
    expected = preview_token(issued_at=timestamp, **kwargs)
    return hmac.compare_digest(expected, f"{timestamp}.{signature}")
