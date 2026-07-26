"""认证 API - 登录/登出/当前用户 + OAuth 凭据管理"""

from datetime import datetime, timedelta
import bcrypt
from fastapi import APIRouter, Request, Response, HTTPException, status, Depends
from pydantic import BaseModel
from database import db_manager
from config import settings
from deps import get_current_user, require_super_admin, create_session, get_session_cookie_config

router = APIRouter(prefix="/api/auth", tags=["认证"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response):
    """用户登录"""
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, username, password_hash, role FROM _users WHERE username = %s",
                (req.username,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=401, detail="用户名或密码错误")
            user_id, username, password_hash, role = row
            if not bcrypt.checkpw(req.password.encode(), password_hash.encode()):
                raise HTTPException(status_code=401, detail="用户名或密码错误")

            session_id = create_session(user_id)
            expires_at = datetime.utcnow() + timedelta(hours=settings.SESSION_EXPIRE_HOURS)
            await cur.execute(
                "INSERT INTO _sessions (session_id, user_id, expires_at) VALUES (%s, %s, %s)",
                (session_id, user_id, expires_at),
            )
    finally:
        pool.release(conn)

    cookie_cfg = get_session_cookie_config()
    response.set_cookie(value=session_id, **cookie_cfg)
    return {"message": "登录成功", "user": {"id": user_id, "username": username, "role": role}}


@router.post("/logout")
async def logout(request: Request, response: Response, user: dict = Depends(get_current_user)):
    """退出登录"""
    session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if session_id:
        pool = db_manager.get_pool("online_data")
        conn = await pool.acquire()
        try:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM _sessions WHERE session_id = %s", (session_id,))
        finally:
            pool.release(conn)

    cookie_cfg = get_session_cookie_config()
    response.delete_cookie(cookie_cfg["key"])
    return {"message": "已退出"}


@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    """获取当前用户信息"""
    return {"user": user}


# ========== OAuth 凭据管理（超管专用）==========

class OAuthRequest(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    open_id: str = ""


@router.get("/status")
async def get_auth_status(user: dict = Depends(get_current_user)):
    """获取 OAuth 配置状态"""
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT client_id, open_id FROM _config_oauth_tokens ORDER BY id DESC LIMIT 1"
            )
            row = await cur.fetchone()
    finally:
        pool.release(conn)

    if row:
        return {"configured": True, "client_id": row[0] or "", "open_id": row[1] or ""}
    return {"configured": False, "client_id": "", "open_id": ""}


@router.post("/oauth")
async def save_oauth(req: OAuthRequest, user: dict = Depends(require_super_admin)):
    """保存 OAuth 凭据（超管）"""
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM _config_oauth_tokens")
            await cur.execute(
                "INSERT INTO _config_oauth_tokens (client_id, client_secret, access_token, refresh_token, open_id) "
                "VALUES (%s, %s, %s, %s, %s)",
                (req.client_id, req.client_secret, req.access_token, req.refresh_token, req.open_id),
            )
    finally:
        pool.release(conn)
    return {"message": "保存成功"}


@router.post("/oauth/test")
async def test_oauth(req: OAuthRequest, user: dict = Depends(require_super_admin)):
    """测试 OAuth 凭据（超管）"""
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://docs.qq.com/openapi/spreadsheet/v3/spreadsheets",
                headers={"Authorization": f"Bearer {req.access_token}"},
                params={"client_id": req.client_id, "open_id": req.open_id},
                timeout=10,
            )
        if resp.status_code == 200:
            return {"valid": True, "message": "凭据有效"}
        return {"valid": False, "message": f"API返回 {resp.status_code}: {resp.text[:100]}"}
    except Exception as e:
        return {"valid": False, "message": f"连接失败: {e}"}
