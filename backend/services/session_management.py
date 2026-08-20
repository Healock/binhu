"""账号会话撤销辅助函数。"""

from __future__ import annotations


async def invalidate_all_sessions(cur, user_id: int) -> None:
    """在同一事务中撤销账号全部会话及在线心跳。"""
    await cur.execute(
        "DELETE FROM _user_presence_clients WHERE user_id=%s",
        (user_id,),
    )
    await cur.execute("DELETE FROM _sessions WHERE user_id=%s", (user_id,))
    await cur.execute(
        """UPDATE _users
           SET active_session_id=NULL,
               active_desktop_session_id=NULL,
               active_mobile_session_id=NULL
           WHERE id=%s""",
        (user_id,),
    )


async def invalidate_all_sessions_for_users(cur, user_ids: list[int]) -> None:
    """批量撤销多个账号的全部会话，保持账号改绑等操作的原子性。"""
    normalized = list(dict.fromkeys(int(item) for item in user_ids))
    if not normalized:
        return
    placeholders = ", ".join(["%s"] * len(normalized))
    await cur.execute(
        f"DELETE FROM _user_presence_clients WHERE user_id IN ({placeholders})",
        normalized,
    )
    await cur.execute(
        f"DELETE FROM _sessions WHERE user_id IN ({placeholders})",
        normalized,
    )
    await cur.execute(
        f"""UPDATE _users
            SET active_session_id=NULL,
                active_desktop_session_id=NULL,
                active_mobile_session_id=NULL
            WHERE id IN ({placeholders})""",
        normalized,
    )


async def invalidate_session(cur, session_id: str) -> None:
    await cur.execute(
        "DELETE FROM _user_presence_clients WHERE session_id=%s",
        (session_id,),
    )
    await cur.execute("DELETE FROM _sessions WHERE session_id=%s", (session_id,))
    await cur.execute(
        """UPDATE _users
           SET active_session_id=CASE WHEN active_session_id=%s THEN NULL ELSE active_session_id END,
               active_desktop_session_id=CASE WHEN active_desktop_session_id=%s THEN NULL ELSE active_desktop_session_id END,
               active_mobile_session_id=CASE WHEN active_mobile_session_id=%s THEN NULL ELSE active_mobile_session_id END
           WHERE active_session_id=%s
              OR active_desktop_session_id=%s
              OR active_mobile_session_id=%s""",
        (session_id, session_id, session_id, session_id, session_id, session_id),
    )
