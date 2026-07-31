"""把工作日志草稿渲染为固定 A4 PDF。"""

from __future__ import annotations

from datetime import date, timedelta
from html import escape
import re
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
    if value:
        return (
            f'<span class="blank blank-filled" '
            f'title="{escape(definition["label"])}">{escape(value)}</span>'
        )
    return (
        f'<span class="blank blank-empty" style="min-width:{width}px" '
        f'title="{escape(definition["label"])}">&nbsp;</span>'
    )


def _sentence_inner(block: dict, values: dict) -> str:
    parts: list[str] = []
    for segment in block["segments"]:
        if isinstance(segment, str):
            parts.append(escape(segment))
        else:
            parts.append(_input_span(segment, values))
    return "".join(parts)


def _sentence_html(
    block: dict,
    values: dict,
    *,
    prefix: str = "",
) -> str:
    style = escape(block.get("style", ""))
    classes = f"sentence {style}".strip()
    return f'<p class="{classes}">{prefix}{_sentence_inner(block, values)}</p>'


def _analysis_html(blocks: list[dict], values: dict) -> str:
    parts = []
    for block in blocks:
        content = _sentence_inner(block, values)
        parts.append(content[:-1] if content.endswith("。") else content)
    return (
        '<p class="sentence analysis">'
        '<strong>问题分析：</strong>'
        f'{"；".join(parts)}。</p>'
    )


def _subheading_title(title: str) -> str:
    return re.sub(r"^(\d+)\.\s*", r"\1、", title)


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
    return (
        '<div class="table-block">'
        '<table class="report-table">'
        f"{colgroup}"
        f"{_table_header(columns)}"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table></div>"
    )


def _section_html(section: dict, values: dict) -> str:
    blocks = []
    if section["id"] in {"notices", "special"}:
        title = section["title"].split("、", 1)[-1]
        blocks.append(
            f'<h1 class="division-title">{escape(title)}</h1>'
        )
    else:
        blocks.append(
            f'<h1 class="section-title">{escape(section["title"])}</h1>'
        )

    source_blocks = section["blocks"]
    index = 0
    while index < len(source_blocks):
        block = source_blocks[index]
        block_type = block["type"]
        if block_type == "heading":
            if block["title"] == "基础数据":
                index += 1
                continue
            title = _subheading_title(block["title"])
            if (
                block.get("combine_with_next", True)
                and index + 1 < len(source_blocks)
                and source_blocks[index + 1]["type"] == "sentence"
            ):
                sentence_block = source_blocks[index + 1]
                prefix = (
                    '<strong class="subsection-label">'
                    f"{escape(title)}：</strong>"
                )
                blocks.append(
                    _sentence_html(sentence_block, values, prefix=prefix)
                )
                index += 2
                continue
            blocks.append(
                '<p class="subsection-heading">'
                f"<strong>{escape(title)}</strong></p>"
            )
        elif block_type == "sentence":
            if block.get("style") == "analysis":
                analysis_blocks = [block]
                next_index = index + 1
                while (
                    next_index < len(source_blocks)
                    and source_blocks[next_index]["type"] == "sentence"
                    and source_blocks[next_index].get("style") == "analysis"
                ):
                    analysis_blocks.append(source_blocks[next_index])
                    next_index += 1
                blocks.append(_analysis_html(analysis_blocks, values))
                index = next_index
                continue
            blocks.append(_sentence_html(block, values))
        elif block_type == "textarea":
            definition = block["field"]
            content = _display(values.get(definition["id"]), definition)
            displayed = (
                escape(content).replace(chr(10), "<br>")
                if content
                else '<span class="blank blank-empty long-blank">&nbsp;</span>'
            )
            if "问题分析" in definition["label"]:
                blocks.append(
                    '<p class="sentence analysis">'
                    f"<strong>问题分析：</strong>{displayed}</p>"
                )
            else:
                blocks.append(
                    '<p class="sentence long-text">'
                    f'<strong>{escape(definition["label"])}：</strong>'
                    f"{displayed}</p>"
                )
        elif block_type == "table":
            blocks.append(_table_html(block, values))
        index += 1
    return (
        '<section class="report-section">'
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
  margin: 37mm 27mm 35mm;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  color: #111;
  font-family: "方正仿宋_GBK", "FangSong", "仿宋",
    "Noto Serif CJK SC", "Noto Serif CJK", serif;
  font-size: 14pt;
  line-height: 22pt;
}}
.document-head {{
  margin: 0;
  border-bottom: 1.5pt solid #ff0000;
}}
.document-title {{
  margin: 0;
  text-align: center;
  font-size: 22pt;
  font-weight: 400;
  line-height: 25pt;
  color: #ff0000;
  font-family: "方正小标宋_GBK", "FZXiaoBiaoSong-B05S",
    "Noto Serif CJK SC", "Noto Serif CJK", serif;
}}
.document-number {{
  margin: 0;
  text-align: center;
  font-size: 16pt;
  font-weight: 700;
  line-height: 25pt;
}}
.document-meta {{
  display: flex;
  justify-content: space-between;
  margin: 0;
  padding: 0 1mm;
  font-size: 14pt;
  line-height: 25pt;
}}
.basic-data-title,
.division-title {{
  margin: 0;
  text-align: center;
  font-size: 16pt;
  font-weight: 700;
  line-height: 26pt;
}}
.section-title {{
  margin: 0;
  font-size: 14pt;
  font-weight: 700;
  line-height: 26pt;
  break-after: avoid;
}}
.division-title {{
  break-after: avoid;
}}
.sentence,
.subsection-heading {{
  margin: 0;
  text-indent: 2em;
  text-align: justify;
  font-size: 14pt;
  line-height: 22pt;
  orphans: 2;
  widows: 2;
}}
.subsection-heading {{
  break-after: avoid;
}}
.subsection-label {{
  font-weight: 700;
}}
.analysis {{
  color: #ff0000;
}}
.strong {{
  font-weight: 700;
}}
.blank {{
  text-indent: 0;
}}
.blank-filled {{
  display: inline;
}}
.blank-empty {{
  display: inline-block;
  margin: 0 .6mm;
  padding: 0 .5mm;
  border-bottom: .35mm solid #222;
  text-align: center;
  line-height: 18pt;
  overflow-wrap: anywhere;
}}
.table-block {{
  margin: 1.5mm 0 3mm;
}}
.report-table {{
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
  font-family: "SimSun", "宋体", "Noto Serif CJK SC",
    "Noto Serif CJK", serif;
  font-size: 7.2pt;
  line-height: 1.15;
}}
.report-table thead {{
  display: table-header-group;
}}
.report-table tr {{
  break-inside: avoid;
}}
.report-table th,
.report-table td {{
  height: 7mm;
  padding: .7mm .45mm;
  border: .3mm solid #111;
  text-align: center;
  vertical-align: middle;
  overflow-wrap: anywhere;
}}
.report-table th {{
  background: #fff;
  font-weight: 700;
}}
.report-table td.cell-textarea {{
  text-align: left;
}}
.long-text {{
  text-align: left;
}}
.long-blank {{
  min-width: 68%;
}}
.issue-date {{
  margin-top: 6mm;
  text-align: right;
  font-size: 14pt;
  line-height: 22pt;
}}
</style>
</head>
<body>
  <header class="document-head">
    <div class="document-title">{escape(schema["document_title"])}</div>
    <div class="document-number">一</div>
    <div class="document-meta">
      <span>防控治理岗</span>
      <span>{business_date.year} 年 {business_date.month} 月
        {business_date.day} 日</span>
    </div>
  </header>
  <div class="basic-data-title">基础数据</div>
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
