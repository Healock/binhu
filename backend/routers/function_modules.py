"""功能模块归属与版本快照管理。"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from database import get_db
from deps import require_permission
from services.module_registry import load_module_definitions, route_prefixes
from services.permissions import ALL_PERMISSIONS, MODULE_ACCESS_MANAGE

router = APIRouter(prefix="/api/function-modules", tags=["功能模块"])


class ModulePayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    owner_unit: str = Field(default="", max_length=60)
    is_public: bool = False
    required_permissions: list[str] = Field(default_factory=list)
    route: str = Field(default="", max_length=190)
    mobile_visible: bool = True
    enabled: bool = True
    module_id: str | None = Field(default=None, max_length=60)

def _snapshot(module_id: str, payload: ModulePayload, version: int) -> str:
    return json.dumps(
        {"module_id": module_id, **payload.model_dump(exclude={"module_id"}), "version": version},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _validate_payload(payload: ModulePayload) -> None:
    unknown = sorted(set(payload.required_permissions) - ALL_PERMISSIONS)
    if unknown:
        raise HTTPException(status_code=422, detail="模块包含未知权限码")
    if payload.owner_unit not in {"common", "community_police", "internal"}:
        raise HTTPException(status_code=422, detail="模块归属单位无效")
    if payload.owner_unit == "common" and not payload.is_public:
        raise HTTPException(status_code=422, detail="common 模块必须标记为公共模块")
    if payload.is_public and payload.owner_unit != "common":
        raise HTTPException(status_code=422, detail="公共模块的归属单位必须为 common")
    if payload.route and any(not item.startswith("/") for item in route_prefixes(payload.route)):
        raise HTTPException(status_code=422, detail="模块路由必须是站内绝对路径")


@router.get("")
async def list_function_modules(
    _user: dict = Depends(require_permission(MODULE_ACCESS_MANAGE)),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        modules = await load_module_definitions(cur)
    return {"data": modules}


@router.post("")
async def create_function_module(
    payload: ModulePayload,
    user: dict = Depends(require_permission(MODULE_ACCESS_MANAGE)),
    conn=Depends(get_db),
):
    module_id = str(payload.module_id or "").strip()
    if not module_id or len(module_id) > 60:
        raise HTTPException(status_code=422, detail="模块标识不能为空且不能超过 60 个字符")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,59}", module_id):
        raise HTTPException(status_code=422, detail="模块标识格式无效")
    _validate_payload(payload)
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM _function_modules WHERE module_id=%s", (module_id,))
            if await cur.fetchone():
                raise HTTPException(status_code=409, detail="模块标识已经存在")
            await cur.execute(
                "INSERT INTO _function_modules (module_id,name,owner_unit,is_public,required_permissions,route,mobile_visible,enabled,version) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1)",
                (module_id, payload.name, payload.owner_unit, int(payload.is_public), json.dumps(payload.required_permissions),
                 payload.route, int(payload.mobile_visible), int(payload.enabled)),
            )
            await cur.execute(
                "INSERT INTO _function_module_versions (module_id,version,snapshot,changed_by) VALUES (%s,1,%s,%s)",
                (module_id, _snapshot(module_id, payload, 1), user.get("id")),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {"module_id": module_id, "version": 1}


@router.patch("/{module_id}")
async def update_function_module(
    module_id: str,
    payload: ModulePayload,
    user: dict = Depends(require_permission(MODULE_ACCESS_MANAGE)),
    conn=Depends(get_db),
):
    _validate_payload(payload)
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute("SELECT version FROM _function_modules WHERE module_id=%s FOR UPDATE", (module_id,))
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="模块不存在")
            version = int(row[0] or 1) + 1
            await cur.execute(
                "UPDATE _function_modules SET name=%s,owner_unit=%s,is_public=%s,required_permissions=%s,route=%s,mobile_visible=%s,enabled=%s,version=%s WHERE module_id=%s",
                (payload.name, payload.owner_unit, int(payload.is_public), json.dumps(payload.required_permissions), payload.route,
                 int(payload.mobile_visible), int(payload.enabled), version, module_id),
            )
            await cur.execute(
                "INSERT INTO _function_module_versions (module_id,version,snapshot,changed_by) VALUES (%s,%s,%s,%s)",
                (module_id, version, _snapshot(module_id, payload, version), user.get("id")),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {"module_id": module_id, "version": version}
