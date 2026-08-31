from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("MYSQL_PASSWORD", "test-password")
os.environ.setdefault("ENCRYPTION_KEY", "test-encryption-key")

from services.help_docs import (
    ensure_help_docs_schema,
    load_builtin_help_documents,
    validate_help_document_update,
)


class HelpDocsCursor:
    def __init__(self, rows: dict[str, dict] | None = None):
        self.rows = rows or {}
        self.executed: list[tuple[str, tuple | None]] = []
        self._result = None

    async def execute(self, sql: str, params=None):
        normalized = " ".join(sql.split())
        self.executed.append((normalized, params))
        if normalized.startswith("CREATE TABLE"):
            self._result = None
        elif normalized.startswith("SELECT builtin_digest"):
            row = self.rows.get(params[0])
            self._result = None if row is None else (
                row["builtin_digest"], row["is_customized"]
            )
        elif normalized.startswith("INSERT INTO _help_documents"):
            slug, title, category, summary, content, order, digest = params
            self.rows[slug] = {
                "title": title,
                "category": category,
                "summary": summary,
                "content_md": content,
                "sort_order": order,
                "builtin_digest": digest,
                "is_customized": False,
                "revision": 1,
            }
        elif normalized.startswith(
            "UPDATE _help_documents SET category=%s,sort_order=%s,builtin_digest=%s"
        ):
            category, order, digest, slug = params
            self.rows[slug].update({
                "category": category,
                "sort_order": order,
                "builtin_digest": digest,
            })
        elif normalized.startswith(
            "UPDATE _help_documents SET title=%s,category=%s,summary=%s,content_md=%s"
        ):
            title, category, summary, content, order, digest, slug = params
            self.rows[slug].update({
                "title": title,
                "category": category,
                "summary": summary,
                "content_md": content,
                "sort_order": order,
                "builtin_digest": digest,
                "revision": self.rows[slug].get("revision", 1) + 1,
            })
        else:
            raise AssertionError(f"unexpected SQL: {normalized}")

    async def fetchone(self):
        return self._result


def test_all_builtin_help_documents_are_publishable():
    documents = load_builtin_help_documents()

    assert len(documents) == 17
    assert len({document.slug for document in documents}) == len(documents)
    assert documents == sorted(documents, key=lambda item: (item.sort_order, item.slug))
    for document in documents:
        assert document.title
        assert document.category
        assert document.summary
        assert document.content_md.startswith("# ")
        assert len(document.digest) == 64


def test_duplicate_slug_is_rejected(tmp_path: Path):
    template = """---
slug: duplicate
title: 标题
category: 分类
summary: 摘要
order: {order}
---
# 标题
"""
    (tmp_path / "01.md").write_text(template.format(order=1), encoding="utf-8")
    (tmp_path / "02.md").write_text(template.format(order=2), encoding="utf-8")

    with pytest.raises(ValueError, match="slug 重复"):
        load_builtin_help_documents(tmp_path)


def test_help_document_update_validation_normalizes_markdown():
    title, summary, content = validate_help_document_update(
        "  新标题  ", "  新摘要  ", "# 标题\r\n\r\n正文  "
    )

    assert title == "新标题"
    assert summary == "新摘要"
    assert content == "# 标题\n\n正文\n"

    with pytest.raises(ValueError, match="正文不能为空"):
        validate_help_document_update("标题", "摘要", "  ")


@pytest.mark.asyncio
async def test_customized_document_keeps_content_when_builtin_changes():
    document = load_builtin_help_documents()[0]
    cursor = HelpDocsCursor({
        document.slug: {
            "title": "管理员标题",
            "category": "旧分类",
            "summary": "管理员摘要",
            "content_md": "# 管理员正文\n",
            "sort_order": 999,
            "builtin_digest": "old-digest",
            "is_customized": True,
            "revision": 7,
        }
    })

    await ensure_help_docs_schema(cursor)

    stored = cursor.rows[document.slug]
    assert stored["title"] == "管理员标题"
    assert stored["summary"] == "管理员摘要"
    assert stored["content_md"] == "# 管理员正文\n"
    assert stored["category"] == document.category
    assert stored["sort_order"] == document.sort_order
    assert stored["builtin_digest"] == document.digest
    assert stored["revision"] == 7


@pytest.mark.asyncio
async def test_non_customized_document_follows_new_builtin_content():
    document = load_builtin_help_documents()[0]
    cursor = HelpDocsCursor({
        document.slug: {
            "title": "旧标题",
            "category": "旧分类",
            "summary": "旧摘要",
            "content_md": "# 旧正文\n",
            "sort_order": 999,
            "builtin_digest": "old-digest",
            "is_customized": False,
            "revision": 2,
        }
    })

    await ensure_help_docs_schema(cursor)

    stored = cursor.rows[document.slug]
    assert stored["title"] == document.title
    assert stored["summary"] == document.summary
    assert stored["content_md"] == document.content_md
    assert stored["builtin_digest"] == document.digest
    assert stored["revision"] == 3
