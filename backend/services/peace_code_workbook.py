"""Read a Peace Code workbook without retaining the source rows."""

from __future__ import annotations

from datetime import datetime
from io import BytesIO
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from services.code_summary import CodeSummaryError, parse_source_datetime
from services.qmf_registration import normalize_identity, valid_identity


MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100_000
HEADERS = {
    "idCard": ("idCard", "身份证号", "证件号码"),
    "comparisonTime": ("comparisonTime", "比对时间"),
    "location": ("location", "采集位置", "采集地点"),
    "population": ("population", "人员类型"),
    "assignJgmc": ("assignJgmc", "分配机构名称"),
    "jgmc": ("jgmc", "机构名称"),
    "updateDate": ("updateDate", "更新时间"),
    "id": ("id",),
}
REQUIRED_COLUMNS = {"idCard", "comparisonTime", "location", "population"}
STATION_COLUMNS = {"assignJgmc", "jgmc"}


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


def _read_rows(sheet) -> list[dict]:
    rows = sheet.iter_rows(max_col=100)
    header = next(rows, ())
    positions = {}
    for index, cell in enumerate(header):
        name = str(cell.value or "").strip()
        for field, aliases in HEADERS.items():
            if name in aliases:
                if field in positions:
                    raise CodeSummaryError("duplicate_header", "平安码表头有重复列，请为每个字段保留一列")
                positions[field] = index
    if not REQUIRED_COLUMNS.issubset(positions) or not STATION_COLUMNS.intersection(positions):
        raise CodeSummaryError("missing_header", "请在第一个工作表的首行填写身份证号、比对时间、采集位置、人员类型及机构名称")

    result = []
    for row_number, cells in enumerate(rows, start=2):
        if row_number > MAX_ROWS + 1:
            raise CodeSummaryError("too_many_rows", "每份平安码工作簿最多 10 万行，请按日期拆分")
        if all(cells[index].value is None for index in positions.values()):
            continue
        if any(cells[index].data_type == "f" for index in positions.values()):
            raise CodeSummaryError("formula_cell", f"第 {row_number} 行包含公式，请粘贴为值后重试")
        row = {field: cells[index].value for field, index in positions.items()}
        identity = row["idCard"]
        if not isinstance(identity, str) or not valid_identity(normalize_identity(identity)):
            raise CodeSummaryError("invalid_identity", f"第 {row_number} 行的身份证号无效，请使用文本格式并填写完整号码")
        row["comparisonTime"] = _read_time(row["comparisonTime"], row_number, "比对时间")
        if row.get("updateDate") is not None:
            row["updateDate"] = _read_time(row["updateDate"], row_number, "更新时间")
        if len(str(row["location"] or "").strip()) > 255:
            raise CodeSummaryError("invalid_location", f"第 {row_number} 行的采集位置过长，请缩短到 255 字以内")
        result.append(row)
    if not result:
        raise CodeSummaryError("empty_workbook", "平安码工作簿没有数据行")
    return result


def read_peace_code_workbook(content: bytes) -> list[dict]:
    try:
        with ZipFile(BytesIO(content)) as archive:
            if len(archive.infolist()) > 200 or sum(item.file_size for item in archive.infolist()) > 250 * 1024 * 1024:
                raise CodeSummaryError("workbook_too_large", "工作簿解压后过大，请按日期拆分")
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=False, keep_links=False)
        try:
            if not workbook.worksheets or workbook.worksheets[0].sheet_state != "visible":
                raise CodeSummaryError("missing_sheet", "请把平安码明细放在第一个可见工作表")
            sheet = workbook.worksheets[0]
            sheet.reset_dimensions()
            return _read_rows(sheet)
        finally:
            workbook.close()
    except CodeSummaryError:
        raise
    except (BadZipFile, InvalidFileException, OSError, KeyError, RuntimeError, ValueError, IndexError, TypeError, ParseError):
        raise CodeSummaryError("invalid_workbook", "文件不是可读取的 XLSX 工作簿，请重新另存或导出") from None
