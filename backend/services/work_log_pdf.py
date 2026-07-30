"""把工作日志草稿渲染为固定 A4 PDF。"""

from __future__ import annotations

from datetime import date, timedelta
from html import escape
from typing import Any

from services.work_log_schema import leaf_columns


def _display(value: Any, definition: dict | None = None) -> str:
    if value is None or value == "":
        return ""
    field_type = (definition or {}).get("type", "text")
    if field_type == "percent":
        try:
            return f"{float(value):g}%"
        except (TypeError, ValueError):
            return str(value)
    if field_type in {"number", "decimal"}:
        try:
            return f"{float(value):g}"
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _input_span(definition: dict, values: dict) -> str:
    value = _display(values.get(definition["id"]), definition)
    width = max(42, min(int(definition.get("width", 88)), 280))
    text = escape(value) if value else "&nbsp;"
    return (
        f'<span class="blank" style="min-width:{width}px" '
        f'title="{escape(definition["label"])}">{text}</span>'
    )


def _sentence_html(block: dict, values: dict) -> str:
    parts: list[str] = []
    for segment in block["segments"]:
        if isinstance(segment, str):
            parts.append(escape(segment))
        else:
            parts.append(_input_span(segment, values))
    return f'<p class="sentence">{"".join(parts)}</p>'


def _table_header(columns: list[dict]) -> str:
    has_groups = any(item.get("children") for item in columns)
    if not has_groups:
        return (
            "<thead><tr>"
            + "".join(
                f"<th>{escape(item['label'])}</th>"
                for item in columns
            )
            + "</tr></thead>"
        )
    first = []
    second = []
    for item in columns:
        children = item.get("children")
        if children:
            first.append(
                f'<th colspan="{len(leaf_columns(children))}">'
                f"{escape(item['label'])}</th>"
            )
            second.extend(
                f"<th>{escape(child['label'])}</th>"
                for child in leaf_columns(children)
            )
        else:
            first.append(
                f'<th rowspan="2">{escape(item["label"])}</th>'
            )
    return (
        f"<thead><tr>{''.join(first)}</tr>"
        f"<tr>{''.join(second)}</tr></thead>"
    )


def _table_html(block: dict, values: dict) -> str:
    definition = block["field"]
    columns = definition["columns"]
    leaves = leaf_columns(columns)
    total_width = sum(int(item.get("width", 96)) for item in leaves) or 1
    colgroup = "<colgroup>" + "".join(
        f'<col style="width:{int(item.get("width", 96)) * 100 / total_width:.3f}%">'
        for item in leaves
    ) + "</colgroup>"
    rows = values.get(definition["id"])
    rows = rows if isinstance(rows, list) and rows else [{}]
    body_rows = []
    for row in rows:
        cells = []
        for item in leaves:
            value = _display(row.get(item["key"]), item)
            cells.append(
                f'<td class="cell-{escape(item.get("type", "text"))}">'
                f"{escape(value).replace(chr(10), '<br>')}</td>"
            )
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    help_text = (
        f'<div class="table-help">{escape(block["help"])}</div>'
        if block.get("help")
        else ""
    )
    return (
        '<div class="table-block">'
        f'<div class="table-title">{escape(definition["label"])}</div>'
        f"{help_text}"
        '<table class="report-table">'
        f"{colgroup}"
        f"{_table_header(columns)}"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div>"
    )


def _section_html(section: dict, values: dict) -> str:
    blocks = []
    for block in section["blocks"]:
        block_type = block["type"]
        if block_type == "heading":
            level = 3 if block.get("level", 2) >= 3 else 2
            blocks.append(
                f'<h{level}>{escape(block["title"])}</h{level}>'
            )
        elif block_type == "sentence":
            blocks.append(_sentence_html(block, values))
        elif block_type == "textarea":
            definition = block["field"]
            content = _display(values.get(definition["id"]), definition)
            blocks.append(
                '<div class="long-text">'
                f'<div class="long-text-title">{escape(definition["label"])}</div>'
                f'<div class="long-text-content">'
                f'{escape(content).replace(chr(10), "<br>") or "&nbsp;"}</div>'
                "</div>"
            )
        elif block_type == "table":
            blocks.append(_table_html(block, values))
    return (
        '<section class="report-section">'
        f'<h1 class="section-title">{escape(section["title"])}</h1>'
        f'{"".join(blocks)}</section>'
    )


def build_daily_pdf(
    draft: dict,
    schema: dict,
    values: dict,
) -> tuple[bytes, str]:
    try:
        from weasyprint import HTML
    except ImportError as exc:
        raise RuntimeError("PDF 生成组件尚未安装") from exc

    business_date = date.fromisoformat(draft["business_date"])
    issue_date = business_date + timedelta(days=1)
    sections = "".join(
        _section_html(section, values)
        for section in schema["sections"]
    )
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{escape(schema["document_title"])}</title>
<style>
@page {{
  size: A4;
  margin: 22mm 18mm 20mm;
  @bottom-center {{
    content: counter(page);
    font-size: 9pt;
    color: #555;
  }}
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  color: #111;
  font-family: "Noto Serif CJK SC", "Noto Serif CJK", "SimSun", serif;
  font-size: 12pt;
  line-height: 1.75;
}}
.document-title {{
  margin: 0 0 8mm;
  text-align: center;
  font-size: 22pt;
  font-weight: 700;
  letter-spacing: 1.5pt;
}}
.document-meta {{
  margin-bottom: 8mm;
  text-align: center;
  font-size: 13pt;
}}
.section-title {{
  margin: 7mm 0 3mm;
  font-size: 16pt;
  line-height: 1.35;
}}
h2 {{
  margin: 4mm 0 2mm;
  font-size: 14pt;
}}
h3 {{
  margin: 3mm 0 1.5mm;
  font-size: 12.5pt;
}}
.sentence {{
  margin: 1.5mm 0;
  text-indent: 2em;
  text-align: justify;
}}
.blank {{
  display: inline-block;
  margin: 0 1.5mm;
  padding: 0 .8mm;
  border-bottom: .35mm solid #222;
  text-align: center;
  text-indent: 0;
  line-height: 1.35;
  overflow-wrap: anywhere;
}}
.table-block {{
  margin: 3mm 0 5mm;
}}
.table-title {{
  margin-bottom: 1.5mm;
  text-align: center;
  font-weight: 700;
  font-size: 11pt;
}}
.table-help {{
  margin-bottom: 1.2mm;
  color: #555;
  font-size: 8.5pt;
}}
.report-table {{
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  font-family: "Noto Sans CJK SC", "Noto Sans CJK", "Microsoft YaHei", sans-serif;
  font-size: 7.6pt;
  line-height: 1.25;
}}
.report-table thead {{
  display: table-header-group;
}}
.report-table tr {{
  break-inside: avoid;
}}
.report-table th,
.report-table td {{
  padding: 1.2mm .8mm;
  border: .25mm solid #333;
  text-align: center;
  vertical-align: middle;
  overflow-wrap: anywhere;
}}
.report-table th {{
  background: #eef2f6;
  font-weight: 700;
}}
.report-table td.cell-textarea {{
  text-align: left;
}}
.long-text {{
  margin: 2mm 0 4mm;
}}
.long-text-title {{
  margin-bottom: 1mm;
  font-weight: 700;
}}
.long-text-content {{
  min-height: 16mm;
  padding: 2mm;
  border: .25mm solid #333;
  white-space: normal;
  overflow-wrap: anywhere;
}}
.issue-date {{
  margin-top: 8mm;
  text-align: right;
}}
</style>
</head>
<body>
  <div class="document-title">{escape(schema["document_title"])}</div>
  <div class="document-meta">防控治理岗&nbsp;&nbsp;
    {business_date.year}年{business_date.month}月{business_date.day}日
  </div>
  {sections}
  <div class="issue-date">滨湖新城派出所&nbsp;&nbsp;
    {issue_date.year}年{issue_date.month}月{issue_date.day}日
  </div>
</body>
</html>"""
    content = HTML(string=html).write_pdf()
    filename = (
        f"{business_date.strftime('%m%d')}"
        "日报滨湖新城派出所社区警务工作日志.pdf"
    )
    return content, filename
