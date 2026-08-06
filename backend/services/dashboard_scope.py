"""Derive a dashboard's responsibility scope without widening permissions."""

from __future__ import annotations

from typing import Any

from services.permissions import permitted_communities


FLOW_POSITIONS = {"组员", "组长"}
GLOBAL_RESPONSIBILITY_POSITIONS = {"基础管控", "中队长", "所队领导"}


def member_position(user: dict[str, Any]) -> str:
    return str((user.get("member") or {}).get("position") or "").strip()


def permission_group_codes(user: dict[str, Any]) -> set[str]:
    codes = {
        str(group.get("code") or "").strip()
        for group in user.get("permission_groups") or []
        if isinstance(group, dict)
    }
    primary = str((user.get("permission_group") or {}).get("code") or "").strip()
    if primary:
        codes.add(primary)
    return codes


def is_admin_account(user: dict[str, Any]) -> bool:
    codes = permission_group_codes(user)
    return bool(codes & {"admin", "super_admin"}) or (
        not codes and str(user.get("role") or "") in {"admin", "super_admin"}
    )


def is_super_admin_account(user: dict[str, Any]) -> bool:
    codes = permission_group_codes(user)
    return "super_admin" in codes or (
        not codes and str(user.get("role") or "") == "super_admin"
    )


async def role_responsibility_communities(cur, user: dict[str, Any]) -> list[str] | None:
    """Return formal community names; ``None`` means station-wide responsibility."""
    position = member_position(user)
    member = user.get("member") or {}
    if not member and is_admin_account(user):
        return None
    if position in GLOBAL_RESPONSIBILITY_POSITIONS:
        return None
    if position == "片长":
        member_id = member.get("id")
        if not member_id:
            return []
        await cur.execute(
            """
            SELECT DISTINCT community.name
            FROM _area_leader_links AS leader
            JOIN _communities AS community ON community.area_id=leader.area_id
            WHERE leader.member_id=%s AND community.is_active=1
            ORDER BY community.name
            """,
            (member_id,),
        )
        return [str(row[0]).strip() for row in await cur.fetchall() if str(row[0]).strip()]
    if position in FLOW_POSITIONS or position == "社区民警":
        names = list(dict.fromkeys(
            str(value).strip()
            for value in user.get("community_names") or []
            if str(value).strip()
        ))
        if not names:
            return []
        placeholders = ", ".join(["%s"] * len(names))
        await cur.execute(
            f"""
            SELECT DISTINCT community.name
            FROM _communities AS community
            LEFT JOIN _community_aliases AS alias
              ON alias.community_id=community.id
            WHERE community.is_active=1
              AND (
                community.name IN ({placeholders})
                OR alias.alias IN ({placeholders})
              )
            ORDER BY community.id
            """,
            [*names, *names],
        )
        return [
            str(row[0]).strip()
            for row in await cur.fetchall()
            if str(row[0]).strip()
        ]
    return []


def intersect_scopes(
    responsibility: list[str] | None,
    permission_scope: list[str] | None,
) -> list[str] | None:
    """Intersect role responsibility with a permission-specific data scope."""
    if responsibility is None:
        return None if permission_scope is None else list(permission_scope)
    if permission_scope is None:
        return list(responsibility)
    accepted = set(permission_scope)
    return [name for name in responsibility if name in accepted]


async def dashboard_communities(
    cur,
    user: dict[str, Any],
    permission: str,
) -> list[str] | None:
    responsibility = await role_responsibility_communities(cur, user)
    permission_scope = permitted_communities(user, permission)
    return intersect_scopes(responsibility, permission_scope)


async def formal_community(cur, value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    await cur.execute(
        """
        SELECT community.name
        FROM _communities AS community
        WHERE community.name=%s
        UNION
        SELECT community.name
        FROM _community_aliases AS alias
        JOIN _communities AS community ON community.id=alias.community_id
        WHERE alias.alias=%s
        LIMIT 1
        """,
        (normalized, normalized),
    )
    row = await cur.fetchone()
    return str(row[0]).strip() if row else ""


async def requested_responsibility_communities(
    cur,
    user: dict[str, Any],
    permission: str,
    community: str = "",
) -> list[str] | None:
    allowed = await dashboard_communities(cur, user, permission)
    requested = str(community or "").strip()
    if not requested:
        return allowed
    formal = await formal_community(cur, requested)
    if not formal or (allowed is not None and formal not in allowed):
        raise PermissionError("所选社区不在当前岗位职责范围内")
    return [formal]


def responsibility_label(position: str, communities: list[str] | None) -> str:
    if position == "自购房" or (position == "组员" and communities):
        return "本人"
    if communities is None:
        return "全所"
    if not communities:
        return "暂无职责社区"
    prefix = "负责片区" if position == "片长" else "负责社区" if position == "社区民警" else "所属社区"
    return f"{prefix}：{'、'.join(communities)}"
