"""认证与权限依赖"""

import secrets
from datetime import datetime, timedelta
from fastapi import Request, HTTPException, status, Depends
from database import db_manager
from config import settings


async def get_current_user(request: Request) -> dict:
    """从 cookie 取 session_id，查 _sessions JOIN _users，返回用户信息"""
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not session_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="未登录")

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT u.id, u.username, u.role "
                "FROM _sessions s JOIN _users u ON s.user_id = u.id "
                "WHERE s.session_id = %s AND s.expires_at > NOW()",
                (session_id,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="会话已过期")
            return {"id": row[0], "username": row[1], "role": row[2]}
    finally:
        pool.release(conn)


async def require_super_admin(user: dict = Depends(get_current_user)) -> dict:
    """要求 super_admin 角色"""
    if user["role"] != "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要超级管理员权限")
    return user


def create_session(user_id: int) -> str:
    """生成 session_id（调用方负责写库）"""
    return secrets.token_urlsafe(32)


def get_session_cookie_config():
    """返回 cookie 配置"""
    return {
        "key": settings.SESSION_COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "max_age": settings.SESSION_EXPIRE_HOURS * 3600,
    }
