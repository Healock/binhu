"""认证 API - 登录/登出/当前用户 + OAuth 凭据管理"""

from datetime import datetime, timedelta
from typing import Any, Literal

import bcrypt
from fastapi import APIRouter, Request, Response, HTTPException, status, Depends
from pydantic import BaseModel
from database import db_manager
from config import settings
from deps import get_current_user, require_super_admin, create_session, get_session_cookie_config
from services.audit import record_admin_audit, request_audit_fields
from services.mobile_navigation import (
    normalize_mobile_dock_config,
    normalize_mobile_navigation_mode,
    serialize_mobile_dock_config,
    validate_mobile_dock_config,
)
from services.ops_redaction import redact_text
from services.theme_preferences import normalize_theme_mode

router = APIRouter(prefix="/api/auth", tags=["认证"])


class LoginRequest(BaseModel):
    username: str
    password: str


class UserPreferencesRequest(BaseModel):
    table_display_mode: Literal["table", "card"] | None = None
    report_column_mode: Literal["two", "three"] | None = None
    mobile_navigation_mode: Literal["sidebar", "dock"] | None = None
    mobile_dock_config: dict[str, Any] | None = None
    theme_mode: Literal["light", "dark", "system"] | None = None


@router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response):
    """用户登录"""
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, username, password_hash, role, "
                "table_display_mode, report_column_mode, "
                "mobile_navigation_mode, mobile_dock_config, theme_mode "
                "FROM _users WHERE username = %s",
                (req.username,),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=401, detail="用户名或密码错误")
            (
                user_id,
                username,
                password_hash,
                role,
                table_display_mode,
                report_column_mode,
                mobile_navigation_mode,
                mobile_dock_config,
                theme_mode,
            ) = row
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
    return {
        "message": "登录成功",
        "user": {
            "id": user_id,
            "username": username,
            "role": role,
            "table_display_mode": table_display_mode or "table",
            "report_column_mode": report_column_mode or "three",
            "mobile_navigation_mode": normalize_mobile_navigation_mode(
                mobile_navigation_mode,
            ),
            "mobile_dock_config": normalize_mobile_dock_config(
                mobile_dock_config,
                str(role),
            ),
            "theme_mode": normalize_theme_mode(theme_mode),
        },
    }


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
    response.delete_cookie(
        cookie_cfg["key"],
        path=cookie_cfg["path"],
        secure=cookie_cfg["secure"],
        httponly=cookie_cfg["httponly"],
        samesite=cookie_cfg["samesite"],
    )
    return {"message": "已退出"}


@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    """获取当前用户信息"""
    return {"user": user}


@router.put("/preferences")
async def update_preferences(
    req: UserPreferencesRequest,
    user: dict = Depends(get_current_user),
):
    """保存当前账号的个性化设置。"""
    updates: list[str] = []
    values: list[Any] = []
    updated_user = dict(user)

    if req.table_display_mode is not None:
        updates.append("table_display_mode=%s")
        values.append(req.table_display_mode)
        updated_user["table_display_mode"] = req.table_display_mode
    if req.report_column_mode is not None:
        updates.append("report_column_mode=%s")
        values.append(req.report_column_mode)
        updated_user["report_column_mode"] = req.report_column_mode
    if req.mobile_navigation_mode is not None:
        updates.append("mobile_navigation_mode=%s")
        values.append(req.mobile_navigation_mode)
        updated_user["mobile_navigation_mode"] = req.mobile_navigation_mode
    if req.mobile_dock_config is not None:
        try:
            dock_config = validate_mobile_dock_config(
                req.mobile_dock_config,
                str(user["role"]),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        updates.append("mobile_dock_config=%s")
        values.append(serialize_mobile_dock_config(dock_config))
        updated_user["mobile_dock_config"] = dock_config
    if req.theme_mode is not None:
        updates.append("theme_mode=%s")
        values.append(req.theme_mode)
        updated_user["theme_mode"] = req.theme_mode

    if not updates:
        raise HTTPException(status_code=400, detail="没有需要保存的个性化设置")

    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE _users SET {', '.join(updates)} WHERE id=%s",
                (*values, user["id"]),
            )
    finally:
        pool.release(conn)

    return {
        "message": "个性化设置已保存",
        "user": {
            **updated_user,
            "mobile_dock_config": normalize_mobile_dock_config(
                updated_user.get("mobile_dock_config"),
                str(user["role"]),
            ),
            "theme_mode": normalize_theme_mode(
                updated_user.get("theme_mode"),
            ),
        },
    }


# ========== OAuth 凭据管理（超管专用）==========

class OAuthRequest(BaseModel):
    client_id: str = ""
    client_secret: str = ""
    access_token: str = ""
    refresh_token: str = ""
    open_id: str = ""


@router.get("/status")
async def get_auth_status(user: dict = Depends(require_super_admin)):
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
async def save_oauth(
    req: OAuthRequest,
    request: Request,
    user: dict = Depends(require_super_admin),
):
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
    await record_admin_audit(
        user,
        "oauth.update",
        target_type="oauth",
        target_name="tencent-docs",
        detail={"configured": True},
        **request_audit_fields(request),
    )
    return {"message": "保存成功"}


@router.post("/oauth/test")
async def test_oauth(
    req: OAuthRequest,
    request: Request,
    user: dict = Depends(require_super_admin),
):
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
        valid = resp.status_code == 200
        await record_admin_audit(
            user,
            "oauth.test",
            target_type="oauth",
            target_name="tencent-docs",
            result="success" if valid else "failed",
            detail={"http_status": resp.status_code},
            **request_audit_fields(request),
        )
        if valid:
            return {"valid": True, "message": "凭据有效"}
        return {"valid": False, "message": f"API返回 {resp.status_code}: {resp.text[:100]}"}
    except Exception as e:
        await record_admin_audit(
            user,
            "oauth.test",
            target_type="oauth",
            target_name="tencent-docs",
            result="failed",
            detail={"error": redact_text(str(e))[:200]},
            **request_audit_fields(request),
        )
        return {
            "valid": False,
            "message": f"连接失败: {redact_text(str(e))}",
        }
