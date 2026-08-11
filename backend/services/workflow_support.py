"""工单通知、附件保存、清理和跨域辅助函数。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import uuid

from config import settings


MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS_PER_TICKET = 10
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".pdf"}
SAFE_FILE_ID = re.compile(r"^[0-9a-f-]{36}$")


def platform_schema() -> str:
    return settings.MYSQL_PLATFORM_DB if settings.PLATFORM_DOMAIN_ACTIVE else settings.MYSQL_ONLINE_DATA_DB


def workflow_can_view_all(user: dict) -> bool:
    permissions = set(user.get("permissions") or [])
    position = str((user.get("member") or {}).get("position") or "")
    scope = (user.get("permission_scopes") or {}).get(
        "workflow.ticket.view",
        user.get("data_scope"),
    )
    return "workflow.ticket.manage" in permissions or (
        scope == "all" and position in {"基础管控", "中队长", "所队领导"}
    )


def workflow_community_scope(user: dict, order_alias: str = "work_orders") -> tuple[str, list[str]] | None:
    """组长可见本社区申请；其他普通账号只看直接参与工单。"""
    position = str((user.get("member") or {}).get("position") or "")
    if position != "组长":
        return None
    communities = [
        str(item).strip()
        for item in user.get("community_names") or []
        if str(item).strip()
    ]
    if not communities:
        return None
    schema = platform_schema().replace("`", "")
    placeholders = ",".join(["%s"] * len(communities))
    sql = (
        "EXISTS (SELECT 1 "
        f"FROM `{schema}`._users scope_user "
        f"JOIN `{schema}`._grid_member_department_links scope_link "
        "ON scope_link.member_id=scope_user.member_id "
        f"JOIN `{schema}`._departments scope_department "
        "ON scope_department.id=scope_link.department_id "
        f"JOIN `{schema}`._communities scope_community "
        "ON scope_community.id=scope_department.community_id "
        f"WHERE scope_user.id={order_alias}.requester_user_id "
        f"AND scope_community.name IN ({placeholders}))"
    )
    return sql, communities


def detect_attachment_mime(content: bytes, extension: str) -> str:
    extension = extension.lower()
    if content.startswith(b"%PDF-"):
        detected = "application/pdf"
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif len(content) >= 12 and content[:4] in {b"RIFF"} and content[8:12] == b"WEBP":
        detected = "image/webp"
    elif len(content) >= 12 and content[4:8] == b"ftyp" and (
        b"heic" in content[8:32] or b"heix" in content[8:32] or b"mif1" in content[8:32]
    ):
        detected = "image/heic"
    else:
        raise ValueError("附件实际格式不受支持")
    allowed_by_extension = {
        ".pdf": {"application/pdf"},
        ".png": {"image/png"},
        ".jpg": {"image/jpeg"},
        ".jpeg": {"image/jpeg"},
        ".webp": {"image/webp"},
        ".heic": {"image/heic"},
    }
    if detected not in allowed_by_extension.get(extension, set()):
        raise ValueError("附件扩展名与实际格式不一致")
    return detected


def save_attachment(ticket_id: int, original_name: str, content: bytes) -> dict:
    if not content or len(content) > MAX_ATTACHMENT_BYTES:
        raise ValueError("单个附件必须小于或等于 20MB")
    extension = Path(original_name or "").suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        raise ValueError("仅支持 JPG、PNG、WebP、HEIC 和 PDF")
    mime_type = detect_attachment_mime(content, extension)
    file_id = str(uuid.uuid4())
    root = Path(settings.WORKFLOW_ATTACHMENT_DIR).resolve()
    ticket_dir = (root / str(ticket_id)).resolve()
    if ticket_dir.parent != root:
        raise ValueError("附件目录无效")
    ticket_dir.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
        ticket_dir.chmod(0o700)
    except OSError:
        pass
    storage_name = f"{file_id}{extension}"
    target = (ticket_dir / storage_name).resolve()
    if target.parent != ticket_dir:
        raise ValueError("附件路径无效")
    temporary = ticket_dir / f".{storage_name}.partial"
    digest = hashlib.sha256(content).hexdigest()
    try:
        with temporary.open("xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        try:
            target.chmod(0o600)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "file_id": file_id,
        "storage_key": f"{ticket_id}/{storage_name}",
        "mime_type": mime_type,
        "size_bytes": len(content),
        "sha256": digest,
    }


def resolve_attachment(storage_key: str) -> Path:
    root = Path(settings.WORKFLOW_ATTACHMENT_DIR).resolve()
    target = (root / storage_key).resolve()
    if root not in target.parents or not target.is_file() or target.is_symlink():
        raise FileNotFoundError("附件文件不存在")
    return target


def remove_attachment(storage_key: str) -> bool:
    try:
        target = resolve_attachment(storage_key)
    except FileNotFoundError:
        return False
    target.unlink()
    return True


async def workflow_notification(
    cur,
    *,
    user_ids: list[int],
    ticket_id: int,
    event_key: str,
    title: str,
    content: str,
    severity: str = "info",
) -> None:
    schema = platform_schema().replace("`", "")
    category = f"workflow_{event_key}"[:30]
    safe_title = title[:100]
    safe_content = content[:1000]
    normalized_ids: set[int] = set()
    for value in user_ids:
        if value in (None, ""):
            continue
        try:
            user_id = int(value)
        except (TypeError, ValueError):
            continue
        if user_id > 0:
            normalized_ids.add(user_id)
    for user_id in sorted(normalized_ids):
        await cur.execute(
            f"INSERT IGNORE INTO `{schema}`._notifications "
            "(user_id, category, severity, title, content, related_task_id, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP())",
            (user_id, category, severity, safe_title, safe_content, ticket_id),
        )


async def queue_user_ids(cur, queue: str) -> list[int]:
    schema = platform_schema().replace("`", "")
    await cur.execute(
        f"SELECT DISTINCT user.id FROM `{schema}`._users user "
        f"JOIN `{schema}`._grid_members member ON member.id=user.member_id "
        "WHERE member.position=%s AND member.status='在岗'",
        (queue,),
    )
    return [int(row[0]) for row in await cur.fetchall()]
