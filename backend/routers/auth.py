"""认证 API - 登录/登出/当前用户 + OAuth 凭据管理"""

from datetime import datetime, timedelta
from pathlib import Path
import uuid
from typing import Any, Literal

import bcrypt
from fastapi import APIRouter, Request, Response, HTTPException, status, Depends, File, UploadFile
from fastapi.responses import FileResponse
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
from services.maintenance import (
    is_database_user_super_admin,
    maintenance_status,
)
from services.workflow_support import detect_attachment_mime
from services.session_devices import (
    hash_device_id,
    infer_device_type,
    public_session_fingerprint,
    user_agent_family,
)
from services.session_management import invalidate_all_sessions, invalidate_session

MAX_AVATAR_BYTES = 5 * 1024 * 1024
AVATAR_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic"}


def _resolve_avatar(storage_key: str) -> Path:
    root = Path(settings.USER_AVATAR_DIR).resolve()
    target = (root / storage_key).resolve()
    if root not in target.parents or not target.is_file() or target.is_symlink():
        raise FileNotFoundError("avatar not found")
    return target


def _remove_avatar(storage_key: str | None) -> None:
    if not storage_key:
        return
    try:
        _resolve_avatar(storage_key).unlink(missing_ok=True)
    except OSError:
        return


router = APIRouter(prefix="/api/auth", tags=["认证"])


class LoginRequest(BaseModel):
    username: str
    password: str
    device_type: str | None = None
    device_id: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class UserPreferencesRequest(BaseModel):
    table_display_mode: Literal["table", "card"] | None = None
    task_display_mode: Literal["table", "card"] | None = None
    report_column_mode: Literal["two", "three"] | None = None
    mobile_navigation_mode: Literal["sidebar", "dock"] | None = None
    mobile_dock_config: dict[str, Any] | None = None
    theme_mode: Literal["light", "dark", "system"] | None = None


@router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response):
    """登录并把该账号之前的设备会话替换为当前会话。"""
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, username, password_hash, role, "
                "table_display_mode, report_column_mode, "
                "mobile_navigation_mode, mobile_dock_config, theme_mode, "
                "COALESCE(NULLIF(display_name, ''), "
                "(SELECT name FROM _grid_members WHERE id=_users.member_id), "
                "username), avatar_storage_key, task_display_mode "
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
                display_name,
                avatar_storage_key,
                task_display_mode,
            ) = row
            if not bcrypt.checkpw(req.password.encode(), password_hash.encode()):
                raise HTTPException(status_code=401, detail="用户名或密码错误")

            device_type = infer_device_type(
                requested=req.device_type,
                platform_header=request.headers.get("X-Binhu-Client-Platform"),
                user_agent=request.headers.get("User-Agent"),
                mobile_hint=request.headers.get("Sec-CH-UA-Mobile"),
            )
            device_id_hash = hash_device_id(req.device_id)
            client_platform = device_type
            ua_family = user_agent_family(request.headers.get("User-Agent"))

            # 登录阶段也必须拦截普通账号，避免维护期间创建新会话。
            await cur.execute(
                "SELECT config_key, config_value FROM _system_config "
                "WHERE config_key IN "
                "('maintenance_enabled', 'maintenance_start_at', "
                "'maintenance_end_at', 'maintenance_message', 'timezone')"
            )
            maintenance_config = {
                str(item[0]): str(item[1] or "")
                for item in await cur.fetchall()
            }
            await cur.execute("SELECT UTC_TIMESTAMP()")
            maintenance_server_time_row = await cur.fetchone()
            maintenance_server_time = (
                maintenance_server_time_row[0]
                if maintenance_server_time_row
                else None
            )
            current_maintenance = maintenance_status(
                maintenance_config,
                now=maintenance_server_time,
            )
            if current_maintenance["active"] and not await is_database_user_super_admin(
                cur, int(user_id), str(role)
            ):
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "maintenance_mode",
                        "message": current_maintenance["message"],
                        "maintenance": current_maintenance,
                    },
                    headers={"Retry-After": "300"},
                )

            await cur.execute(
                "SELECT id FROM _users WHERE id=%s FOR UPDATE",
                (user_id,),
            )
            session_id = create_session(user_id)
            expires_at = datetime.utcnow() + timedelta(hours=settings.SESSION_EXPIRE_HOURS)
            await cur.execute(
                "SELECT active_desktop_session_id, active_mobile_session_id, "
                "active_session_id FROM _users WHERE id=%s FOR UPDATE",
                (user_id,),
            )
            active_slots = await cur.fetchone() or (None, None, None)
            slot_column = (
                "active_mobile_session_id"
                if device_type == "mobile"
                else "active_desktop_session_id"
            )
            if settings.MULTI_DEVICE_SESSION_ENABLED:
                previous_session = active_slots[1 if device_type == "mobile" else 0]
            else:
                previous_session = active_slots[2]
            if previous_session and previous_session != session_id:
                await invalidate_session(cur, str(previous_session))
            management_id = str(uuid.uuid4())
            await cur.execute(
                "INSERT INTO _sessions "
                "(session_id, management_id, user_id, device_type, device_id_hash, "
                "client_platform, user_agent_family, last_activity_at, expires_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, UTC_TIMESTAMP(), %s)",
                (
                    session_id,
                    management_id,
                    user_id,
                    device_type,
                    device_id_hash,
                    client_platform,
                    ua_family,
                    expires_at,
                ),
            )
            await cur.execute(
                f"UPDATE _users SET {slot_column}=%s, "
                "active_session_id=%s WHERE id=%s",
                (session_id, session_id, user_id),
            )
            await cur.execute(
                "DELETE FROM _sessions WHERE user_id=%s "
                "AND expires_at<=UTC_TIMESTAMP() AND session_id<>%s",
                (user_id, session_id),
            )
            await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        pool.release(conn)

    cookie_cfg = get_session_cookie_config()
    response.set_cookie(value=session_id, **cookie_cfg)
    return {
        "message": "登录成功",
        "session_refresh_required": True,
        "session": {
            "management_id": management_id,
            "device_type": device_type,
        },
        "user": {
            "id": user_id,
            "username": username,
            "display_name": display_name,
            "role": role,
            "table_display_mode": table_display_mode or "table",
            "task_display_mode": task_display_mode or "table",
            "report_column_mode": report_column_mode or "three",
            "mobile_navigation_mode": normalize_mobile_navigation_mode(
                mobile_navigation_mode,
            ),
            "mobile_dock_config": normalize_mobile_dock_config(
                mobile_dock_config,
                str(role),
            ),
            "theme_mode": normalize_theme_mode(theme_mode),
            "avatar_url": (
                f"/api/auth/avatar/{int(user_id)}?v={Path(str(avatar_storage_key)).stem}"
                if avatar_storage_key else None
            ),
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
            await conn.begin()
            async with conn.cursor() as cur:
                await invalidate_session(cur, session_id)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
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


@router.get("/sessions")
async def list_sessions(
    request: Request,
    user: dict = Depends(get_current_user),
):
    """列出当前账号的有效登录设备，不返回认证会话值。"""
    current_session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT management_id, session_id, device_type,
                          client_platform, user_agent_family, created_at,
                          last_activity_at, expires_at
                   FROM _sessions
                   WHERE user_id=%s AND expires_at>UTC_TIMESTAMP()
                   ORDER BY last_activity_at DESC, created_at DESC""",
                (user["id"],),
            )
            rows = await cur.fetchall()
    finally:
        pool.release(conn)

    def iso(value):
        return value.isoformat() + "Z" if value else None

    return {
        "sessions": [
            {
                "management_id": row[0],
                "device_type": row[2] or "desktop",
                "client_platform": row[3] or row[2] or "desktop",
                "user_agent_family": row[4] or "其他浏览器",
                "created_at": iso(row[5]),
                "last_activity_at": iso(row[6]),
                "expires_at": iso(row[7]),
                "current": bool(current_session_id and row[1] == current_session_id),
            }
            for row in rows
        ]
    }


@router.delete("/sessions/{management_id}")
async def revoke_session(
    management_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
):
    current_session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT session_id FROM _sessions WHERE management_id=%s AND user_id=%s FOR UPDATE",
                (management_id, user["id"]),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="登录设备不存在或已退出")
            if current_session_id and row[0] == current_session_id:
                raise HTTPException(status_code=400, detail="当前设备请使用退出登录")
            await invalidate_session(cur, str(row[0]))
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        pool.release(conn)
    return {"message": "设备已退出"}


@router.post("/sessions/revoke-others")
async def revoke_other_sessions(
    request: Request,
    user: dict = Depends(get_current_user),
):
    current_session_id = request.cookies.get(settings.SESSION_COOKIE_NAME)
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT session_id FROM _sessions WHERE user_id=%s AND session_id<>%s",
                (user["id"], current_session_id or ""),
            )
            session_ids = [str(row[0]) for row in await cur.fetchall()]
            for session_id in session_ids:
                await invalidate_session(cur, session_id)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        pool.release(conn)
    return {"message": "其他设备已退出", "revoked": len(session_ids)}


@router.post("/sessions/revoke-all")
async def revoke_all_sessions(
    request: Request,
    response: Response,
    user: dict = Depends(get_current_user),
):
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await invalidate_all_sessions(cur, int(user["id"]))
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
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
    return {"message": "全部设备已退出"}


@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    """获取当前用户信息"""
    return {"user": user}


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    extension = Path(file.filename or "avatar").suffix.lower()
    if extension not in AVATAR_EXTENSIONS:
        raise HTTPException(status_code=400, detail="仅支持 JPG、PNG、WebP 或 HEIC 图片")
    content = await file.read()
    if not content or len(content) > MAX_AVATAR_BYTES:
        raise HTTPException(status_code=400, detail="头像文件必须小于或等于 5MB")
    try:
        mime_type = detect_attachment_mime(content, extension)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    root = Path(settings.USER_AVATAR_DIR).resolve()
    user_dir = (root / str(user["id"])).resolve()
    if user_dir.parent != root:
        raise HTTPException(status_code=400, detail="头像目录无效")
    user_dir.mkdir(parents=True, exist_ok=True)
    storage_name = f"{uuid.uuid4()}{extension}"
    target = (user_dir / storage_name).resolve()
    if target.parent != user_dir:
        raise HTTPException(status_code=400, detail="头像路径无效")
    target.write_bytes(content)
    storage_key = f"{user['id']}/{storage_name}"
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT avatar_storage_key FROM _users WHERE id=%s",
                (user["id"],),
            )
            previous_row = await cur.fetchone()
            previous_storage_key = str(previous_row[0]) if previous_row and previous_row[0] else None
            await cur.execute(
                "UPDATE _users SET avatar_storage_key=%s, avatar_mime=%s WHERE id=%s",
                (storage_key, mime_type, user["id"]),
            )
    except Exception:
        target.unlink(missing_ok=True)
        raise
    finally:
        pool.release(conn)
    _remove_avatar(previous_storage_key)
    return {
        "message": "头像已更新",
        "avatar_url": f"/api/auth/avatar/{int(user['id'])}?v={Path(storage_key).stem}",
    }


@router.get("/avatar/{user_id}")
async def get_avatar(user_id: int, _user: dict = Depends(get_current_user)):
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT avatar_storage_key, avatar_mime FROM _users WHERE id=%s",
                (user_id,),
            )
            row = await cur.fetchone()
    finally:
        pool.release(conn)
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="头像不存在")
    try:
        path = _resolve_avatar(str(row[0]))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="头像不存在") from exc
    return FileResponse(
        path,
        media_type=str(row[1] or "image/jpeg"),
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/activity")
async def record_activity(user: dict = Depends(get_current_user)):
    """由前端在页面跳转、查询或提交时显式刷新空闲时间。"""
    return {
        "message": "活动时间已更新",
        "user": user,
        "session_policy": user["session_policy"],
    }


@router.put("/password")
async def change_password(
    req: ChangePasswordRequest,
    request: Request,
    user: dict = Depends(get_current_user),
):
    if len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="新密码至少需要 8 位")
    if req.new_password == req.current_password:
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
    pool = db_manager.get_pool("online_data")
    conn = await pool.acquire()
    try:
        await conn.begin()
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT password_hash FROM _users WHERE id=%s",
                (user["id"],),
            )
            row = await cur.fetchone()
            if not row or not bcrypt.checkpw(
                req.current_password.encode(), str(row[0]).encode()
            ):
                raise HTTPException(status_code=400, detail="当前密码不正确")
            password_hash = bcrypt.hashpw(
                req.new_password.encode(), bcrypt.gensalt()
            ).decode()
            await cur.execute(
                "UPDATE _users SET password_hash=%s, "
                "password_is_temporary=0 WHERE id=%s",
                (password_hash, user["id"]),
            )
            await invalidate_all_sessions(cur, int(user["id"]))
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    finally:
        pool.release(conn)
    await record_admin_audit(
        user,
        "account.password.change",
        target_type="user",
        target_name=str(user["id"]),
        detail={"temporary_password_cleared": True},
        **request_audit_fields(request),
    )
    return {"message": "密码已修改", "password_is_temporary": False}


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
    if req.task_display_mode is not None:
        updates.append("task_display_mode=%s")
        values.append(req.task_display_mode)
        updated_user["task_display_mode"] = req.task_display_mode
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
                user.get("permissions"),
                [
                    str(group.get("code") or "")
                    for group in user.get("permission_groups") or []
                    if isinstance(group, dict)
                ],
                (user.get("member") or {}).get("position"),
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
                user.get("permissions"),
                [
                    str(group.get("code") or "")
                    for group in user.get("permission_groups") or []
                    if isinstance(group, dict)
                ],
                (user.get("member") or {}).get("position"),
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
