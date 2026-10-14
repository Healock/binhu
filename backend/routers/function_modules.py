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
    department_ids: list[int] = Field(default_factory=list)
    is_public: bool = False
    required_permissions: list[str] = Field(default_factory=list)
    route: str = Field(default="", max_length=190)
    mobile_visible: bool = True
    enabled: bool = True
    module_id: str | None = Field(default=None, max_length=60)


def _normalized_department_ids(values: list[int]) -> list[int]:
    result: list[int] = []
    for value in values:
        try:
            department_id = int(value)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="模块归属部门编号无效") from exc
        if department_id <= 0:
            raise HTTPException(status_code=422, detail="模块归属部门编号无效")
        if department_id not in result:
            result.append(department_id)
    return result


def _validate_payload(payload: ModulePayload) -> list[int]:
    unknown = sorted(set(payload.required_permissions) - ALL_PERMISSIONS)
    if unknown:
        raise HTTPException(status_code=422, detail="模块包含未知权限码")
    department_ids = _normalized_department_ids(payload.department_ids)
    if payload.is_public and department_ids:
        raise HTTPException(status_code=422, detail="公共模块不能配置部门归属")
    if payload.route and any(not item.startswith("/") for item in route_prefixes(payload.route)):
        raise HTTPException(status_code=422, detail="模块路由必须是站内绝对路径")
    return department_ids


def _snapshot(
    module_id: str,
    payload: ModulePayload,
    department_ids: list[int],
    version: int,
) -> str:
    return json.dumps(
        {
            "module_id": module_id,
            **payload.model_dump(exclude={"module_id"}),
            "department_ids": department_ids,
            "version": version,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


async def _check_departments(cur, department_ids: list[int]) -> None:
    if not department_ids:
        return
    placeholders = ", ".join(["%s"] * len(department_ids))
    await cur.execute(
        f"SELECT id FROM _departments WHERE id IN ({placeholders}) AND is_active=1",
        department_ids,
    )
    existing = {int(row[0]) for row in await cur.fetchall()}
    if existing != set(department_ids):
        raise HTTPException(status_code=422, detail="模块归属部门不存在或已停用")


@router.get("")
async def list_function_modules(
    _user: dict = Depends(require_permission(MODULE_ACCESS_MANAGE)),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        modules = await load_module_definitions(cur)
        await cur.execute(
            "SELECT id, name, parent_id, department_type, is_active "
            "FROM _departments ORDER BY parent_id, name, id"
        )
        departments = [
            {
                "id": int(row[0]),
                "name": str(row[1] or ""),
                "parent_id": int(row[2]) if row[2] is not None else None,
                "type": str(row[3] or ""),
                "is_active": bool(row[4]),
            }
            for row in await cur.fetchall()
        ]
    return {"data": modules, "departments": departments}


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
    department_ids = _validate_payload(payload)
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await _check_departments(cur, department_ids)
            await cur.execute("SELECT 1 FROM _function_modules WHERE module_id=%s", (module_id,))
            if await cur.fetchone():
                raise HTTPException(status_code=409, detail="模块标识已经存在")
            await cur.execute(
                "INSERT INTO _function_modules "
                "(module_id,name,is_public,required_permissions,route,mobile_visible,enabled,version) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,1)",
                (module_id, payload.name, int(payload.is_public), json.dumps(payload.required_permissions),
                 payload.route, int(payload.mobile_visible), int(payload.enabled)),
            )
            if department_ids:
                await cur.executemany(
                    "INSERT INTO _function_module_units (module_id,department_id,granted_by) VALUES (%s,%s,%s)",
                    [(module_id, department_id, user.get("id")) for department_id in department_ids],
                )
            await cur.execute(
                "INSERT INTO _function_module_versions "
                "(module_id,version,snapshot,changed_by) VALUES (%s,1,%s,%s)",
                (module_id, _snapshot(module_id, payload, department_ids, 1), user.get("id")),
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
    department_ids = _validate_payload(payload)
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await _check_departments(cur, department_ids)
            await cur.execute(
                "SELECT version FROM _function_modules WHERE module_id=%s FOR UPDATE",
                (module_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="模块不存在")
            version = int(row[0] or 1) + 1
            await cur.execute(
                "UPDATE _function_modules SET name=%s,is_public=%s,required_permissions=%s,"
                "route=%s,mobile_visible=%s,enabled=%s,version=%s WHERE module_id=%s",
                (payload.name, int(payload.is_public), json.dumps(payload.required_permissions), payload.route,
                 int(payload.mobile_visible), int(payload.enabled), version, module_id),
            )
            await cur.execute("DELETE FROM _function_module_units WHERE module_id=%s", (module_id,))
            if department_ids:
                await cur.executemany(
                    "INSERT INTO _function_module_units (module_id,department_id,granted_by) VALUES (%s,%s,%s)",
                    [(module_id, department_id, user.get("id")) for department_id in department_ids],
                )
            await cur.execute(
                "INSERT INTO _function_module_versions "
                "(module_id,version,snapshot,changed_by) VALUES (%s,%s,%s,%s)",
                (module_id, version, _snapshot(module_id, payload, department_ids, version), user.get("id")),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {"module_id": module_id, "version": version}
