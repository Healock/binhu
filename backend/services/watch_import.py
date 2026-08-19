"""人员标签名单的安全解析、去重和预览统计。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from services.police_dispatch import PoliceWorkbookError, read_workbook_rows
from services.registry_security import normalize_identity, normalize_phone


CORE_WATCH_CATEGORIES = (
    ("通勤人员", "通勤人员", "#1677ff", "notice", 10),
    ("五失人员", "五失人员", "#fa8c16", "warning", 20),
    ("重点人员", "重点人员", "#f5222d", "critical", 30),
    ("精障人员", "精障人员", "#722ed1", "warning", 40),
)
CORE_CATEGORY_CODES = {item[0] for item in CORE_WATCH_CATEGORIES}

IDENTITY_PATTERN = re.compile(r"^(?:\d{15}|\d{17}[0-9X])$")
IDENTITY_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
IDENTITY_CHECKS = "10X98765432"

HEADER_ALIASES = {
    "name": ("姓名", "人员姓名", "对象姓名"),
    "identity": ("身份证", "身份证号", "身份证号码", "证件号码"),
    "phone": ("手机号", "手机号码", "电话号码", "联系电话"),
    "organization": ("所属机构", "派出所", "派出所名称", "单位"),
    "source": ("来源", "数据来源"),
    "occurred_at": ("核查时间", "核查时间(非必填)", "创建时间", "日期"),
    "result": ("核查结果", "结果"),
    "note": ("备注", "备注（跨区、跨市、跨省）", "说明"),
}


@dataclass(slots=True)
class WatchImportRow:
    source_file: str
    source_sheet: str
    source_row: int
    category_code: str
    name: str
    identity_number: str
    phone: str = ""
    organization: str = ""
    source_label: str = ""
    occurred_at: str = ""
    result: str = ""
    note: str = ""
    blocking_issue: str = ""

    @property
    def source_ref(self) -> str:
        return f"{self.source_file}:{self.source_sheet}:{self.source_row}"[:190]

    def payload(self) -> dict[str, Any]:
        return asdict(self)


def normalize_name(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").replace("\u3000", " ")).strip()


def valid_identity(value: Any) -> bool:
    identity = normalize_identity(str(value or ""))
    if not IDENTITY_PATTERN.fullmatch(identity):
        return False
    if len(identity) == 15:
        return True
    try:
        datetime.strptime(identity[6:14], "%Y%m%d")
    except ValueError:
        return False
    total = sum(int(char) * weight for char, weight in zip(identity[:17], IDENTITY_WEIGHTS, strict=True))
    return identity[-1] == IDENTITY_CHECKS[total % 11]


def _normalized_header(value: Any) -> str:
    return re.sub(r"[\s　:：()（）]", "", str(value or "")).lower()


def _header_mapping(row: list[str]) -> dict[str, int]:
    normalized = [_normalized_header(value) for value in row]
    result: dict[str, int] = {}
    for field, aliases in HEADER_ALIASES.items():
        wanted = {_normalized_header(alias) for alias in aliases}
        for index, value in enumerate(normalized):
            if value in wanted:
                result[field] = index
                break
    return result


def _cell(row: list[str], mapping: dict[str, int], field: str) -> str:
    index = mapping.get(field)
    if index is None or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _parse_datetime(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = (
        text.replace("年", "-").replace("月", "-").replace("日", " ")
        .replace("/", "-").replace(".", "-")
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    for candidate in (normalized, normalized[:19], normalized[:10]):
        try:
            return datetime.fromisoformat(candidate).isoformat(sep=" ", timespec="seconds")
        except ValueError:
            continue
    for pattern in (
        "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M", "%Y/%m/%d", "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(text, pattern).isoformat(sep=" ", timespec="seconds")
        except ValueError:
            continue
    return ""


def _row_from_values(
    *,
    source_file: str,
    sheet_name: str,
    row_number: int,
    category_code: str,
    values: list[str],
    mapping: dict[str, int],
) -> WatchImportRow | None:
    if not any(str(value or "").strip() for value in values):
        return None
    name = normalize_name(_cell(values, mapping, "name"))
    identity = normalize_identity(_cell(values, mapping, "identity"))
    issue = ""
    if not identity:
        issue = "身份证号为空"
    elif not valid_identity(identity):
        issue = "身份证号格式或校验位异常"
    elif not name:
        issue = "姓名为空"
    return WatchImportRow(
        source_file=source_file,
        source_sheet=sheet_name,
        source_row=row_number,
        category_code=category_code,
        name=name,
        identity_number=identity,
        phone=normalize_phone(_cell(values, mapping, "phone")),
        organization=_cell(values, mapping, "organization"),
        source_label=_cell(values, mapping, "source"),
        occurred_at=_parse_datetime(_cell(values, mapping, "occurred_at")),
        result=_cell(values, mapping, "result"),
        note=_cell(values, mapping, "note"),
        blocking_issue=issue,
    )


def parse_watch_workbook(
    content: bytes,
    filename: str,
    *,
    category_code: str,
) -> list[WatchImportRow]:
    if category_code not in CORE_CATEGORY_CODES:
        raise ValueError("人员标签分类无效")
    try:
        sheets = read_workbook_rows(content, filename)
    except PoliceWorkbookError as exc:
        raise ValueError(str(exc)) from exc
    parsed: list[WatchImportRow] = []
    for sheet_name, rows in sheets:
        if not rows:
            continue
        header_row = -1
        mapping: dict[str, int] = {}
        for index, values in enumerate(rows[:20]):
            candidate = _header_mapping(values)
            if {"name", "identity"}.issubset(candidate):
                header_row = index
                mapping = candidate
                break
        if header_row >= 0:
            source_rows = enumerate(rows[header_row + 1 :], start=header_row + 2)
        else:
            nonempty = [row for row in rows[:20] if any(str(value or "").strip() for value in row)]
            identity_hits = sum(
                1 for row in nonempty
                if len(row) >= 2 and valid_identity(row[1])
            )
            if not nonempty or identity_hits < max(1, (len(nonempty) + 1) // 2):
                continue
            mapping = {"name": 0, "identity": 1}
            source_rows = enumerate(rows, start=1)
        for row_number, values in source_rows:
            item = _row_from_values(
                source_file=filename,
                sheet_name=sheet_name,
                row_number=row_number,
                category_code=category_code,
                values=values,
                mapping=mapping,
            )
            if item:
                parsed.append(item)
    if not parsed:
        raise ValueError("未找到包含姓名和身份证号的人员名单")
    return parsed


def summarize_watch_rows(rows: list[WatchImportRow]) -> dict[str, Any]:
    grouped: dict[str, list[WatchImportRow]] = {}
    invalid_count = 0
    missing_identity_count = 0
    invalid_identity_count = 0
    missing_name_count = 0
    for row in rows:
        if row.blocking_issue:
            invalid_count += 1
            if row.blocking_issue == "身份证号为空":
                missing_identity_count += 1
            elif row.blocking_issue == "姓名为空":
                missing_name_count += 1
            else:
                invalid_identity_count += 1
            continue
        grouped.setdefault(row.identity_number, []).append(row)

    name_conflicts = 0
    phone_conflicts = 0
    normalized: list[dict[str, Any]] = []
    for identity, items in grouped.items():
        names = list(dict.fromkeys(item.name for item in items if item.name))
        phones = list(dict.fromkeys(item.phone for item in items if item.phone))
        if len(names) > 1:
            name_conflicts += 1
        if len(phones) > 1:
            phone_conflicts += 1
        occurred_values = sorted(item.occurred_at for item in items if item.occurred_at)
        normalized.append({
            "identity_number": identity,
            "name": names[0] if names else "",
            "phone": phones[0] if phones else "",
            "valid_from": occurred_values[0] if occurred_values else None,
            "source_refs": [item.source_ref for item in items],
            "row_count": len(items),
            "name_conflict": len(names) > 1,
            "phone_conflict": len(phones) > 1,
        })

    unique_people = len(grouped)
    duplicate_rows = max(0, len(rows) - invalid_count - unique_people)
    blocking_count = invalid_count + name_conflicts + phone_conflicts
    return {
        "total_rows": len(rows),
        "valid_rows": len(rows) - invalid_count,
        "unique_people": unique_people,
        "duplicate_rows": duplicate_rows,
        "blocking_count": blocking_count,
        "missing_identity_count": missing_identity_count,
        "invalid_identity_count": invalid_identity_count,
        "missing_name_count": missing_name_count,
        "name_conflict_groups": name_conflicts,
        "phone_conflict_groups": phone_conflicts,
        "can_confirm": blocking_count == 0,
        "people": normalized,
    }
