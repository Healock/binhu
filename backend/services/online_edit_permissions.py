"""在线原始数据回写的岗位硬限制。"""

from __future__ import annotations

from typing import Any

from services.permissions import (
    ONLINE_RAW_EDIT,
    ONLINE_RAW_ROW_MANAGE,
    ONLINE_RAW_VIEW,
    has_permission,
    permitted_communities,
)


COMMUNITY_EDITOR_POSITIONS = {"组长", "组员"}
AREA_EDITOR_POSITION = "片长"
GLOBAL_EDITOR_POSITIONS = {
    "基础管控",
    "中队长",
    "社区民警",
    "所队领导",
}
GRID_STANDARD_FIELDS = {"核查人", "现住址", "核查结果", "核查反馈"}
SECONDARY_FIELDS = {"二次反馈", "二次核查结果"}


def _is_super_admin(user: dict[str, Any]) -> bool:
    codes = {
        str(group.get("code"))
        for group in user.get("permission_groups") or []
        if isinstance(group, dict)
    }
    return user.get("role") == "super_admin" or "super_admin" in codes


def _position(user: dict[str, Any]) -> str:
    member = user.get("member") or {}
    return str(member.get("position") or "").strip()


def _role_class(user: dict[str, Any]) -> str:
    if _is_super_admin(user):
        return "global"
    position = _position(user)
    if position in COMMUNITY_EDITOR_POSITIONS:
        return "community"
    if position == AREA_EDITOR_POSITION:
        return "area"
    if position in GLOBAL_EDITOR_POSITIONS:
        return "global"
    if not position and user.get("role") == "admin":
        return "global"
    return "none"


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


async def _hard_communities(cur, user: dict[str, Any]) -> list[str] | None:
    role_class = _role_class(user)
    if role_class == "global":
        return None
    if role_class == "community":
        return list(dict.fromkeys(
            str(name).strip()
            for name in user.get("community_names") or []
            if str(name).strip()
        ))
    if role_class == "area":
        member_id = (user.get("member") or {}).get("id")
        if not member_id:
            return []
        await cur.execute(
            """
            SELECT DISTINCT community.name
            FROM _area_leader_links AS leader
            JOIN _communities AS community ON community.area_id=leader.area_id
            WHERE leader.member_id=%s
            ORDER BY community.name
            """,
            (member_id,),
        )
        return [str(row[0]).strip() for row in await cur.fetchall()]
    return []


async def effective_edit_communities(
    cur,
    user: dict[str, Any],
) -> list[str] | None:
    """返回岗位规定的编辑范围；None 表示全所。

    权限组决定账号是否拥有编辑能力，但不能扩大或缩小岗位规定的编辑
    地域：组长、组员固定本人社区，片长固定负责片区，全局岗位固定全所。
    """
    if not has_permission(user, ONLINE_RAW_EDIT):
        return []
    return await _hard_communities(cur, user)


async def inspector_option_context(
    cur,
    user: dict[str, Any],
    *,
    assignment_only: bool = False,
) -> dict[str, Any]:
    """返回按正式社区分组的核查人选项。

    下拉选项只能来自当前账号的在线回写地域。社区有可分配人员时
    使用社区名单；社区为空、无法识别或没有人员时，前端使用
    ``fallback_inspectors``。
    """
    allowed = await effective_edit_communities(cur, user)
    await cur.execute(
        """
        SELECT community.name, alias.alias
        FROM _communities AS community
        LEFT JOIN _community_aliases AS alias
          ON alias.community_id=community.id
        JOIN _departments AS department
          ON department.community_id=community.id
         AND department.department_type='community'
         AND department.is_active=1
        WHERE community.is_active=1
        ORDER BY community.id, alias.id
        """
    )
    aliases: dict[str, str] = {}
    formal_names: list[str] = []
    for formal_value, alias_value in await cur.fetchall():
        formal = str(formal_value or "").strip()
        if not formal:
            continue
        aliases[formal] = formal
        if formal not in formal_names:
            formal_names.append(formal)
        alias = str(alias_value or "").strip()
        if alias:
            aliases[alias] = formal

    allowed_set = set(formal_names if allowed is None else allowed)
    # Only expose communities the account can actually edit.  The row-level
    # validator remains authoritative, but the editor must not advertise
    # unrelated communities in the first place.
    aliases = {
        alias: formal
        for alias, formal in aliases.items()
        if formal in allowed_set
    }
    # 组长参与批量分配时也应作为可分配对象，尤其是平均分配场景。
    # assignment_only 只限制候选来源为在岗的一线核查人员，不排除组长。
    position_condition = "member.position IN ('组长', '组员')"
    await cur.execute(
        f"""
        SELECT DISTINCT community.name, member.name
        FROM _grid_members AS member
        JOIN _grid_member_department_links AS link
          ON link.member_id=member.id
        JOIN _departments AS department
          ON department.id=link.department_id
         AND department.department_type='community'
         AND department.is_active=1
        JOIN _communities AS community
          ON community.id=department.community_id
         AND community.is_active=1
        WHERE {position_condition}
          AND member.status='在岗'
        ORDER BY community.name, member.name
        """
    )
    by_community: dict[str, list[str]] = {
        name: [] for name in formal_names if name in allowed_set
    }
    fallback: list[str] = []
    for community_value, member_value in await cur.fetchall():
        community = str(community_value or "").strip()
        member = str(member_value or "").strip()
        if not community or not member or community not in allowed_set:
            continue
        members = by_community.setdefault(community, [])
        if member not in members:
            members.append(member)
        if member not in fallback:
            fallback.append(member)

    return {
        "community_aliases": aliases,
        "inspectors_by_community": by_community,
        "fallback_inspectors": fallback,
        "community_column": "社区",
        "inspector_column": "核查人",
    }


def inspector_assignment_mismatch(
    context: dict[str, Any],
    community_value: str | None,
    inspector_value: str | None,
) -> bool:
    inspector = str(inspector_value or "").strip()
    if not inspector:
        return False
    community = str(community_value or "").strip()
    formal = (context.get("community_aliases") or {}).get(community, "")
    local = list((context.get("inspectors_by_community") or {}).get(formal) or [])
    allowed = local or list(context.get("fallback_inspectors") or [])
    return inspector not in allowed


def validate_inspector_assignment(
    context: dict[str, Any],
    community_value: str | None,
    inspector_value: str | None,
) -> None:
    inspector = str(inspector_value or "").strip()
    if not inspector:
        return
    if inspector_assignment_mismatch(context, community_value, inspector):
        raise ValueError("所选核查人不属于当前社区或当前账号可编辑范围")


def effective_view_communities(user: dict[str, Any]) -> list[str] | None:
    """返回在线原始数据查看范围；None 表示全所。

    组长、组员的权限组可以把查看范围扩大到全所；片长和全局编辑岗位
    按既定岗位规则始终查看全所。其他岗位继续使用权限组的数据范围。
    """
    if not has_permission(user, ONLINE_RAW_VIEW):
        return []
    role_class = _role_class(user)
    if role_class in {"area", "global"}:
        return None
    return permitted_communities(user, ONLINE_RAW_VIEW)


def editable_fields_for_row(
    user: dict[str, Any],
    columns: list[str],
    values: dict[str, str],
    *,
    extra_fields: tuple[str, ...] = (),
) -> list[str]:
    role_class = _role_class(user)
    if role_class in {"area", "global"}:
        return list(columns)
    if role_class != "community":
        return []
    result = [column for column in columns if column in GRID_STANDARD_FIELDS]
    result.extend(column for column in extra_fields if column in columns)
    if "实际情况" in columns:
        result.append("实际情况")
    primary_result = str(
        values.get("核查结果") or values.get("核查反馈") or ""
    )
    if "无法核实" in primary_result:
        result.extend(column for column in columns if column in SECONDARY_FIELDS)
    return list(dict.fromkeys(result))


async def row_edit_capabilities(
    cur,
    user: dict[str, Any],
    parser,
    values: dict[str, str],
) -> dict[str, Any]:
    allowed = await effective_edit_communities(cur, user)
    formal = await formal_community(cur, parser.community_value(values))
    within_scope = allowed is None or bool(formal and formal in allowed)
    fields = (
        editable_fields_for_row(
            user,
            parser.COLUMNS,
            values,
            extra_fields=getattr(parser, "MOBILE_EDITABLE_FIELDS", ()),
        )
        if within_scope
        else []
    )
    return {
        "editable_fields": fields,
        "can_edit": bool(fields),
        "formal_community": formal,
        "edit_communities": allowed,
    }


async def validate_row_change(
    cur,
    user: dict[str, Any],
    parser,
    before: dict[str, str],
    after: dict[str, str],
    column: str,
) -> None:
    await validate_row_changes(cur, user, parser, before, after, [column])


async def validate_row_changes(
    cur,
    user: dict[str, Any],
    parser,
    before: dict[str, str],
    after: dict[str, str],
    columns: list[str],
) -> None:
    """一次校验同一来源行的多个字段。

    二次反馈能否填写取决于本次提交后的主结果，避免用户必须先保存
    “无法核实”，再单独保存二次反馈。
    """
    capabilities = await row_edit_capabilities(cur, user, parser, before)
    editable = set(capabilities["editable_fields"])
    if _role_class(user) == "community" and capabilities["can_edit"]:
        editable = set(editable_fields_for_row(
            user,
            parser.COLUMNS,
            after,
            extra_fields=getattr(parser, "MOBILE_EDITABLE_FIELDS", ()),
        ))
        # A secondary feedback value may be entered while the row is
        # "无法核实" and saved together with a final primary result.  Keep
        # those fields writable for this transition so the historical
        # feedback is not silently dropped from the batch request.
        primary_before = str(
            before.get("核查结果") or before.get("核查反馈") or ""
        )
        if "无法核实" in primary_before:
            editable.update(
                column for column in parser.COLUMNS
                if column in SECONDARY_FIELDS
            )
    if any(column not in editable for column in columns):
        raise PermissionError("当前岗位不能修改该字段或该社区数据")
    role_class = _role_class(user)
    if role_class == "area" and parser.COMMUNITY_COLUMN in columns:
        allowed = capabilities["edit_communities"]
        formal_after = await formal_community(cur, parser.community_value(after))
        if allowed is not None and (not formal_after or formal_after not in allowed):
            raise PermissionError("片长不能把数据移出本人负责片区")


def can_manage_rows(user: dict[str, Any]) -> bool:
    return (
        has_permission(user, ONLINE_RAW_ROW_MANAGE)
        and _role_class(user) == "global"
    )


async def validate_new_row_scope(
    cur,
    user: dict[str, Any],
    parser,
    values: dict[str, str],
) -> str:
    if not can_manage_rows(user):
        raise PermissionError("当前岗位不能新增或删除腾讯原始行")
    formal = await formal_community(cur, parser.community_value(values))
    if parser.COMMUNITY_COLUMN in parser.COLUMNS and not formal:
        raise ValueError("请选择系统中已登记的正式社区")
    return formal
