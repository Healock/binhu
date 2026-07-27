"""用户管理 API - 超管专用"""

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Optional
from database import db_manager
from deps import get_current_user, require_super_admin

router = APIRouter(prefix="/api/users", tags=["用户管理"])

ROLE_LABELS = {"super_admin": "超级管理员", "admin": "管理员", "leader": "组长", "member": "组员"}


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str  # super_admin / admin / leader / member


class UpdateUserRequest(BaseModel):
    role: Optional[str] = None
    password: Optional[str] = None


@router.get("")
async def list_users(user: dict = Depends(require_super_admin)):
    """列出所有用户（超管）"""
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, username, role, created_at, updated_at FROM _users ORDER BY id"
            )
            rows = await cur.fetchall()
    finally:
        pool.release(conn)

    return {
        "data": [
            {"id": r[0], "username": r[1], "role": r[2],
             "role_label": ROLE_LABELS.get(r[2], r[2]),
             "created_at": str(r[3]) if r[3] else None,
             "updated_at": str(r[4]) if r[4] else None}
            for r in rows
        ]
    }


@router.post("")
async def create_user(req: CreateUserRequest, user: dict = Depends(require_super_admin)):
    """创建用户（超管）"""
    if req.role not in ROLE_LABELS:
        raise HTTPException(status_code=400, detail=f"无效角色，可选：{list(ROLE_LABELS.keys())}")

    password_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO _users (username, password_hash, role) VALUES (%s, %s, %s)",
                (req.username, password_hash, req.role),
            )
    except Exception as e:
        if "Duplicate" in str(e):
            raise HTTPException(status_code=400, detail="用户名已存在")
        raise
    finally:
        pool.release(conn)

    return {"message": "用户创建成功", "username": req.username, "role": req.role}


@router.put("/{user_id}")
async def update_user(user_id: int, req: UpdateUserRequest, user: dict = Depends(require_super_admin)):
    """修改用户（改角色/改密码）（超管）"""
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            sets = []
            params = []
            if req.role:
                if req.role not in ROLE_LABELS:
                    raise HTTPException(status_code=400, detail=f"无效角色")
                sets.append("role = %s")
                params.append(req.role)
            if req.password:
                password_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
                sets.append("password_hash = %s")
                params.append(password_hash)
            if not sets:
                raise HTTPException(status_code=400, detail="无修改内容")
            params.append(user_id)
            await cur.execute(f"UPDATE _users SET {', '.join(sets)} WHERE id = %s", params)
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="用户不存在")
    finally:
        pool.release(conn)

    return {"message": "用户修改成功"}


@router.delete("/{user_id}")
async def delete_user(user_id: int, user: dict = Depends(require_super_admin)):
    """删除用户（超管），不能删自己"""
    if user["id"] == user_id:
        raise HTTPException(status_code=400, detail="不能删除自己")

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM _users WHERE id = %s", (user_id,))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="用户不存在")
            # 同时删除该用户的 session
            await cur.execute("DELETE FROM _sessions WHERE user_id = %s", (user_id,))
            await cur.execute(
                "DELETE FROM _notifications WHERE user_id = %s",
                (user_id,),
            )
    finally:
        pool.release(conn)

    return {"message": "用户已删除"}
