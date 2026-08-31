"""Authenticated help center with super-administrator Markdown editing."""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from database import get_db
from deps import get_current_user, require_super_admin
from services.audit import record_admin_audit, request_audit_fields
from services.help_docs import builtin_help_document, validate_help_document_update
from services.maintenance import is_super_admin_user


router = APIRouter(prefix="/api/help", tags=["帮助中心"])


class HelpDocumentUpdate(BaseModel):
    title: str = Field(max_length=160)
    summary: str = Field(default="", max_length=500)
    content_md: str = Field(max_length=200_000)
    expected_revision: int = Field(ge=1)


class HelpDocumentReset(BaseModel):
    expected_revision: int = Field(ge=1)


def _serialize_document(row, *, include_content: bool, can_edit: bool) -> dict:
    data = {
        "slug": str(row[0]),
        "title": str(row[1]),
        "category": str(row[2]),
        "summary": str(row[3] or ""),
        "sort_order": int(row[5]),
        "revision": int(row[6]),
        "is_customized": bool(row[7]),
        "updated_at": row[8].isoformat() if row[8] else None,
        "can_edit": can_edit,
    }
    if include_content:
        data["content_md"] = str(row[4] or "")
    return data


async def _read_help_document(conn, slug: str, *, can_edit: bool) -> dict:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT slug,title,category,summary,content_md,sort_order,revision,"
            "is_customized,updated_at FROM _help_documents WHERE slug=%s",
            (slug,),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="帮助文档不存在")
    return _serialize_document(row, include_content=True, can_edit=can_edit)


async def _raise_revision_conflict(cur, slug: str) -> None:
    await cur.execute("SELECT revision FROM _help_documents WHERE slug=%s", (slug,))
    current = await cur.fetchone()
    if not current:
        raise HTTPException(status_code=404, detail="帮助文档不存在")
    raise HTTPException(
        status_code=409,
        detail={
            "message": "帮助文档已被其他管理员更新，请刷新后重试",
            "revision": int(current[0]),
        },
    )


@router.get("/documents")
async def list_help_documents(
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT slug,title,category,summary,content_md,sort_order,revision,"
            "is_customized,updated_at FROM _help_documents "
            "ORDER BY sort_order,slug"
        )
        rows = await cur.fetchall()
    can_edit = is_super_admin_user(user)
    return {
        "data": [
            _serialize_document(row, include_content=False, can_edit=can_edit)
            for row in rows
        ],
        "can_edit": can_edit,
    }


@router.get("/documents/{slug}")
async def get_help_document(
    slug: str,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    return await _read_help_document(
        conn, slug, can_edit=is_super_admin_user(user)
    )


@router.put("/documents/{slug}")
async def update_help_document(
    slug: str,
    payload: HelpDocumentUpdate,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    try:
        title, summary, content_md = validate_help_document_update(
            payload.title, payload.summary, payload.content_md
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE _help_documents SET title=%s,summary=%s,content_md=%s,"
            "is_customized=1,updated_by=%s,revision=revision+1 "
            "WHERE slug=%s AND revision=%s",
            (title, summary, content_md, user["id"], slug, payload.expected_revision),
        )
        if cur.rowcount != 1:
            await _raise_revision_conflict(cur, slug)
    await record_admin_audit(
        user,
        "help_document.update",
        target_type="help_document",
        target_name=slug,
        detail={
            "revision": payload.expected_revision + 1,
            "title_length": len(title),
            "summary_length": len(summary),
            "content_length": len(content_md),
        },
        **request_audit_fields(request),
    )
    return await _read_help_document(conn, slug, can_edit=True)


@router.post("/documents/{slug}/reset")
async def reset_help_document(
    slug: str,
    payload: HelpDocumentReset,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    document = builtin_help_document(slug)
    if not document:
        raise HTTPException(status_code=404, detail="内置帮助文档不存在")
    async with conn.cursor() as cur:
        await cur.execute(
            "UPDATE _help_documents SET title=%s,category=%s,summary=%s,content_md=%s,"
            "sort_order=%s,builtin_digest=%s,is_customized=0,updated_by=%s,"
            "revision=revision+1 WHERE slug=%s AND revision=%s",
            (
                document.title, document.category, document.summary,
                document.content_md, document.sort_order, document.digest,
                user["id"], slug, payload.expected_revision,
            ),
        )
        if cur.rowcount != 1:
            await _raise_revision_conflict(cur, slug)
    await record_admin_audit(
        user,
        "help_document.reset",
        target_type="help_document",
        target_name=slug,
        detail={"revision": payload.expected_revision + 1},
        **request_audit_fields(request),
    )
    return await _read_help_document(conn, slug, can_edit=True)
