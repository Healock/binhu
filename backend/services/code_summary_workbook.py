"""Read a Peace Code or Manager Code workbook without retaining the source rows."""

from __future__ import annotations

import re
from datetime import datetime
from io import BytesIO
from itertools import chain
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from services.code_summary import SOURCE_META, CodeSummaryError, parse_source_datetime
from services.qmf_registration import normalize_identity, valid_identity


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100_000
PEACE_HEADERS = {
    "idCard": ("idCard", "身份证号", "证件号码", "身份证"),
    "comparisonTime": ("comparisonTime", "比对时间", "采集时间"),
    "location": ("location", "采集位置", "采集地点", "采集窗口"),
    "population": ("population", "人员类型", "登记情况"),
    "assignJgmc": ("assignJgmc", "分配机构名称"),
    "jgmc": ("jgmc", "机构名称", "所属单位"),
    "updateDate": ("updateDate", "更新时间"),
    "id": ("id",),
}
MANAGER_HEADERS = {
    "zhUserIdCard": ("zhUserIdCard", "住户身份证号", "住户证件号码", "身份证号", "证件号码"),
    "comparisonTime": ("comparisonTime", "比对时间"),
    "gjUserName": ("gjUserName", "管家姓名", "管家名称"),
    "population": ("population", "人员类型"),
    "pcsname": ("pcsname", "派出所名称", "所属派出所"),
    "jgmc": ("jgmc", "机构名称"),
    "updateDate": ("updateDate", "更新日期时间"),
    "updateTime": ("updateTime", "更新时间"),
    "id": ("id",),
}
SOURCE_LABELS = {"peace": "平安码", "manager": "管家码"}
SOURCE_HEADERS = {"peace": PEACE_HEADERS, "manager": MANAGER_HEADERS}
RECOMMENDED_HEADERS = {
    "peace": "身份证号、比对时间、采集位置、人员类型及机构名称",
    "manager": "住户身份证号、比对时间、管家姓名、人员类型及派出所名称",
}
# For exports without a header, column G is the collection point, while
# D is the residential address and N is the scan channel.
PEACE_EXPORT_COLUMNS = {"idCard": 1, "location": 6, "jgmc": 7, "comparisonTime": 8, "population": 9}
PEACE_EXPORT_WIDTH = 16


def _read_time(value: object, row_number: int, label: str) -> datetime:
    try:
        parsed = parse_source_datetime(value, "comparisonTime")
        if parsed.tzinfo is not None or (
            not isinstance(value, datetime)
            and (not isinstance(value, str) or len(value.strip()) <= 10 or value.strip().endswith("Z"))
        ):
            raise ValueError
        return parsed
    except (CodeSummaryError, ValueError):
        raise CodeSummaryError("invalid_time", f"第 {row_number} 行的{label}无效，请使用本地日期和时间") from None


def _has_window_export_layout(cells) -> bool:
    if not cells or any(cell.value not in (None, "") for cell in cells[PEACE_EXPORT_WIDTH:]):
        return False
    station, channel = cells[7].value, cells[13].value
    return (
        isinstance(station, str) and station.strip().endswith("派出所")
        and isinstance(channel, str) and channel.strip().startswith(("微信", "支付宝"))
    )


def _is_peace_window_export(cells) -> bool:
    if not _has_window_export_layout(cells):
        return False
    identity = cells[PEACE_EXPORT_COLUMNS["idCard"]].value
    return isinstance(identity, str) and re.fullmatch(r"[0-9]{17}[0-9Xx]", identity.strip()) is not None


def _header_positions(source: str, header) -> dict[str, int]:
    aliases = {alias: field for field, names in SOURCE_HEADERS[source].items() for alias in names}
    positions = {}
    for index, cell in enumerate(header):
        field = aliases.get(str(cell.value or "").strip())
        if field is None:
            continue
        if field in positions:
            raise CodeSummaryError("duplicate_header", f"{SOURCE_LABELS[source]}表头有重复列，请为每个字段保留一列")
        positions[field] = index
    meta = SOURCE_META[source]
    if not set(meta["required"]).issubset(positions) or not set(meta["station_fields"]).intersection(positions):
        raise CodeSummaryError(
            "missing_header", f"请在第一个工作表的首行填写{RECOMMENDED_HEADERS[source]}"
        )
    return positions


def _read_rows(source: str, sheet) -> list[dict]:
    rows = sheet.iter_rows(max_col=100)
    first_row = next(rows, ())
    window_export = source == "peace" and _is_peace_window_export(first_row)
    if window_export:
        positions = PEACE_EXPORT_COLUMNS
        data_rows = chain((first_row,), rows)
        first_row_number = 1
    else:
        positions = _header_positions(source, first_row)
        data_rows = rows
        first_row_number = 2

    meta = SOURCE_META[source]
    result = []
    for row_number, cells in enumerate(data_rows, start=first_row_number):
        if row_number - first_row_number >= MAX_ROWS:
            raise CodeSummaryError("too_many_rows", "每份工作簿最多 10 万行，请按日期拆分")
        if window_export:
            if all(cell.value in (None, "") for cell in cells):
                continue
            if not _has_window_export_layout(cells):
                raise CodeSummaryError("invalid_export_layout", f"第 {row_number} 行不符合窗口登记原始导出的列顺序")
        elif all(cells[index].value in (None, "") for index in positions.values()):
            continue
        if any(cells[index].data_type == "f" for index in positions.values()):
            raise CodeSummaryError("formula_cell", f"第 {row_number} 行包含公式，请粘贴为值后重试")
        row = {field: cells[index].value for field, index in positions.items()}
        identity = row[meta["identity"]]
        if not isinstance(identity, str) or not valid_identity(normalize_identity(identity)):
            raise CodeSummaryError("invalid_identity", f"第 {row_number} 行的身份证号无效，请使用文本格式并填写完整号码")
        row["comparisonTime"] = _read_time(row["comparisonTime"], row_number, "比对时间")
        for field in meta["update_fields"]:
            if str(row.get(field) or "").strip():
                row[field] = _read_time(row[field], row_number, "更新时间")
        if len(str(row.get("location") or "").strip()) > 255:
            raise CodeSummaryError("invalid_location", f"第 {row_number} 行的采集位置过长，请缩短到 255 字以内")
        result.append(row)
    if not result:
        raise CodeSummaryError("empty_workbook", f"{SOURCE_LABELS[source]}工作簿没有数据行")
    return result


def read_code_summary_workbook(source: str, content: bytes) -> list[dict]:
    try:
        with ZipFile(BytesIO(content)) as archive:
            if len(archive.infolist()) > 200 or sum(item.file_size for item in archive.infolist()) > 250 * 1024 * 1024:
                raise CodeSummaryError("workbook_too_large", "工作簿解压后过大，请按日期拆分")
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=False, keep_links=False)
        try:
            if not workbook.worksheets or workbook.worksheets[0].sheet_state != "visible":
                raise CodeSummaryError("missing_sheet", f"请把{SOURCE_LABELS[source]}明细放在第一个可见工作表")
            sheet = workbook.worksheets[0]
            sheet.reset_dimensions()
            return _read_rows(source, sheet)
        finally:
            workbook.close()
    except CodeSummaryError:
        raise
    except (BadZipFile, InvalidFileException, OSError, KeyError, RuntimeError, ValueError, IndexError, TypeError, ParseError):
        raise CodeSummaryError("invalid_workbook", "文件不是可读取的 XLSX 工作簿，请重新另存或导出") from None
