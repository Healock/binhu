"""Small, dependency-light helpers for permission-checked XLSX exports."""

from __future__ import annotations

from io import BytesIO
from typing import Iterable, Sequence

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font


XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _safe_cell_value(value: object) -> object:
    """Keep user-controlled text from being interpreted as an Excel formula."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def build_xlsx(
    title: str,
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
) -> BytesIO:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = title[:31] or "导出"
    sheet.append([_safe_cell_value(value) for value in headers])
    for cell in sheet[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    for row in rows:
        sheet.append([
            "" if value is None else _safe_cell_value(value)
            for value in row
        ])
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for column in sheet.columns:
        letter = column[0].column_letter
        width = min(max(max((len(str(cell.value or "")) for cell in column), default=8) + 2, 10), 48)
        sheet.column_dimensions[letter].width = width
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    return output
