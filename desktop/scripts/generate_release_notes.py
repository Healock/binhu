#!/usr/bin/env python3
"""Generate safe, human-readable release notes from merged pull requests."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any


SECTION_RE = re.compile(
    r"^#{1,6}\s*(?:解决了哪些问题|解决的问题|问题修复|修改范围|总结|summary|what changed|changes)\s*$",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^#{1,6}\s+")
SENSITIVE_RE = re.compile(
    r"(?<!\d)(?:\d{18}|\d{17}[0-9Xx]|1\d{10}|\d{3}-\d{4}-\d{4})(?!\d)"
)
URL_RE = re.compile(r"https?://[^\s)]+")


def _normalize_body_newlines(value: str) -> str:
    """Recover line breaks accidentally submitted as literal backslash escapes.

    GitHub's API normally gives us real newlines, but callers that pass a JSON
    encoded body twice can store ``\\n`` literally.  Normalize only when the
    body contains no meaningful line breaks, so an intentional ``\\n`` inside
    a code sample is not rewritten.
    """
    if "\\n" not in value or "\n" in value.strip():
        return value
    return value.replace("\\r\\n", "\n").replace("\\n", "\n")


def _strip_markdown_links(value: str) -> str:
    """Keep link labels while removing destinations from release summaries."""
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", value)


def _safe_text(value: str, limit: int) -> str:
    value = re.sub(r"```.*?```", "", value, flags=re.DOTALL)
    value = URL_RE.sub("外部链接", value)
    value = SENSITIVE_RE.sub("[已省略敏感值]", value)
    value = _strip_markdown_links(value)
    lines = [re.sub(r"\s+", " ", line).strip(" -*\t") for line in value.splitlines()]
    text = " ".join(line for line in lines if line)
    return text[:limit].rstrip() + ("…" if len(text) > limit else "")


def summarize_body(body: str) -> str:
    body = _normalize_body_newlines(body)
    lines = body.splitlines()
    selected: list[str] = []
    in_section = False
    for line in lines:
        if SECTION_RE.match(line.strip()):
            in_section = True
            continue
        if in_section and HEADING_RE.match(line.strip()):
            break
        if in_section:
            selected.append(line)
    if selected:
        return _safe_text("\n".join(selected), 420)
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    return _safe_text(paragraphs[0] if paragraphs else "已合并功能与修复。", 300)


def parse_release_markdown(markdown: str) -> list[dict[str, Any]]:
    """Parse the one release-level changelog authored by Codex.

    The source is intentionally separate from individual PR bodies.  It may
    use either numbered topic lines (``1. 主题``) or Markdown headings, with
    ``-`` bullets beneath each topic.
    """
    markdown = _normalize_body_newlines(markdown)
    markdown = re.sub(r"<!--.*?-->", "", markdown, flags=re.DOTALL)
    sections: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        numbered = re.match(r"^\d+[.)、]\s*(.+?)\s*$", line)
        heading = re.match(r"^#{2,4}\s+(?:\d+[.)、]\s*)?(.+?)\s*$", line)
        topic = numbered or heading
        if topic:
            title = _safe_text(topic.group(1), 120)
            if title and title not in {"更新内容", "更新说明", "技术追溯"}:
                current = {"title": title, "items": [], "pullRequests": []}
                sections.append(current)
            else:
                current = None
            continue
        if current is None:
            continue
        item = re.sub(r"^[-*+]\s+", "", line)
        item = re.sub(r"^\[[ xX]\]\s*", "", item)
        if item:
            safe_item = _safe_text(item, 320)
            if safe_item and safe_item not in current["items"]:
                current["items"].append(safe_item)
    return [section for section in sections if section["items"]]


def fallback_user_sections(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep old releases publishable when no release draft was supplied."""
    if not prs:
        return []
    return [{
        "title": "其他更新",
        "items": [f"#{pr['number']} {pr['title']}：{pr['summary']}" for pr in prs],
        "pullRequests": [pr["number"] for pr in prs],
    }]


def commit_range(previous_tag: str | None, commit: str) -> set[str]:
    if not previous_tag:
        args = ["git", "rev-list", commit]
    else:
        args = ["git", "rev-list", f"{previous_tag}..{commit}"]
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def normalize_prs(raw: Any, commits: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("PR JSON must be an array")
    result = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        merge_commit = item.get("mergeCommit") or {}
        oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
        if oid and oid not in commits:
            continue
        if not oid:
            continue
        number = item.get("number")
        title = str(item.get("title") or "未命名变更").strip()
        if isinstance(number, int):
            result.append({
                "number": number,
                "title": title,
                "summary": summarize_body(str(item.get("body") or "")),
                "merge_commit": oid,
            })
    result.sort(key=lambda item: item["number"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--previous-tag", default="")
    parser.add_argument("--prs-json", type=Path, required=True)
    parser.add_argument("--curated-markdown", type=Path)
    parser.add_argument("--output-markdown", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()

    raw = json.loads(args.prs_json.read_text(encoding="utf-8"))
    prs = normalize_prs(raw, commit_range(args.previous_tag or None, args.commit))
    curated_markdown = ""
    if args.curated_markdown and args.curated_markdown.is_file():
        curated_markdown = args.curated_markdown.read_text(encoding="utf-8")
    user_sections = parse_release_markdown(curated_markdown) or fallback_user_sections(prs)
    public_prs = [
        {"number": item["number"], "title": item["title"], "summary": item["summary"]}
        for item in prs
    ]
    previous_version = args.previous_tag.removeprefix("v") if args.previous_tag else None
    payload = {
        "schemaVersion": 2,
        "version": args.version,
        "previousVersion": previous_version,
        "commit": args.commit,
        "pullRequests": public_prs,
        "sections": user_sections,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        f"# 滨湖桌面客户端 v{args.version} 更新日志",
        "",
        f"本次版本由提交 `{args.commit[:12]}` 生成。",
        "",
    ]
    if previous_version:
        lines.append(f"更新范围：v{previous_version} → v{args.version}。")
        lines.append("")
    if user_sections:
        lines.extend(["## 更新内容", ""])
        for index, section in enumerate(user_sections, start=1):
            lines.append(f"### {index}. {section['title']}")
            lines.extend(f"- {item}" for item in section["items"])
            lines.append("")
    else:
        lines.extend(["## 更新内容", "", "本次版本没有检测到新的更新说明。", ""])
    lines.extend(["## 技术追溯", ""])
    if prs:
        for item in prs:
            lines.append(f"- [#{item['number']}] {item['title']}：{item['summary']}")
    else:
        lines.append("本次版本没有检测到新的合并 PR。")
    lines.extend([
        "",
        "## 更新说明",
        "",
        "桌面客户端优先使用更新服务器提供的增量包；增量包不可用时自动回退到全量包。",
    ])
    args.output_markdown.parent.mkdir(parents=True, exist_ok=True)
    args.output_markdown.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
