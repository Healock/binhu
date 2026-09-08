"""一次性接续导入工具（默认只读预览）。

该工具用于把同事已经处理过一部分的本地 XLSX 接入现有任务池，
不创建下发批次，也不访问腾讯文档。生产写入必须显式传入 ``--apply``；
脚本会再次核对本地数据源开关和生产环境身份，并在单一事务中提交。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

import aiomysql

from services.local_source import create_local_source_row, ensure_local_source_schema
from services.parsers import get_parser

FILES = {
    "疑似未注销模型三": "疑似未注销模型三.xlsx",
    "全链条": "全链条.xlsx",
    "出租房屋核查": "出租房屋核查.xlsx",
    "疑似返苏": "注销人员疑似返苏核查.xlsx",
}
HEADER_HINTS = {
    "疑似未注销模型三": "截止时间",
    "全链条": "下发日期",
    "出租房屋核查": "下发时间",
    "疑似返苏": "下发日期",
}


def _excel_value(value: str) -> str:
    value = (value or "").strip()
    # 文件中的 8.31/9.2/9.5 是 2026 年业务日期；截止日期原样保留。
    m = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})", value)
    if m:
        return f"2026-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return value


def _sheet_rows(path: Path) -> list[list[str]]:
    """读取 XLSX sharedStrings/inlineStr，不依赖本机 Excel。"""
    with zipfile.ZipFile(path) as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            for si in root.findall("x:si", ns):
                shared.append("".join(t.text or "" for t in si.findall(".//x:t", ns)))
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        relmap = {r.attrib["Id"]: r.attrib["Target"] for r in rels}
        ns = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
              "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
        sheets = workbook.find("x:sheets", ns)
        if sheets is None:
            return []
        for sheet in sheets.findall("x:sheet", ns):
            target = relmap.get(sheet.attrib.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"), "")
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            if target not in zf.namelist():
                continue
            root = ET.fromstring(zf.read(target))
            rows: list[list[str]] = []
            for row in root.findall(".//x:row", ns):
                cells: dict[int, str] = {}
                for cell in row.findall("x:c", ns):
                    ref = cell.attrib.get("r", "A1")
                    col = 0
                    for ch in re.match(r"[A-Z]+", ref).group(0):
                        col = col * 26 + ord(ch) - 64
                    col -= 1
                    value = cell.find("x:v", ns)
                    inline = cell.find("x:is", ns)
                    text = "" if value is None else value.text or ""
                    if cell.attrib.get("t") == "s" and text.isdigit():
                        text = shared[int(text)] if int(text) < len(shared) else ""
                    elif inline is not None:
                        text = "".join(t.text or "" for t in inline.findall(".//x:t", ns))
                    cells[col] = text
                if cells:
                    # 保留 XLSX 中的真实物理行号，不能因为空行被过滤而偏移来源引用。
                    physical = int(re.search(r"\d+", row.attrib.get("r", "1")).group(0))
                    values = [cells.get(i, "") for i in range(max(cells) + 1)]
                    rows.append([str(physical), *values])
            if any(HEADER_HINTS.get(parser, "") in row for parser in HEADER_HINTS for row in rows[:10]):
                return rows
        return []


def parse_file(parser_type: str, path: Path) -> tuple[list[dict[str, str]], dict]:
    parser = get_parser(parser_type)
    rows = _sheet_rows(path)
    header_index = next((i for i, row in enumerate(rows[:10]) if HEADER_HINTS[parser_type] in row), None)
    if header_index is None:
        raise ValueError(f"{path.name}: 未找到表头")
    header = rows[header_index][1:]
    data_offset = 1  # 第一个元素是保存下来的 XLSX 物理行号
    aliases = {column: column for column in parser.COLUMNS}
    # 历史工作簿常把身份证号写成“身份证号码”；这是已知安全别名。
    if "身份证号" in aliases and "身份证号" not in header and "身份证号码" in header:
        aliases["身份证号"] = "身份证号码"
    for standard, names in parser.DATABASE_COLUMN_ALIASES.items():
        for name in names:
            if name in header and standard not in header:
                aliases[standard] = name
    parsed: list[dict[str, str]] = []
    invalid = 0
    for values in rows[header_index + 1 :]:
        physical_row = values[0]
        values = values[data_offset:]
        if not any(str(v).strip() for v in values):
            continue
        item = {column: _excel_value(values[header.index(aliases[column])] if aliases[column] in header and header.index(aliases[column]) < len(values) else "") for column in parser.COLUMNS}
        if not any(item.values()):
            continue
        try:
            parser.validate_new_row(item)
        except ValueError:
            invalid += 1
            continue
        item["__physical_row"] = str(physical_row)
        parsed.append(item)
    return parsed, {"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "valid": len(parsed), "invalid": invalid, "header_row": header_index + 1}


async def _connect():
    from config import settings
    return await aiomysql.connect(host=settings.MYSQL_HOST, port=settings.MYSQL_PORT, user=settings.MYSQL_USER, password=settings.MYSQL_PASSWORD, db=settings.MYSQL_ONLINE_DATA_DB, autocommit=False, charset="utf8mb4")


async def apply_import(run_id: str, parsed: dict[str, list[dict[str, str]]], reports: list[dict]) -> None:
    from config import settings
    if settings.APP_ENVIRONMENT != "production" or not settings.LOCAL_DATA_SOURCE_ENABLED or settings.TXDOCS_ENABLED:
        raise RuntimeError("生产身份或本地数据源开关不符合要求，拒绝写入")
    conn = await _connect()
    try:
        async with conn.cursor() as cur:
            await ensure_local_source_schema(cur)
            for report in reports:
                parser_type = report["parser_type"]
                parser = get_parser(parser_type)
                for item in parsed[parser_type]:
                    physical = item.pop("__physical_row")
                    source_ref = f"continuation:{run_id}:{report['sha256']}:{report['header_row']}:{physical}"[:190]
                    result = await create_local_source_row(cur, parser_type, item, source_kind="one_time_continuation_import", source_ref=source_ref)
                    date_field = next((name for name in ("下发日期", "下发时间", "截止时间") if item.get(name)), None)
                    business_date = item.get(date_field, "") if date_field else ""
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", business_date):
                        business_date = datetime.utcnow().date().isoformat()
                    await cur.execute("INSERT INTO _online_summary_updates (task_id,parser_type,row_key,revision,business_date,operation_id,status) VALUES (%s,%s,%s,1,%s,%s,'pending') ON DUPLICATE KEY UPDATE operation_id=VALUES(operation_id),updated_at=UTC_TIMESTAMP()", (result["local_task_id"], parser_type, result["row_key"], business_date, run_id))
            await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-dir", type=Path, default=Path.home() / "Desktop")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()
    parsed: dict[str, list[dict[str, str]]] = {}
    reports: list[dict] = []
    for parser_type, filename in FILES.items():
        rows, report = parse_file(parser_type, args.input_dir / filename)
        report["parser_type"] = parser_type
        parsed[parser_type] = rows
        reports.append(report)
    output = {"run_id": args.run_id, "reports": reports, "total_valid": sum(r["valid"] for r in reports), "apply": args.apply}
    if args.report:
        args.report.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if args.apply:
        asyncio.run(apply_import(args.run_id, parsed, reports))


if __name__ == "__main__":
    main()
