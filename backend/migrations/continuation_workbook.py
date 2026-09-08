"""Read-only continuation workbook reconciliation. Reports contain no task values."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
from pathlib import Path
import posixpath
import re
from urllib.parse import quote
import zipfile
from xml.etree import ElementTree as ET

from services.parsers import get_parser

NS = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
MODEL_HASH = "3dfcd519fe3388d9d14887c713b75dd3f35b3b3bcf69cb946b09a59164dc46d1"
DATE_FIELDS = {"下发日期", "下发时间", "截止日期", "截止时间", "创建时间"}
ALIASES = {
    "出租房屋核查": {
        "身份证号码": "身份证号",
        "入住方式（自购，房东出租，中介出租）": "入住方式",
        "二次核查结果": "二次反馈",
    },
    "疑似返苏": {"核查结果": "核查反馈"},
}
RESULT_ALIASES = {
    ("疑似返苏", "移交，备注后面填写移交哪个社区"): "移交，移交哪个社区写备注",
}


def normalize_date(value: str, *, epoch_1904: bool = False) -> str:
    """Only called for date columns; never use wall-clock time as fallback."""
    text = value.strip()
    if not text:
        return ""
    short = re.fullmatch(r"(\d{1,2})[./月-](\d{1,2})日?", text)
    if short:
        return date(2026, int(short[1]), int(short[2])).isoformat()
    explicit = re.fullmatch(r"(\d{4})[./年-](\d{1,2})[./月-](\d{1,2})日?", text)
    if explicit:
        return date(int(explicit[1]), int(explicit[2]), int(explicit[3])).isoformat()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", text):
        return datetime.fromisoformat(text).isoformat(sep=" ")
    try:
        serial = Decimal(text)
        if Decimal(30000) <= serial < Decimal(60000):
            base = datetime(1904, 1, 1) if epoch_1904 else datetime(1899, 12, 30)
            result = base + timedelta(seconds=float(serial * 86400))
            return result.date().isoformat() if serial == int(serial) else result.isoformat(sep=" ", timespec="seconds")
    except InvalidOperation:
        pass
    raise ValueError("invalid_date")


def read_sheets(path: Path):
    with zipfile.ZipFile(path) as archive:
        strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            strings = ["".join(t.text or "" for t in si.findall(".//x:t", NS))
                       for si in ET.fromstring(archive.read("xl/sharedStrings.xml"))]
        book = ET.fromstring(archive.read("xl/workbook.xml"))
        props = book.find("x:workbookPr", NS)
        epoch = props is not None and props.get("date1904") in {"1", "true"}
        rels = {r.get("Id"): r for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
        for sheet in book.findall("x:sheets/x:sheet", NS):
            rel = rels[sheet.get(REL)]
            if rel.get("TargetMode") == "External":
                raise ValueError("external_worksheet")
            target = rel.get("Target", "")
            target = posixpath.normpath(target.lstrip("/") if target.startswith("/") else "xl/" + target)
            if not target.startswith("xl/worksheets/"):
                raise ValueError("invalid_worksheet_target")
            rows = []
            for row in ET.fromstring(archive.read(target)).findall(".//x:sheetData/x:row", NS):
                cells = {}
                formula_columns = []
                for cell in row.findall("x:c", NS):
                    reference = cell.get("r", "")
                    match = re.fullmatch(r"([A-Z]+)\d+", reference)
                    if not match:
                        raise ValueError("invalid_cell_reference")
                    index = 0
                    for ch in match[1]:
                        index = index * 26 + ord(ch) - 64
                    value = cell.find("x:v", NS)
                    text = "" if value is None else value.text or ""
                    if cell.get("t") == "s" and text:
                        text = strings[int(text)]
                    elif cell.get("t") == "inlineStr":
                        text = "".join(t.text or "" for t in cell.findall(".//x:t", NS))
                    if cell.find("x:f", NS) is not None or cell.get("t") == "e":
                        formula_columns.append(index - 1)
                    cells[index - 1] = text
                rows.append((int(row.attrib["r"]), cells, formula_columns))
            yield sheet.attrib["name"], target, epoch, rows


def read_workbook(parser_type: str, path: Path, run_id: str):
    parser = get_parser(parser_type)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    report = {"parser_type": parser_type, "file": path.name, "sha256": digest,
              "sheets": [], "issues": [], "adjustments": [], "total": 0}
    records = []
    keys = defaultdict(list)
    for sheet, part, epoch, rows in read_sheets(path):
        summary = {"sheet": sheet, "part": part, "blank_rows": 0, "preamble_rows": 0,
                   "repeated_headers": 0, "data_rows": 0, "header_row": None, "missing_columns": []}
        report["sheets"].append(summary)
        header = None
        for physical, cells, formulas in rows:
            stripped = {i: value.strip() for i, value in cells.items()}
            if not any(stripped.values()):
                summary["blank_rows"] += 1
                continue
            is_header = "核查人" in stripped.values() and any(v in {"身份证号", "身份证号码"} for v in stripped.values())
            if is_header:
                if header is not None:
                    summary["repeated_headers"] += 1
                    continue
                header = {i: ALIASES.get(parser_type, {}).get(value, value) for i, value in stripped.items()}
                for canonical, aliases in parser.DATABASE_COLUMN_ALIASES.items():
                    header = {i: canonical if value in aliases else value for i, value in header.items()}
                names = [v for v in header.values() if v in parser.COLUMNS]
                if len(set(names)) != len(names):
                    report["issues"].append({"sheet": sheet, "row": physical, "code": "duplicate_header"})
                summary["header_row"] = physical
                summary["missing_columns"] = [c for c in parser.COLUMNS if c not in names]
                summary["mapped_columns"] = names
                continue
            if header is None:
                # Only the known rental instruction before its actual header is a preamble.
                if parser_type == "出租房屋核查" and physical == 1 and part == "xl/worksheets/sheet1.xml":
                    summary["preamble_rows"] += 1
                else:
                    report["issues"].append({"sheet": sheet, "row": physical, "code": "unrecognized_nonempty_row"})
                continue
            summary["data_rows"] += 1
            row_issues = []
            if formulas:
                row_issues.append("formula_or_error_cell")
            unmapped = [i + 1 for i, v in stripped.items() if v and header.get(i) not in parser.COLUMNS]
            if unmapped:
                row_issues.append("unmapped_nonempty_column")
            values = {c: "" for c in parser.COLUMNS}
            for i, value in stripped.items():
                if header.get(i) in values:
                    values[header[i]] = value
            for column in DATE_FIELDS.intersection(values):
                try:
                    values[column] = normalize_date(values[column], epoch_1904=epoch)
                except (ValueError, OverflowError):
                    row_issues.append("invalid_date:" + column)
            result_field = getattr(__import__("services.task_workflow", fromlist=["TASK_WORKFLOWS"]).TASK_WORKFLOWS[parser_type], "result_field")
            result_value = values.get(result_field, "")
            if (parser_type, result_value) in RESULT_ALIASES:
                values[result_field] = RESULT_ALIASES[(parser_type, result_value)]
                report["adjustments"].append({"sheet": sheet, "row": physical, "field": result_field, "code": "canonical_result_alias"})
            if parser_type == "疑似未注销模型三" and digest == MODEL_HASH and part == "xl/worksheets/sheet1.xml" and physical == 48:
                if values["联系方式"]:
                    row_issues.append("approved_phone_override_not_empty")
                else:
                    values["联系方式"] = "无电话"
                    report["adjustments"].append({"sheet": sheet, "row": physical, "field": "联系方式", "code": "approved_no_phone"})
            try:
                parser.validate_new_row(values)
            except ValueError:
                row_issues.append("missing_required_field")
            ref = f"continuation:{run_id}:{digest}:{quote(sheet, safe='')}:{physical}"
            if len(ref) > 190:
                row_issues.append("source_ref_too_long")
            locator = {"sheet": sheet, "row": physical}
            key = parser.make_row_key(values)
            keys[key].append(locator)
            for code in row_issues:
                report["issues"].append({**locator, "code": code})
            records.append({"values": values, "row_key": key, "source_ref": ref, **locator})
    for locators in keys.values():
        if len(locators) > 1:
            report["issues"].append({"code": "duplicate_business_key", "rows": locators})
    report["total"] = len(records)
    report["issue_count"] = len(report["issues"])
    report["ready"] = not report["issues"]
    return records, report
