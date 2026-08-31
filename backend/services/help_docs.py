"""Built-in Markdown help documents and persistent customization support."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
import re


HELP_DOCS_DIR = Path(__file__).resolve().parent.parent / "help_docs"
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class BuiltinHelpDocument:
    slug: str
    title: str
    category: str
    summary: str
    sort_order: int
    content_md: str
    digest: str


def _parse_front_matter(text: str, filename: str) -> tuple[dict[str, str], str]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise ValueError(f"{filename} 缺少 Markdown front matter")
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise ValueError(f"{filename} 的 Markdown front matter 未闭合")
    metadata: dict[str, str] = {}
    for line in normalized[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError(f"{filename} 的元数据行格式错误: {line}")
        metadata[key.strip()] = value.strip().strip('"').strip("'")
    return metadata, normalized[end + 5 :].strip() + "\n"


def load_builtin_help_documents(
    directory: Path = HELP_DOCS_DIR,
) -> list[BuiltinHelpDocument]:
    documents: list[BuiltinHelpDocument] = []
    seen_slugs: set[str] = set()
    if not directory.is_dir():
        raise RuntimeError(f"帮助文档目录不存在: {directory}")
    for path in sorted(directory.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        metadata, content = _parse_front_matter(raw, path.name)
        missing = [
            key for key in ("slug", "title", "category", "summary", "order")
            if not metadata.get(key)
        ]
        if missing:
            raise ValueError(f"{path.name} 缺少元数据: {', '.join(missing)}")
        slug = metadata["slug"]
        if not SLUG_PATTERN.fullmatch(slug):
            raise ValueError(f"{path.name} 的 slug 不合法: {slug}")
        if slug in seen_slugs:
            raise ValueError(f"帮助文档 slug 重复: {slug}")
        seen_slugs.add(slug)
        try:
            sort_order = int(metadata["order"])
        except ValueError as exc:
            raise ValueError(f"{path.name} 的 order 必须是整数") from exc
        if not content.startswith("# "):
            raise ValueError(f"{path.name} 正文必须以一级标题开始")
        digest = sha256(raw.replace("\r\n", "\n").encode("utf-8")).hexdigest()
        documents.append(BuiltinHelpDocument(
            slug=slug,
            title=metadata["title"][:160],
            category=metadata["category"][:100],
            summary=metadata["summary"][:500],
            sort_order=sort_order,
            content_md=content,
            digest=digest,
        ))
    if not documents:
        raise RuntimeError("帮助文档目录中没有可发布的 Markdown 文件")
    return sorted(documents, key=lambda item: (item.sort_order, item.slug))


async def ensure_help_docs_schema(cur) -> None:
    """Create the catalog and seed/update non-customized built-in documents."""
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _help_documents (
            slug VARCHAR(100) PRIMARY KEY,
            title VARCHAR(160) NOT NULL,
            category VARCHAR(100) NOT NULL,
            summary VARCHAR(500) NOT NULL DEFAULT '',
            content_md MEDIUMTEXT NOT NULL,
            sort_order INT NOT NULL DEFAULT 0,
            revision INT UNSIGNED NOT NULL DEFAULT 1,
            is_customized TINYINT(1) NOT NULL DEFAULT 0,
            builtin_digest CHAR(64) NOT NULL,
            updated_by INT DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_help_category_order (category, sort_order, slug),
            INDEX idx_help_active_order (sort_order, slug)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    for document in load_builtin_help_documents():
        await cur.execute(
            "SELECT builtin_digest, is_customized FROM _help_documents WHERE slug=%s",
            (document.slug,),
        )
        existing = await cur.fetchone()
        if not existing:
            await cur.execute(
                "INSERT INTO _help_documents "
                "(slug,title,category,summary,content_md,sort_order,builtin_digest) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    document.slug, document.title, document.category,
                    document.summary, document.content_md,
                    document.sort_order, document.digest,
                ),
            )
            continue
        old_digest, is_customized = str(existing[0] or ""), bool(existing[1])
        if is_customized:
            if old_digest != document.digest:
                await cur.execute(
                    "UPDATE _help_documents SET category=%s,sort_order=%s,"
                    "builtin_digest=%s WHERE slug=%s",
                    (
                        document.category, document.sort_order,
                        document.digest, document.slug,
                    ),
                )
            continue
        if old_digest != document.digest:
            await cur.execute(
                "UPDATE _help_documents SET title=%s,category=%s,summary=%s,"
                "content_md=%s,sort_order=%s,builtin_digest=%s,revision=revision+1,"
                "updated_by=NULL WHERE slug=%s",
                (
                    document.title, document.category, document.summary,
                    document.content_md, document.sort_order,
                    document.digest, document.slug,
                ),
            )


def builtin_help_document(slug: str) -> BuiltinHelpDocument | None:
    return next((item for item in load_builtin_help_documents() if item.slug == slug), None)


def validate_help_document_update(
    title: str,
    summary: str,
    content_md: str,
) -> tuple[str, str, str]:
    normalized_title = title.strip()
    normalized_summary = summary.strip()
    normalized_content = content_md.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized_title:
        raise ValueError("文档标题不能为空")
    if len(normalized_title) > 160:
        raise ValueError("文档标题不能超过 160 个字符")
    if len(normalized_summary) > 500:
        raise ValueError("文档摘要不能超过 500 个字符")
    if not normalized_content:
        raise ValueError("Markdown 正文不能为空")
    if len(normalized_content) > 200_000:
        raise ValueError("Markdown 正文不能超过 20 万个字符")
    return normalized_title, normalized_summary, normalized_content + "\n"
