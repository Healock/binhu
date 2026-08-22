#!/usr/bin/env python3
"""Validate pull-request bodies before CI accepts them."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


REQUIRED_HEADINGS = (
    "## PR 摘要",
    "### 解决了哪些问题",
    "### 使用了什么方式",
    "### 数据与安全边界",
    "### 建议怎么验收",
    "### 测试与验证结果",
    "### 风险、回滚与监控",
    "### 后续发展方向",
    "### 暂时无法解决的问题",
    "### 兼容性与发布信息",
)


def validate_body(body: str) -> list[str]:
    errors: list[str] = []
    if "\\n" in body:
        errors.append("PR 正文包含字面量 \\n，请提交真正的换行符。")
    for heading in REQUIRED_HEADINGS:
        if heading not in body:
            errors.append(f"缺少模板章节：{heading}")
    if re.search(r"问题\s*[1-3]：\s*$", body, flags=re.MULTILINE):
        errors.append("仍保留模板中的问题占位项，请填写实际问题。")
    if re.search(r"建议怎么验收[\s\S]*?\n\s*1\.\s*前置条件：\s*\n", body):
        errors.append("验收前置条件尚未填写。")
    return errors


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: validate_pr_body.py <github-event-json>")
    event = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    body = str(((event.get("pull_request") or {}).get("body") or ""))
    errors = validate_body(body)
    if errors:
        for error in errors:
            print(f"::error::{error}")
        return 1
    print("PR body matches the repository template contract.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
