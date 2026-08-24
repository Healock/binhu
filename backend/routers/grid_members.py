"""人员管理 API"""

import csv
import io
import json
import bcrypt
from datetime import date, timedelta
from fastapi import APIRouter, Depends, Query, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal, Optional
from database import get_db
from deps import require_permission, require_super_admin
from services.audit import record_admin_audit, request_audit_fields
from services.grid_member_status import (
    apply_weekend_duty_status,
    get_business_date,
    get_status_snapshot,
    validate_leave_period,
)
from services.personnel_positions import (
    POSITION_CATEGORIES,
    WEEKEND_DUTY_POSITION_CONFIG_KEY,
    get_configured_positions,
    normalize_position,
)
from services.privacy import mask_identity_number
from services.qmf_community import (
    normalize_qmf_community_code,
    valid_qmf_community_code,
)
from services.member_departments import (
    get_member_departments,
    replace_member_departments,
    resolve_departments,
    sync_community_police_compat,
)
from services.visit_import import normalize_community, normalize_identity
from services.permissions import (
    ATTENDANCE_MANAGE,
    COMMUNITY_MANAGE,
    COMMUNITY_POSITIONS,
    COMMUNITY_VIEW,
    INTERNAL_POSITIONS,
    PERSONNEL_BASIC_VIEW,
    PERSONNEL_MANAGE,
    PERSONNEL_SENSITIVE_VIEW,
    has_permission,
)
from services.session_management import (
    invalidate_all_sessions,
    invalidate_all_sessions_for_users,
)

router = APIRouter(prefix="/api/grid-members", tags=["人员管理"])

# 有"核查人"列的业务表
TABLES_WITH_INSPECTOR = [
    "t_fullchain", "t_rental_check", "t_delivery_industry",
    "t_suspect_unrevoked", "t_suspect_return", "t_group_rental",
]


class GridMemberCreate(BaseModel):
    name: str
    community: str = ""
    department_id: Optional[int] = None
    department_ids: Optional[list[int]] = Field(default=None, max_length=30)
    position: str = "组员"
    phone: str = ""
    id_card_number: Optional[str] = Field(default=None, max_length=50)
    notes: str = ""
    status: Literal["在岗", "离岗"] = "在岗"
    leave_start_date: Optional[date] = None
    leave_end_date: Optional[date] = None
    leave_reason: str = Field(default="", max_length=200)
    leave_source: str = Field(default="manual", max_length=30)
    account_mode: Literal["existing", "create"]
    existing_user_id: Optional[int] = Field(default=None, gt=0)
    username: Optional[str] = Field(default=None, min_length=2, max_length=50)
    password: Optional[str] = Field(default=None, min_length=8, max_length=200)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("姓名不能为空")
        return normalized

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        if len(normalized) < 2:
            raise ValueError("登录用户名至少需要 2 个字符")
        return normalized

    @field_validator("position")
    @classmethod
    def validate_position(cls, value: str) -> str:
        return normalize_position(value)

    @field_validator("id_card_number")
    @classmethod
    def validate_id_card_number(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        normalized, valid, _ = normalize_identity(value)
        if not valid:
            raise ValueError("身份证号必须是有效的 15 位或 18 位号码")
        return normalized

    @model_validator(mode="after")
    def validate_leave_dates(self):
        validate_leave_period(self.leave_start_date, self.leave_end_date)
        if self.account_mode == "existing" and self.existing_user_id is None:
            raise ValueError("请选择要关联的已有账号")
        if self.account_mode == "create" and (not self.username or not self.password):
            raise ValueError("请填写新账号用户名和初始密码")
        return self


class GridMemberUpdate(BaseModel):
    community: Optional[str] = None
    department_id: Optional[int] = None
    department_ids: Optional[list[int]] = Field(default=None, max_length=30)
    position: Optional[str] = None
    phone: Optional[str] = None
    id_card_number: Optional[str] = Field(default=None, max_length=50)
    account_id: Optional[int] = Field(default=None, gt=0)
    notes: Optional[str] = None
    status: Optional[Literal["在岗", "离岗"]] = None
    leave_start_date: Optional[date] = None
    leave_end_date: Optional[date] = None
    leave_reason: Optional[str] = Field(default=None, max_length=200)
    leave_source: Optional[str] = Field(default=None, max_length=30)

    @field_validator("position")
    @classmethod
    def validate_position(cls, value: Optional[str]) -> Optional[str]:
        return normalize_position(value) if value is not None else None

    @field_validator("id_card_number")
    @classmethod
    def validate_id_card_number(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        normalized, valid, _ = normalize_identity(value)
        if not valid:
            raise ValueError("身份证号必须是有效的 15 位或 18 位号码")
        return normalized


class GridMemberLeaveUpdate(BaseModel):
    action: Literal["temporary", "long_term", "clear"]
    leave_start_date: Optional[date] = None
    leave_end_date: Optional[date] = None
    leave_reason: str = Field(default="", max_length=200)

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "temporary":
            validate_leave_period(
                self.leave_start_date,
                self.leave_end_date,
            )
            if not self.leave_start_date:
                raise ValueError("临时请假需要选择日期")
        elif self.leave_start_date or self.leave_end_date:
            raise ValueError("长期或恢复正常不需要填写日期")
        return self


class CommunityAliasesUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    police_officers: Optional[list[str]] = Field(default=None, max_length=20)
    police_officer_ids: Optional[list[int]] = Field(default=None, max_length=50)
    area_id: Optional[int] = Field(default=None, gt=0)
    qmf_community_code: Optional[str] = Field(default=None, max_length=20)
    qmf_organization_codes: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = normalize_community(value)
        if not normalized:
            raise ValueError("社区名不能为空")
        return normalized

    @field_validator("qmf_community_code")
    @classmethod
    def validate_qmf_community_code(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = normalize_qmf_community_code(value)
        if normalized and not valid_qmf_community_code(normalized):
            raise ValueError("全民防社区代码必须为 10 位大写字母或数字")
        return normalized

    @field_validator("qmf_organization_codes")
    @classmethod
    def normalize_qmf_organization_codes(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = str(value or "").strip().upper()
            if not item:
                continue
            if len(item) > 50 or not item.isalnum():
                raise ValueError("全民防组织编码只能填写字母或数字，长度不超过 50 位")
            if item not in normalized:
                normalized.append(item)
        return normalized
    @field_validator("aliases")
    @classmethod
    def normalize_aliases(cls, aliases: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw_alias in aliases:
            alias = normalize_community(raw_alias)
            if not alias:
                raise ValueError("社区别名不能为空")
            if len(alias) > 200:
                raise ValueError("社区别名不能超过 200 个字符")
            if alias not in normalized:
                normalized.append(alias)
        return normalized

    @field_validator("police_officers")
    @classmethod
    def normalize_police_officers(
        cls,
        police_officers: Optional[list[str]],
    ) -> Optional[list[str]]:
        if police_officers is None:
            return None
        normalized: list[str] = []
        for raw_name in police_officers:
            name = str(raw_name).strip()
            if not name:
                raise ValueError("社区民警姓名不能为空")
            if len(name) > 100:
                raise ValueError("社区民警姓名不能超过 100 个字符")
            if "、" in name:
                raise ValueError("请把多位社区民警分别添加，不要在姓名中使用顿号")
            if name not in normalized:
                normalized.append(name)
        return normalized


class CommunityStatusUpdate(BaseModel):
    is_active: bool


class AreaWrite(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    leader_ids: list[int] = Field(default_factory=list, max_length=20)

    @field_validator("name")
    @classmethod
    def normalize_area_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("片区名称不能为空")
        return normalized

    @field_validator("leader_ids")
    @classmethod
    def normalize_leaders(cls, values: list[int]) -> list[int]:
        return list(dict.fromkeys(values))


def _parse_police_officers(value) -> list[str]:
    if value in (None, ""):
        return []
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    if not isinstance(parsed, list):
        return []
    return [
        str(name).strip()
        for name in parsed
        if str(name).strip()
    ]


async def _resolve_department(cur, position: str, department_id: int | None):
    """按岗位规则确定部门，并返回 (部门 ID, 兼容社区字段)。"""
    if position in INTERNAL_POSITIONS:
        await cur.execute(
            "SELECT id FROM _departments "
            "WHERE department_type='internal' ORDER BY id LIMIT 1"
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(500, "内勤部门尚未初始化")
        return int(row[0]), ""
    if department_id is None:
        if position in COMMUNITY_POSITIONS:
            raise HTTPException(400, "该岗位必须选择社区部门")
        return None, ""
    await cur.execute(
        """
        SELECT department.id, department.department_type, community.name
        FROM _departments AS department
        LEFT JOIN _communities AS community
          ON community.id=department.community_id
        WHERE department.id=%s
        """,
        (department_id,),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(400, "所属部门不存在")
    if row[1] != "community":
        raise HTTPException(400, "该岗位只能选择社区部门")
    return int(row[0]), str(row[2] or "")


async def _resolved_departments_for_payload(
    cur,
    position: str,
    department_ids: list[int] | None,
    department_id: int | None,
) -> list[dict]:
    selected = department_ids
    if selected is None:
        selected = [department_id] if department_id is not None else []
    return await resolve_departments(cur, position, selected)


async def _position_primary_group(cur, position: str) -> tuple[int, str, str]:
    await cur.execute(
        """
        SELECT permission_group.id, permission_group.code,
               permission_group.name
        FROM _position_permission_group_links AS link
        JOIN _permission_groups AS permission_group
          ON permission_group.id=link.permission_group_id
        WHERE link.position=%s
        ORDER BY permission_group.sort_order, permission_group.id
        LIMIT 1
        """,
        (position,),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(400, "该岗位尚未配置权限组")
    return int(row[0]), str(row[1]), str(row[2])


def _legacy_role_for_group(code: str) -> str:
    if code == "admin" or code == "internal_business":
        return "admin"
    if code == "global_viewer":
        return "leader"
    return "member"


async def _refresh_inherited_account_group(cur, member_id: int) -> None:
    await cur.execute(
        "SELECT position FROM _grid_members WHERE id=%s",
        (member_id,),
    )
    row = await cur.fetchone()
    if not row:
        return
    group_id, group_code, _ = await _position_primary_group(cur, str(row[0]))
    await cur.execute(
        "DELETE links FROM _user_permission_group_links AS links "
        "JOIN _users AS user ON user.id=links.user_id "
        "WHERE user.member_id=%s",
        (member_id,),
    )
    await cur.execute(
        "SELECT id FROM _users WHERE member_id=%s "
        "AND group_assignment_mode='inherited' FOR UPDATE",
        (member_id,),
    )
    account_ids = [int(item[0]) for item in await cur.fetchall()]
    for account_id in account_ids:
        await invalidate_all_sessions(cur, account_id)
    await cur.execute(
        """
        UPDATE _users AS user
        SET user.permission_group_id=%s,
            user.group_assignment_mode='inherited',
            user.role=%s
        WHERE user.member_id=%s
        """,
        (group_id, _legacy_role_for_group(group_code), member_id),
    )


def _member_to_dict(
    row,
    business_date: date,
    *,
    sensitive: bool,
    identity_access: bool,
    departments: list[dict] | None = None,
    account: dict | None = None,
    weekend_duty_positions: set[str] | None = None,
    weekend_duty_recorded: bool = False,
    weekend_duty_date: date | None = None,
) -> dict:
    departments = departments or []
    primary_department = departments[0] if departments else None
    snapshot = get_status_snapshot(row[6], row[7], row[8], business_date)
    snapshot = apply_weekend_duty_status(
        snapshot,
        position=str(row[3]),
        as_of=business_date,
        duty_positions=weekend_duty_positions or set(),
        duty_recorded=weekend_duty_recorded,
        duty_date=weekend_duty_date,
    )
    result = {
        "id": row[0],
        "name": row[1],
        "community": row[2],
        "position": row[3],
        "phone": row[4],
        "notes": row[5],
        "status": row[6],
        "leave_start_date": row[7],
        "leave_end_date": row[8],
        "leave_reason": row[9],
        "leave_source": row[10],
        "department": primary_department,
        "department_id": primary_department["id"] if primary_department else None,
        "departments": departments,
        "department_ids": [item["id"] for item in departments],
        "community_names": [
            item["community_name"] for item in departments
            if item.get("community_name")
        ],
        "account": account,
        **snapshot,
    }
    if sensitive:
        result.update({
            "phone": row[4],
            "notes": row[5],
            "leave_start_date": row[7],
            "leave_end_date": row[8],
            "leave_reason": row[9],
            "leave_source": row[10],
        })
    else:
        for field in (
            "notes", "leave_start_date", "leave_end_date", "leave_reason",
            "leave_source",
        ):
            result.pop(field, None)
    if identity_access:
        result.update({
            "has_id_card": bool(row[11]),
            "id_card_masked": mask_identity_number(row[11]),
            "id_card_number": str(row[11] or ""),
        })
    return result


@router.get("")
async def list_members(
    keyword: Optional[str] = Query(None),
    community: Optional[str] = Query(None),
    position: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    user: dict = Depends(require_permission(PERSONNEL_BASIC_VIEW)),
    conn=Depends(get_db),
):
    """列表查询"""
    where_parts = []
    params = []
    can_view_sensitive = has_permission(user, PERSONNEL_SENSITIVE_VIEW)
    can_manage_identity = str(user.get("role") or "") == "super_admin"
    if keyword:
        if can_view_sensitive:
            where_parts.append(
                "(member.name LIKE %s OR member.phone LIKE %s OR "
                "member.notes LIKE %s OR member.position LIKE %s)"
            )
            params.extend([f"%{keyword}%"] * 4)
        else:
            where_parts.append(
                "(member.name LIKE %s OR member.phone LIKE %s OR "
                "member.position LIKE %s OR EXISTS ("
                "SELECT 1 FROM _grid_member_department_links AS keyword_link "
                "JOIN _departments AS keyword_department "
                "ON keyword_department.id=keyword_link.department_id "
                "WHERE keyword_link.member_id=member.id "
                "AND keyword_department.name LIKE %s))"
            )
            params.extend([f"%{keyword}%"] * 4)
    if community:
        where_parts.append(
            "EXISTS (SELECT 1 FROM _grid_member_department_links AS filter_link "
            "JOIN _departments AS filter_department "
            "ON filter_department.id=filter_link.department_id "
            "JOIN _communities AS filter_community "
            "ON filter_community.id=filter_department.community_id "
            "WHERE filter_link.member_id=member.id "
            "AND filter_community.name=%s)"
        )
        params.append(community)
    if position:
        try:
            position = normalize_position(position)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        where_parts.append("member.position = %s")
        params.append(position)
    base_where_parts = list(where_parts)
    base_params = list(params)
    if category:
        category_positions = POSITION_CATEGORIES.get(category)
        if not category_positions:
            raise HTTPException(400, "未知的人员分类")
        placeholders = ", ".join(["%s"] * len(category_positions))
        where_parts.append(f"member.position IN ({placeholders})")
        params.extend(category_positions)
    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

    async with conn.cursor() as cur:
        business_date = await get_business_date(cur)
        joins = """
            FROM _grid_members AS member
            LEFT JOIN _departments AS department
              ON department.id=member.department_id
            LEFT JOIN _communities AS community
              ON community.id=department.community_id
        """
        await cur.execute(f"SELECT COUNT(*) {joins}{where}", params)
        total = (await cur.fetchone())[0]
        base_where = (
            f" WHERE {' AND '.join(base_where_parts)}"
            if base_where_parts else ""
        )
        await cur.execute(
            f"""
            SELECT
                CASE
                    WHEN member.position IN ('组员','组长','自购房','片长')
                        THEN 'flow_work'
                    WHEN member.position IN ('基础管控','中队长')
                        THEN 'internal_business'
                    WHEN member.position IN ('社区民警','所队领导')
                        THEN 'police_leadership'
                    ELSE 'other'
                END AS category_code,
                COUNT(*)
            {joins}{base_where}
            GROUP BY category_code
            """,
            base_params,
        )
        category_counts = {
            str(row[0]): int(row[1]) for row in await cur.fetchall()
        }
        offset = (page - 1) * page_size
        await cur.execute(
            f"SELECT member.id, member.name, "
            f"COALESCE(community.name, member.community), member.position, "
            f"member.phone, member.notes, member.status, "
            f"member.leave_start_date, member.leave_end_date, "
            f"member.leave_reason, member.leave_source, member.id_card_number, "
            f"department.id, department.name, department.department_type, "
            f"community.name {joins}{where} "
            f"ORDER BY department.name, member.name LIMIT %s OFFSET %s",
            params + [page_size, offset],
        )
        rows = await cur.fetchall()
        member_ids = [int(row[0]) for row in rows]
        departments_by_member = await get_member_departments(cur, member_ids)
        accounts_by_member: dict[int, dict] = {}
        weekend_duty_positions: set[str] = set()
        weekend_duty_records: dict[int, date | None] = {}
        if member_ids:
            placeholders = ", ".join(["%s"] * len(member_ids))
            await cur.execute(
                f"SELECT id, username, display_name, member_id "
                f"FROM _users WHERE member_id IN ({placeholders})",
                member_ids,
            )
            accounts_by_member = {
                int(account_row[3]): {
                    "id": int(account_row[0]),
                    "username_masked": mask_identity_number(account_row[1]),
                    "display_name": str(account_row[2] or ""),
                }
                for account_row in await cur.fetchall()
            }
            if business_date.weekday() >= 5:
                weekend_duty_positions = set(await get_configured_positions(
                    cur,
                    WEEKEND_DUTY_POSITION_CONFIG_KEY,
                ))
                week_start = business_date - timedelta(days=business_date.weekday())
                placeholders = ", ".join(["%s"] * len(member_ids))
                await cur.execute(
                    f"SELECT member_id, duty_date "
                    f"FROM _personnel_weekend_duty "
                    f"WHERE week_start=%s AND member_id IN ({placeholders})",
                    [week_start, *member_ids],
                )
                weekend_duty_records = {
                    int(duty_row[0]): duty_row[1]
                    for duty_row in await cur.fetchall()
                }

    return {
        "data": [
            _member_to_dict(
                row,
                business_date,
                sensitive=can_view_sensitive,
                identity_access=can_manage_identity,
                departments=departments_by_member.get(int(row[0]), []),
                account=accounts_by_member.get(int(row[0])),
                weekend_duty_positions=weekend_duty_positions,
                weekend_duty_recorded=int(row[0]) in weekend_duty_records,
                weekend_duty_date=weekend_duty_records.get(int(row[0])),
            )
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "as_of_date": business_date,
        "category_counts": category_counts,
    }


@router.get("/departments")
async def list_departments(
    user: dict = Depends(require_permission(PERSONNEL_BASIC_VIEW)),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT department.id, department.name,
                   department.department_type, community.name,
                   CASE WHEN department.department_type='internal' THEN 1
                        ELSE COALESCE(community.is_active, 0) END AS is_active
            FROM _departments AS department
            LEFT JOIN _communities AS community
              ON community.id=department.community_id
            ORDER BY department.department_type, department.name
            """
        )
        rows = await cur.fetchall()
    return {"data": [
        {
            "id": row[0], "name": row[1], "type": row[2],
            "community_name": row[3],
            "is_active": bool(row[4]),
        }
        for row in rows
    ]}


@router.get("/communities")
async def list_communities(
    user: dict = Depends(require_permission(COMMUNITY_VIEW)),
    conn=Depends(get_db),
):
    """获取社区列表（人员数量由 _grid_members 表实时统计）"""
    del user
    async with conn.cursor() as cur:
        await cur.execute(f"""
            SELECT c.id, c.name, c.police_officers,
                   COALESCE(g.grid_count, 0) AS grid_count,
                   area.id, area.name, c.is_active, c.qmf_community_code
            FROM _communities c
            LEFT JOIN _areas AS area ON area.id=c.area_id
            LEFT JOIN (
                SELECT department.community_id,
                       COUNT(DISTINCT link.member_id) AS grid_count
                FROM _grid_member_department_links AS link
                JOIN _departments AS department
                  ON department.id=link.department_id
                WHERE department.department_type='community'
                GROUP BY department.community_id
            ) AS g ON g.community_id = c.id
            ORDER BY c.name
        """)
        rows = await cur.fetchall()
        await cur.execute(
            "SELECT community_id, alias FROM _community_aliases "
            "ORDER BY community_id, alias"
        )
        alias_rows = await cur.fetchall()
        await cur.execute(
            "SELECT community_id, organization_code FROM _qmf_organization_codes "
            "WHERE is_active=1 ORDER BY community_id, organization_code"
        )
        organization_rows = await cur.fetchall()
        await cur.execute(
            """
            SELECT department.community_id, member.id, member.name
            FROM _grid_member_department_links AS link
            JOIN _departments AS department ON department.id=link.department_id
            JOIN _grid_members AS member ON member.id=link.member_id
            WHERE member.position='社区民警'
              AND department.community_id IS NOT NULL
            ORDER BY department.community_id, member.name
            """
        )
        officer_rows = await cur.fetchall()
    aliases_by_community: dict[int, list[str]] = {}
    for community_id, alias in alias_rows:
        aliases_by_community.setdefault(community_id, []).append(alias)
    officers_by_community: dict[int, list[dict]] = {}
    for community_id, member_id, member_name in officer_rows:
        officers_by_community.setdefault(int(community_id), []).append({
            "id": int(member_id),
            "name": str(member_name),
        })
    organizations_by_community: dict[int, list[str]] = {}
    for community_id, organization_code in organization_rows:
        organizations_by_community.setdefault(int(community_id), []).append(
            str(organization_code or "").strip()
        )
    return {
        "data": [
            {
                "id": row[0],
                "name": row[1],
                "police_officers": (
                    [item["name"] for item in officers_by_community.get(int(row[0]), [])]
                    or _parse_police_officers(row[2])
                ),
                "police_officer_ids": [
                    item["id"] for item in officers_by_community.get(int(row[0]), [])
                ],
                "grid_count": row[3],
                "aliases": aliases_by_community.get(row[0], []),
                "area_id": int(row[4]) if row[4] is not None else None,
                "area_name": str(row[5] or ""),
                "is_active": bool(row[6]),
                "qmf_community_code": str(row[7] or ""),
                "qmf_organization_codes": organizations_by_community.get(int(row[0]), []),
            }
            for row in rows
        ]
    }


@router.get("/areas")
async def list_areas(
    user: dict = Depends(require_permission(COMMUNITY_VIEW)),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT area.id, area.name,
                   COUNT(DISTINCT community.id) AS community_count
            FROM _areas AS area
            LEFT JOIN _communities AS community ON community.area_id=area.id
            GROUP BY area.id, area.name
            ORDER BY area.name
            """
        )
        area_rows = await cur.fetchall()
        await cur.execute(
            """
            SELECT link.area_id, member.id, member.name
            FROM _area_leader_links AS link
            JOIN _grid_members AS member ON member.id=link.member_id
            WHERE member.position='片长'
            ORDER BY link.area_id, member.name
            """
        )
        leaders: dict[int, list[dict]] = {}
        for area_id, member_id, name in await cur.fetchall():
            leaders.setdefault(int(area_id), []).append({
                "id": int(member_id),
                "name": str(name),
            })
    return {"data": [
        {
            "id": int(row[0]),
            "name": str(row[1]),
            "community_count": int(row[2] or 0),
            "leaders": leaders.get(int(row[0]), []),
            "leader_ids": [
                item["id"] for item in leaders.get(int(row[0]), [])
            ],
        }
        for row in area_rows
    ]}


@router.get("/area-leader-options")
async def list_area_leader_options(
    user: dict = Depends(require_permission(COMMUNITY_VIEW)),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, name FROM _grid_members "
            "WHERE position='片长' ORDER BY name"
        )
        rows = await cur.fetchall()
    return {"data": [
        {"id": int(row[0]), "name": str(row[1])}
        for row in rows
    ]}


async def _replace_area_leaders(cur, area_id: int, leader_ids: list[int]) -> None:
    if leader_ids:
        placeholders = ", ".join(["%s"] * len(leader_ids))
        await cur.execute(
            f"SELECT id FROM _grid_members "
            f"WHERE position='片长' AND id IN ({placeholders})",
            leader_ids,
        )
        valid = {int(row[0]) for row in await cur.fetchall()}
        if valid != set(leader_ids):
            raise HTTPException(400, "片区负责人只能选择人员管理中的片长")
    await cur.execute(
        "DELETE FROM _area_leader_links WHERE area_id=%s",
        (area_id,),
    )
    if leader_ids:
        await cur.executemany(
            "INSERT INTO _area_leader_links (area_id, member_id) "
            "VALUES (%s, %s)",
            [(area_id, leader_id) for leader_id in leader_ids],
        )


@router.post("/areas")
async def create_area(
    data: AreaWrite,
    request: Request,
    user: dict = Depends(require_permission(COMMUNITY_MANAGE)),
    conn=Depends(get_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            try:
                await cur.execute("INSERT INTO _areas (name) VALUES (%s)", (data.name,))
            except Exception as exc:
                if "Duplicate" in str(exc):
                    raise HTTPException(400, "该片区名称已存在") from exc
                raise
            area_id = int(cur.lastrowid)
            await _replace_area_leaders(cur, area_id, data.leader_ids)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "area.create",
        target_type="area",
        target_name=data.name,
        detail={"area_id": area_id, "leader_count": len(data.leader_ids)},
        **request_audit_fields(request),
    )
    return {"id": area_id, "message": "片区已创建"}


@router.put("/areas/{area_id}")
async def update_area(
    area_id: int,
    data: AreaWrite,
    request: Request,
    user: dict = Depends(require_permission(COMMUNITY_MANAGE)),
    conn=Depends(get_db),
):
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            try:
                await cur.execute(
                    "UPDATE _areas SET name=%s WHERE id=%s",
                    (data.name, area_id),
                )
            except Exception as exc:
                if "Duplicate" in str(exc):
                    raise HTTPException(400, "该片区名称已存在") from exc
                raise
            if cur.rowcount == 0:
                raise HTTPException(404, "片区不存在")
            await _replace_area_leaders(cur, area_id, data.leader_ids)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "area.update",
        target_type="area",
        target_name=data.name,
        detail={"area_id": area_id, "leader_count": len(data.leader_ids)},
        **request_audit_fields(request),
    )
    return {"message": "片区已保存"}


@router.delete("/areas/{area_id}")
async def delete_area(
    area_id: int,
    request: Request,
    user: dict = Depends(require_permission(COMMUNITY_MANAGE)),
    conn=Depends(get_db),
):
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT name FROM _areas WHERE id=%s",
            (area_id,),
        )
        row = await cur.fetchone()
        if not row:
            raise HTTPException(404, "片区不存在")
        await cur.execute(
            "SELECT COUNT(*) FROM _communities WHERE area_id=%s",
            (area_id,),
        )
        if int((await cur.fetchone())[0] or 0):
            raise HTTPException(409, "该片区仍有关联社区，不能删除")
        await cur.execute(
            "DELETE FROM _area_leader_links WHERE area_id=%s",
            (area_id,),
        )
        await cur.execute("DELETE FROM _areas WHERE id=%s", (area_id,))
    await record_admin_audit(
        user,
        "area.delete",
        target_type="area",
        target_name=str(row[0]),
        detail={"area_id": area_id},
        **request_audit_fields(request),
    )
    return {"message": "片区已删除"}


@router.get("/unlinked-accounts")
async def list_unlinked_accounts(
    user: dict = Depends(require_permission(PERSONNEL_MANAGE)),
    conn=Depends(get_db),
):
    """人员新增时可关联的普通账号，不返回权限或其他敏感资料。"""
    del user
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT user.id, user.username, user.display_name
            FROM _users AS user
            WHERE user.member_id IS NULL
              AND user.role<>'super_admin'
            ORDER BY user.display_name, user.username
            """
        )
        rows = await cur.fetchall()
    return {"data": [
        {
            "id": int(row[0]),
            "username_masked": mask_identity_number(row[1]),
            "display_name": str(row[2] or row[1]),
        }
        for row in rows
    ]}


@router.get("/account-options")
async def list_account_options(
    member_id: int = Query(..., gt=0),
    user: dict = Depends(require_permission(PERSONNEL_MANAGE)),
    conn=Depends(get_db),
):
    """编辑人员时可选择的普通账号；已关联账号会标明当前人员，用于安全互换。"""
    del user
    async with conn.cursor() as cur:
        await cur.execute("SELECT id FROM _grid_members WHERE id=%s", (member_id,))
        if not await cur.fetchone():
            raise HTTPException(404, "人员不存在")
        await cur.execute(
            """
            SELECT account.id, account.username, account.display_name,
                   account.member_id, member.name
            FROM _users AS account
            LEFT JOIN _grid_members AS member ON member.id=account.member_id
            WHERE account.role<>'super_admin'
            ORDER BY account.member_id IS NULL DESC,
                     account.display_name, account.username
            """
        )
        rows = await cur.fetchall()
    return {"data": [
        {
            "id": int(row[0]),
            "username_masked": mask_identity_number(row[1]),
            "display_name": str(row[2] or row[1]),
            "linked_member_id": int(row[3]) if row[3] is not None else None,
            "linked_member_name": str(row[4] or ""),
            "is_current": row[3] is not None and int(row[3]) == member_id,
        }
        for row in rows
    ]}


@router.get("/community-police-options")
async def list_community_police_options(
    user: dict = Depends(require_permission(COMMUNITY_VIEW)),
    conn=Depends(get_db),
):
    del user
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id, name FROM _grid_members "
            "WHERE position='社区民警' ORDER BY name"
        )
        rows = await cur.fetchall()
    return {"data": [
        {"id": int(row[0]), "name": str(row[1])}
        for row in rows
    ]}


@router.post("/communities")
async def add_community(
    name: str = Query(...),
    user: dict = Depends(require_permission(COMMUNITY_MANAGE)),
    conn=Depends(get_db),
):
    """添加社区"""
    del user
    name = normalize_community(name)
    if not name:
        raise HTTPException(400, "社区名不能为空")
    async with conn.cursor() as cur:
        await cur.execute("SELECT name FROM _communities")
        existing_names = {
            normalize_community(row[0])
            for row in await cur.fetchall()
        }
        if name in existing_names:
            raise HTTPException(400, "该社区已存在")
        await cur.execute("SELECT alias FROM _community_aliases")
        existing_aliases = {
            normalize_community(row[0])
            for row in await cur.fetchall()
        }
        if name in existing_aliases:
            raise HTTPException(400, "该名称已经是其他社区的别名")
        try:
            await cur.execute("INSERT INTO _communities (name) VALUES (%s)", (name,))
            community_id = int(cur.lastrowid)
            await cur.execute(
                "INSERT INTO _departments "
                "(name, department_type, community_id) "
                "VALUES (%s, 'community', %s)",
                (name, community_id),
            )
        except Exception as e:
            if "Duplicate" in str(e):
                raise HTTPException(400, "该社区已存在")
            raise
    return {"message": "添加成功"}


@router.delete("/communities/{community_id}")
async def delete_community(
    community_id: int,
    user: dict = Depends(require_permission(COMMUNITY_MANAGE)),
    conn=Depends(get_db),
):
    """删除社区"""
    del user
    async with conn.cursor() as cur:
        await cur.execute(
            """
            SELECT COUNT(*)
            FROM _grid_member_department_links AS link
            JOIN _departments AS department
              ON department.id=link.department_id
            WHERE department.community_id=%s
            """,
            (community_id,),
        )
        if int((await cur.fetchone())[0] or 0):
            raise HTTPException(409, "该社区部门仍有人员，不能删除")
        await cur.execute(
            "SELECT COUNT(*) FROM _police_address_entries WHERE community_id=%s",
            (community_id,),
        )
        address_count = int((await cur.fetchone())[0] or 0)
        await cur.execute(
            """
            SELECT COUNT(*) FROM _police_dispatch_tasks
            WHERE suggested_community_id=%s OR final_community_id=%s
            """,
            (community_id, community_id),
        )
        task_count = int((await cur.fetchone())[0] or 0)
        if address_count or task_count:
            raise HTTPException(
                409,
                "该社区已被小区地址库或历史批次引用，请改为停用",
            )
        await cur.execute(
            "DELETE FROM _departments WHERE community_id=%s",
            (community_id,),
        )
        await cur.execute("DELETE FROM _communities WHERE id=%s", (community_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "社区不存在")
    return {"message": "删除成功"}


@router.patch("/communities/{community_id}/status")
async def update_community_status(
    community_id: int,
    data: CommunityStatusUpdate,
    request: Request,
    user: dict = Depends(require_permission(COMMUNITY_MANAGE)),
    conn=Depends(get_db),
):
    """启用或停用社区；历史关系保留，停用前必须清空当前归属。"""
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT name, is_active FROM _communities WHERE id=%s FOR UPDATE",
                (community_id,),
            )
            current = await cur.fetchone()
            if not current:
                raise HTTPException(404, "社区不存在")
            if bool(current[1]) == data.is_active:
                await conn.commit()
                return {
                    "message": "社区状态未变化",
                    "is_active": data.is_active,
                }
            if not data.is_active:
                await cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM _grid_member_department_links AS link
                    JOIN _departments AS department
                      ON department.id=link.department_id
                    WHERE department.community_id=%s
                    """,
                    (community_id,),
                )
                member_count = int((await cur.fetchone())[0] or 0)
                await cur.execute(
                    """
                    SELECT COUNT(*) FROM _police_dispatch_tasks
                    WHERE task_status<>'completed'
                      AND (suggested_community_id=%s OR final_community_id=%s)
                    """,
                    (community_id, community_id),
                )
                pending_count = int((await cur.fetchone())[0] or 0)
                if member_count or pending_count:
                    raise HTTPException(
                        409,
                        detail={
                            "message": "请先转移社区人员并处理未完成下发任务",
                            "member_count": member_count,
                            "pending_task_count": pending_count,
                        },
                    )
            active_value = 1 if data.is_active else 0
            await cur.execute(
                "UPDATE _communities SET is_active=%s WHERE id=%s",
                (active_value, community_id),
            )
            await cur.execute(
                "UPDATE _departments SET is_active=%s "
                "WHERE community_id=%s AND department_type='community'",
                (active_value, community_id),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "community.enable" if data.is_active else "community.disable",
        target_type="community",
        target_name=str(community_id),
        detail={"is_active": data.is_active},
        **request_audit_fields(request),
    )
    return {
        "message": "社区已启用" if data.is_active else "社区已停用",
        "is_active": data.is_active,
    }


@router.put("/communities/{community_id}/aliases")
async def update_community_aliases(
    community_id: int,
    data: CommunityAliasesUpdate,
    user: dict = Depends(require_permission(COMMUNITY_MANAGE)),
    conn=Depends(get_db),
):
    """设置社区别名和民警，并把已导入走访数据归到正式名称。"""
    del user
    returned_officers = data.police_officers
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id, name FROM _communities")
            community_rows = await cur.fetchall()
            communities = {
                row[0]: {
                    "name": str(row[1]).strip(),
                    "normalized_name": normalize_community(row[1]),
                }
                for row in community_rows
            }
            current = communities.get(community_id)
            if not current:
                raise HTTPException(404, "社区不存在")

            if "area_id" in data.model_fields_set:
                await cur.execute(
                    "SELECT id FROM _areas WHERE id=%s",
                    (data.area_id,),
                )
                if not await cur.fetchone():
                    raise HTTPException(400, "所选片区不存在")
                await cur.execute(
                    "UPDATE _communities SET area_id=%s WHERE id=%s",
                    (data.area_id, community_id),
                )

            if "qmf_community_code" in data.model_fields_set:
                await cur.execute(
                    "UPDATE _communities SET qmf_community_code=%s WHERE id=%s",
                    (data.qmf_community_code or None, community_id),
                )

            if "qmf_organization_codes" in data.model_fields_set:
                requested_codes = set(data.qmf_organization_codes)
                if requested_codes:
                    placeholders = ",".join(["%s"] * len(requested_codes))
                    await cur.execute(
                        f"SELECT community_id, organization_code "
                        f"FROM _qmf_organization_codes "
                        f"WHERE organization_code IN ({placeholders}) "
                        "AND community_id<>%s AND is_active=1",
                        [*sorted(requested_codes), community_id],
                    )
                    conflict = await cur.fetchone()
                    if conflict:
                        raise HTTPException(
                            400,
                            f"组织编码“{conflict[1]}”已绑定其他社区",
                        )
                await cur.execute(
                    "UPDATE _qmf_organization_codes SET is_active=0 "
                    "WHERE community_id=%s",
                    (community_id,),
                )
                if requested_codes:
                    await cur.executemany(
                        """
                        INSERT INTO _qmf_organization_codes (
                            community_id, organization_code, source, is_active
                        ) VALUES (%s, %s, 'manual', 1)
                        ON DUPLICATE KEY UPDATE
                            community_id=VALUES(community_id),
                            is_active=1,
                            source='manual'
                        """,
                        [(community_id, code) for code in sorted(requested_codes)],
                    )

            target_name = data.name or current["name"]
            target_normalized_name = normalize_community(target_name)
            aliases = list(data.aliases)
            if target_name != current["name"]:
                aliases.append(current["name"])
            aliases = list(dict.fromkeys(
                alias for alias in aliases
                if alias != target_normalized_name
            ))

            for other_id, other in communities.items():
                if (
                    other_id != community_id
                    and target_normalized_name == other["normalized_name"]
                ):
                    raise HTTPException(400, "该社区名称已存在")

            for alias in aliases:
                if alias == target_normalized_name:
                    raise HTTPException(400, "别名不能与社区正式名称相同")
                for other_id, other in communities.items():
                    if (
                        other_id != community_id
                        and alias == other["normalized_name"]
                    ):
                        raise HTTPException(
                            400,
                            f"别名“{alias}”与社区“{other['name']}”的正式名称冲突",
                        )

            if aliases:
                placeholders = ", ".join(["%s"] * len(aliases))
                await cur.execute(
                    f"""
                    SELECT a.alias, c.id, c.name
                    FROM _community_aliases AS a
                    JOIN _communities AS c ON c.id = a.community_id
                    WHERE a.alias IN ({placeholders})
                    """,
                    aliases,
                )
                for alias, owner_id, owner_name in await cur.fetchall():
                    if owner_id != community_id:
                        raise HTTPException(
                            400,
                            f"别名“{alias}”已经属于社区“{owner_name}”",
                        )

            await cur.execute(
                "DELETE FROM _community_aliases WHERE community_id=%s",
                (community_id,),
            )
            if aliases:
                await cur.executemany(
                    "INSERT INTO _community_aliases (community_id, alias) "
                    "VALUES (%s, %s)",
                    [(community_id, alias) for alias in aliases],
                )

            if target_name != current["name"]:
                try:
                    await cur.execute(
                        "UPDATE _communities SET name=%s WHERE id=%s",
                        (target_name, community_id),
                    )
                    await cur.execute(
                        "UPDATE _departments SET name=%s "
                        "WHERE community_id=%s",
                        (target_name, community_id),
                    )
                    await cur.execute(
                        "UPDATE _grid_members AS member "
                        "JOIN _departments AS department "
                        "ON department.id=member.department_id "
                        "SET member.community=%s "
                        "WHERE department.community_id=%s",
                        (target_name, community_id),
                    )
                except Exception as exc:
                    if "Duplicate" in str(exc):
                        raise HTTPException(400, "该社区名称已存在") from exc
                    raise

            selected_officer_ids = data.police_officer_ids
            if selected_officer_ids is None and data.police_officers is not None:
                selected_officer_ids = []
                for officer_name in data.police_officers:
                    await cur.execute(
                        "SELECT id FROM _grid_members "
                        "WHERE name=%s AND position='社区民警'",
                        (officer_name,),
                    )
                    officer = await cur.fetchone()
                    if not officer:
                        raise HTTPException(
                            400,
                            f"社区民警“{officer_name}”尚未登记到人员管理",
                        )
                    selected_officer_ids.append(int(officer[0]))

            if selected_officer_ids is not None:
                selected_officer_ids = list(dict.fromkeys(selected_officer_ids))
                await cur.execute(
                    "SELECT id FROM _departments "
                    "WHERE community_id=%s AND department_type='community'",
                    (community_id,),
                )
                department_row = await cur.fetchone()
                if not department_row:
                    raise HTTPException(500, "社区部门尚未初始化")
                community_department_id = int(department_row[0])

                if selected_officer_ids:
                    placeholders = ", ".join(["%s"] * len(selected_officer_ids))
                    await cur.execute(
                        f"SELECT id FROM _grid_members "
                        f"WHERE id IN ({placeholders}) AND position='社区民警'",
                        selected_officer_ids,
                    )
                    valid_ids = {int(row[0]) for row in await cur.fetchall()}
                    if valid_ids != set(selected_officer_ids):
                        raise HTTPException(400, "只能选择人员管理中的社区民警")

                await cur.execute(
                    """
                    SELECT link.member_id
                    FROM _grid_member_department_links AS link
                    JOIN _grid_members AS member ON member.id=link.member_id
                    WHERE link.department_id=%s
                      AND member.position='社区民警'
                    """,
                    (community_department_id,),
                )
                current_officer_ids = {
                    int(row[0]) for row in await cur.fetchall()
                }
                affected_officer_ids = current_officer_ids | set(selected_officer_ids)
                for officer_id in affected_officer_ids:
                    await cur.execute(
                        """
                        SELECT department.id
                        FROM _grid_member_department_links AS link
                        JOIN _departments AS department
                          ON department.id=link.department_id
                        WHERE link.member_id=%s
                          AND department.department_type='community'
                          AND department.is_active=1
                        ORDER BY link.sort_order, department.name
                        """,
                        (officer_id,),
                    )
                    officer_departments = [
                        int(row[0]) for row in await cur.fetchall()
                    ]
                    if officer_id in selected_officer_ids:
                        if community_department_id not in officer_departments:
                            officer_departments.append(community_department_id)
                    else:
                        officer_departments = [
                            department_id for department_id in officer_departments
                            if department_id != community_department_id
                        ]
                    resolved = await resolve_departments(
                        cur,
                        "社区民警",
                        officer_departments,
                    )
                    await replace_member_departments(cur, officer_id, resolved)
                await sync_community_police_compat(cur)
                if selected_officer_ids:
                    placeholders = ", ".join(["%s"] * len(selected_officer_ids))
                    await cur.execute(
                        f"SELECT name FROM _grid_members "
                        f"WHERE id IN ({placeholders}) ORDER BY name",
                        selected_officer_ids,
                    )
                    returned_officers = [
                        str(row[0]).strip()
                        for row in await cur.fetchall()
                        if str(row[0]).strip()
                    ]
                else:
                    returned_officers = []

            matched_rows = 0
            for alias in aliases:
                await cur.execute(
                    "UPDATE t_visit_details SET 社区=%s WHERE 社区=%s",
                    (target_name, alias),
                )
                matched_rows += cur.rowcount
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise

    return {
        "message": "社区资料已保存",
        "name": target_name,
        "aliases": aliases,
        "police_officers": returned_officers,
        "matched_visit_rows": matched_rows,
        "area_id": data.area_id,
        "qmf_community_code": data.qmf_community_code,
    }


@router.post("/communities/import-from-data")
async def import_communities_from_data(
    user: dict = Depends(require_permission(COMMUNITY_MANAGE)),
    conn=Depends(get_db),
):
    """从原始数据提取社区名，去重后导入 _communities 表"""
    del user
    raw = set()
    async with conn.cursor() as cur:
        for table in TABLES_WITH_INSPECTOR:
            try:
                await cur.execute(f"SELECT DISTINCT `社区` FROM {table} WHERE `社区` IS NOT NULL AND `社区` != ''")
                for r in await cur.fetchall():
                    name = normalize_community(r[0])
                    if name:
                        raw.add(name)
            except Exception:
                continue
        # 已有的
        await cur.execute("SELECT name FROM _communities")
        existing = {
            normalize_community(r[0])
            for r in await cur.fetchall()
        }
        await cur.execute("SELECT alias FROM _community_aliases")
        aliases = {
            normalize_community(r[0])
            for r in await cur.fetchall()
        }
        new = raw - existing - aliases
        for name in new:
            await cur.execute("INSERT IGNORE INTO _communities (name) VALUES (%s)", (name,))
            await cur.execute(
                """
                INSERT IGNORE INTO _departments
                    (name, department_type, community_id)
                SELECT name, 'community', id
                FROM _communities WHERE name=%s
                """,
                (name,),
            )
    return {"message": f"导入完成，新增 {len(new)} 个社区", "new_count": len(new), "new_names": sorted(new)}


async def _attach_account_to_new_member(
    cur,
    data: GridMemberCreate,
    member_id: int,
) -> tuple[int, str]:
    group_id, group_code, _ = await _position_primary_group(cur, data.position)
    role = _legacy_role_for_group(group_code)
    if data.account_mode == "existing":
        await cur.execute(
            "SELECT id, username, role, member_id FROM _users "
            "WHERE id=%s FOR UPDATE",
            (data.existing_user_id,),
        )
        account = await cur.fetchone()
        if not account:
            raise HTTPException(400, "要关联的账号不存在")
        if str(account[2]) == "super_admin":
            raise HTTPException(400, "超级管理员账号不能通过人员管理关联")
        if account[3] is not None:
            raise HTTPException(409, "该账号已经关联其他人员")
        account_id = int(account[0])
        username = str(account[1])
        await cur.execute(
            "DELETE FROM _user_permission_group_links WHERE user_id=%s",
            (account_id,),
        )
        await cur.execute(
            """
            UPDATE _users
            SET member_id=%s, display_name=%s,
                group_assignment_mode='inherited',
                permission_group_id=%s, role=%s
            WHERE id=%s
            """,
            (member_id, data.name.strip(), group_id, role, account_id),
        )
        return account_id, username

    username = str(data.username or "").strip()
    password = str(data.password or "")
    password_hash = bcrypt.hashpw(
        password.encode("utf-8"),
        bcrypt.gensalt(),
    ).decode("utf-8")
    await cur.execute(
        """
        INSERT INTO _users (
            username, display_name, password_hash, role, member_id,
            permission_group_id, group_assignment_mode,
            password_is_temporary
        ) VALUES (%s, %s, %s, %s, %s, %s, 'inherited', 1)
        """,
        (
            username,
            data.name.strip(),
            password_hash,
            role,
            member_id,
            group_id,
        ),
    )
    return int(cur.lastrowid), username


async def _set_account_inherited_for_member(
    cur,
    *,
    account_id: int,
    member_id: int,
    member_name: str,
    position: str,
) -> None:
    group_id, group_code, _ = await _position_primary_group(cur, position)
    await cur.execute(
        "DELETE FROM _user_permission_group_links WHERE user_id=%s",
        (account_id,),
    )
    await invalidate_all_sessions(cur, int(account_id))
    await cur.execute(
        """
        UPDATE _users
        SET member_id=%s, display_name=%s,
            group_assignment_mode='inherited',
            permission_group_id=%s, role=%s
        WHERE id=%s
        """,
        (
            member_id,
            member_name,
            group_id,
            _legacy_role_for_group(group_code),
            account_id,
        ),
    )


async def _reassign_member_account(
    cur,
    *,
    member_id: int,
    target_account_id: int,
) -> dict:
    await cur.execute(
        """
        SELECT id, username, role, member_id
        FROM _users
        WHERE id=%s OR member_id=%s
        ORDER BY id
        FOR UPDATE
        """,
        (target_account_id, member_id),
    )
    account_rows = await cur.fetchall()
    current_account = next(
        (row for row in account_rows if row[3] is not None and int(row[3]) == member_id),
        None,
    )
    target_account = next(
        (row for row in account_rows if int(row[0]) == target_account_id),
        None,
    )
    if current_account is None:
        raise HTTPException(409, "该人员当前没有关联账号，请刷新后重试")
    if target_account is None:
        raise HTTPException(400, "目标账号不存在")
    if str(current_account[2]) == "super_admin":
        raise HTTPException(400, "超级管理员账号不能通过人员管理改绑")
    if str(target_account[2]) == "super_admin":
        raise HTTPException(400, "超级管理员账号不能通过人员管理改绑")
    current_account_id = int(current_account[0])
    if current_account_id == target_account_id:
        return {
            "changed": False,
            "swapped": False,
            "affected_account_ids": [],
        }

    other_member_id = (
        int(target_account[3]) if target_account[3] is not None else None
    )
    member_ids = [member_id]
    if other_member_id is not None:
        member_ids.append(other_member_id)
    placeholders = ", ".join(["%s"] * len(member_ids))
    await cur.execute(
        f"SELECT id, name, position FROM _grid_members "
        f"WHERE id IN ({placeholders}) ORDER BY id FOR UPDATE",
        member_ids,
    )
    members = {
        int(row[0]): {"name": str(row[1]), "position": str(row[2])}
        for row in await cur.fetchall()
    }
    if member_id not in members or (
        other_member_id is not None and other_member_id not in members
    ):
        raise HTTPException(409, "账号关联的人员状态已经变化，请刷新后重试")

    affected_account_ids = [current_account_id, target_account_id]
    await invalidate_all_sessions_for_users(cur, affected_account_ids)
    await cur.execute(
        "UPDATE _users SET member_id=NULL WHERE id IN (%s, %s)",
        affected_account_ids,
    )

    current_member = members[member_id]
    await _set_account_inherited_for_member(
        cur,
        account_id=target_account_id,
        member_id=member_id,
        member_name=current_member["name"],
        position=current_member["position"],
    )
    if other_member_id is None:
        await cur.execute(
            "DELETE FROM _user_permission_group_links WHERE user_id=%s",
            (current_account_id,),
        )
        await cur.execute(
            """
            UPDATE _users
            SET group_assignment_mode='custom', permission_group_id=NULL,
                role='member', active_session_id=NULL,
                active_desktop_session_id=NULL,
                active_mobile_session_id=NULL
            WHERE id=%s AND member_id IS NULL
            """,
            (current_account_id,),
        )
    else:
        other_member = members[other_member_id]
        await _set_account_inherited_for_member(
            cur,
            account_id=current_account_id,
            member_id=other_member_id,
            member_name=other_member["name"],
            position=other_member["position"],
        )
    return {
        "changed": True,
        "swapped": other_member_id is not None,
        "affected_account_ids": affected_account_ids,
    }


@router.post("")
async def create_member(
    data: GridMemberCreate,
    request: Request,
    user: dict = Depends(require_permission(PERSONNEL_MANAGE)),
    conn=Depends(get_db),
):
    """原子创建人员，并关联或创建其登录账号。"""
    if data.id_card_number and str(user.get("role") or "") != "super_admin":
        raise HTTPException(403, "只有超级管理员可以填写身份证号")
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            departments = await _resolved_departments_for_payload(
                cur,
                data.position,
                data.department_ids,
                data.department_id,
            )
            primary = departments[0] if departments else None
            if data.id_card_number:
                await cur.execute(
                    "SELECT id FROM _grid_members WHERE id_card_number=%s FOR UPDATE",
                    (data.id_card_number,),
                )
                if await cur.fetchone():
                    raise HTTPException(409, "该身份证号已被其他人员使用")
            await cur.execute(
                "INSERT INTO _grid_members "
                "(name, community, department_id, position, phone, id_card_number, "
                "notes, status, "
                "leave_start_date, leave_end_date, leave_reason, leave_source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    data.name.strip(),
                    primary.get("community_name") or "" if primary else "",
                    primary["id"] if primary else None,
                    data.position,
                    data.phone,
                    data.id_card_number,
                    data.notes,
                    data.status,
                    data.leave_start_date,
                    data.leave_end_date,
                    data.leave_reason,
                    data.leave_source,
                ),
            )
            member_id = int(cur.lastrowid)
            await replace_member_departments(cur, member_id, departments)
            account_id, account_username = await _attach_account_to_new_member(
                cur,
                data,
                member_id,
            )
            if data.position == "社区民警":
                await sync_community_police_compat(cur)
        await conn.commit()
    except Exception as exc:
        await conn.rollback()
        if "Duplicate" in str(exc):
            if data.id_card_number and "id_card" in str(exc).lower():
                raise HTTPException(409, "该身份证号已被其他人员使用") from exc
            message = "该人员已存在" if "uk_name" in str(exc) else "该用户名已存在"
            raise HTTPException(400, message) from exc
        raise
    await record_admin_audit(
        user,
        "personnel.create_with_account",
        target_type="grid_member",
        target_name=str(member_id),
        detail={
            "position": data.position,
            "department_count": len(departments),
            "account_id": account_id,
            "account_mode": data.account_mode,
            "identity_added": bool(data.id_card_number),
        },
        **request_audit_fields(request),
    )
    return {
        "message": "人员和账号已添加",
        "member_id": member_id,
        "account": {
            "id": account_id,
            "username_masked": mask_identity_number(account_username),
        },
    }


@router.put("/{member_id}")
async def update_member(
    member_id: int,
    data: GridMemberUpdate,
    request: Request,
    user: dict = Depends(require_permission(PERSONNEL_MANAGE)),
    conn=Depends(get_db),
):
    """修改"""
    fields_set = data.model_fields_set
    relationship_fields = {"department_id", "department_ids", "community", "position"}
    identity_changed = "id_card_number" in fields_set
    account_requested = "account_id" in fields_set
    if identity_changed and str(user.get("role") or "") != "super_admin":
        raise HTTPException(403, "只有超级管理员可以修改身份证号")
    if account_requested and data.account_id is None:
        raise HTTPException(400, "请选择要关联的账号")
    account_result = {
        "changed": False,
        "swapped": False,
        "affected_account_ids": [],
    }
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT status, leave_start_date, leave_end_date, "
                "position, department_id FROM _grid_members "
                "WHERE id=%s FOR UPDATE",
                (member_id,),
            )
            existing = await cur.fetchone()
            if not existing:
                raise HTTPException(404, "人员不存在")
            existing_departments = await get_member_departments(cur, [member_id])
            current_ids = [
                item["id"] for item in existing_departments.get(member_id, [])
            ]
            if not current_ids and existing[4] is not None:
                current_ids = [int(existing[4])]

            next_start = (
                data.leave_start_date
                if "leave_start_date" in fields_set else existing[1]
            )
            next_end = (
                data.leave_end_date
                if "leave_end_date" in fields_set else existing[2]
            )
            try:
                validate_leave_period(next_start, next_end)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from exc

            updates = {}
            for field in ["position", "phone", "notes", "leave_reason"]:
                if field in fields_set:
                    updates[field] = getattr(data, field) or ""
            if identity_changed:
                if data.id_card_number:
                    await cur.execute(
                        "SELECT id FROM _grid_members "
                        "WHERE id_card_number=%s AND id<>%s FOR UPDATE",
                        (data.id_card_number, member_id),
                    )
                    if await cur.fetchone():
                        raise HTTPException(409, "该身份证号已被其他人员使用")
                updates["id_card_number"] = data.id_card_number
            for field in ["leave_start_date", "leave_end_date"]:
                if field in fields_set:
                    updates[field] = getattr(data, field)
            if "status" in fields_set and data.status is not None:
                updates["status"] = data.status
            if "leave_source" in fields_set:
                updates["leave_source"] = data.leave_source or "manual"
            if {"leave_start_date", "leave_end_date"} & fields_set:
                updates.setdefault("leave_source", "manual")
                if next_start is None and next_end is None:
                    updates.setdefault("leave_reason", "")
            if (
                not updates
                and not (relationship_fields & fields_set)
                and not account_requested
            ):
                raise HTTPException(400, "没有要更新的字段")

            old_position = str(existing[3])
            next_position = str(updates.get("position") or old_position)
            departments = existing_departments.get(member_id, [])
            if relationship_fields & fields_set:
                if "department_ids" in fields_set:
                    requested_ids = data.department_ids or []
                elif "department_id" in fields_set:
                    requested_ids = (
                        [data.department_id] if data.department_id is not None else []
                    )
                else:
                    requested_ids = current_ids
                departments = await resolve_departments(
                    cur,
                    next_position,
                    requested_ids,
                )

            if updates:
                set_clause = ", ".join(f"{key}=%s" for key in updates)
                await cur.execute(
                    f"UPDATE _grid_members SET {set_clause} WHERE id=%s",
                    [*updates.values(), member_id],
                )
            if relationship_fields & fields_set:
                await replace_member_departments(cur, member_id, departments)
            if account_requested:
                account_result = await _reassign_member_account(
                    cur,
                    member_id=member_id,
                    target_account_id=int(data.account_id),
                )
            elif "position" in fields_set:
                await _refresh_inherited_account_group(cur, member_id)
            if old_position == "社区民警" or next_position == "社区民警":
                await sync_community_police_compat(cur)
        await conn.commit()
    except Exception as exc:
        await conn.rollback()
        if "Duplicate" in str(exc) and identity_changed:
            raise HTTPException(409, "该身份证号已被其他人员使用") from exc
        raise
    await record_admin_audit(
        user,
        "personnel.update",
        target_type="grid_member",
        target_name=str(member_id),
        detail={
            "changed_fields": sorted(
                field for field in fields_set
                if field not in {"id_card_number", "account_id"}
            ),
            "identity_changed": identity_changed,
            "account_changed": account_result["changed"],
            "account_swapped": account_result["swapped"],
            "affected_account_ids": account_result["affected_account_ids"],
        },
        **request_audit_fields(request),
    )
    return {"message": "修改成功"}


@router.put("/{member_id}/leave")
async def update_member_leave(
    member_id: int,
    data: GridMemberLeaveUpdate,
    user: dict = Depends(require_permission(ATTENDANCE_MANAGE)),
    conn=Depends(get_db),
):
    """更新当前状态，同时把过去的请假记录保留在出勤历史中。"""
    async with conn.cursor() as cur:
        business_date = await get_business_date(cur)
    if data.action == "temporary":
        updates = (
            "在岗",
            data.leave_start_date,
            data.leave_end_date,
            data.leave_reason,
            "manual",
        )
    elif data.action == "long_term":
        updates = ("离岗", None, None, data.leave_reason, "manual")
    else:
        updates = ("在岗", None, None, "", "manual")
    historical_only = (
        data.action == "temporary"
        and data.leave_end_date is not None
        and data.leave_end_date < business_date
    )

    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id FROM _grid_members WHERE id=%s FOR UPDATE",
                (member_id,),
            )
            if not await cur.fetchone():
                raise HTTPException(404, "人员不存在")

            if not historical_only:
                await cur.execute(
                    """
                    UPDATE _personnel_attendance_history
                    SET is_active=0
                    WHERE member_id=%s
                      AND is_active=1
                      AND start_date >= %s
                    """,
                    (member_id, business_date),
                )
                await cur.execute(
                    """
                    UPDATE _personnel_attendance_history
                    SET end_date=%s
                    WHERE member_id=%s
                      AND is_active=1
                      AND start_date < %s
                      AND (end_date IS NULL OR end_date >= %s)
                    """,
                    (
                        business_date - timedelta(days=1),
                        member_id,
                        business_date,
                        business_date,
                    ),
                )

            if data.action == "temporary":
                await cur.execute(
                    """
                    INSERT INTO _personnel_attendance_history (
                        member_id, absence_type, start_date, end_date,
                        reason, source, created_by
                    ) VALUES (%s, 'temporary_leave', %s, %s, %s, 'manual', %s)
                    """,
                    (
                        member_id,
                        data.leave_start_date,
                        data.leave_end_date,
                        data.leave_reason,
                        user["id"],
                    ),
                )
            elif data.action == "long_term":
                await cur.execute(
                    """
                    INSERT INTO _personnel_attendance_history (
                        member_id, absence_type, start_date, end_date,
                        reason, source, created_by
                    ) VALUES (%s, 'long_term_leave', %s, NULL, %s, 'manual', %s)
                    """,
                    (
                        member_id,
                        business_date,
                        data.leave_reason,
                        user["id"],
                    ),
                )

            if not historical_only:
                await cur.execute(
                    """
                    UPDATE _grid_members
                    SET status=%s, leave_start_date=%s, leave_end_date=%s,
                        leave_reason=%s, leave_source=%s
                    WHERE id=%s
                    """,
                    (*updates, member_id),
                )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    return {
        "message": (
            "过去的请假记录已补录"
            if historical_only
            else "请假状态已更新"
        )
    }


@router.delete("/{member_id}")
async def delete_member(
    member_id: int,
    request: Request,
    user: dict = Depends(require_super_admin),
    conn=Depends(get_db),
):
    """仅超管可联动删除误建的人员与账号。"""
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT name, position FROM _grid_members "
                "WHERE id=%s FOR UPDATE",
                (member_id,),
            )
            member = await cur.fetchone()
            if not member:
                raise HTTPException(404, "人员不存在")
            await cur.execute(
                "SELECT id FROM _users WHERE member_id=%s FOR UPDATE",
                (member_id,),
            )
            account = await cur.fetchone()
            account_id = int(account[0]) if account else None
            if account_id == int(user["id"]):
                raise HTTPException(400, "不能删除自己关联的人员和账号")
            if account_id is not None:
                await invalidate_all_sessions(cur, account_id)
                await cur.execute("DELETE FROM _notifications WHERE user_id=%s", (account_id,))
                await cur.execute(
                    "DELETE FROM _announcement_reads WHERE user_id=%s",
                    (account_id,),
                )
                await cur.execute(
                    "DELETE FROM _user_permission_group_links WHERE user_id=%s",
                    (account_id,),
                )
                await cur.execute("DELETE FROM _users WHERE id=%s", (account_id,))
            await cur.execute(
                "DELETE FROM _personnel_attendance_history WHERE member_id=%s",
                (member_id,),
            )
            await cur.execute(
                "DELETE FROM _personnel_weekend_duty WHERE member_id=%s",
                (member_id,),
            )
            await cur.execute(
                "DELETE FROM _grid_member_department_links WHERE member_id=%s",
                (member_id,),
            )
            await cur.execute("DELETE FROM _grid_members WHERE id=%s", (member_id,))
            if str(member[1]) == "社区民警":
                await sync_community_police_compat(cur)
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise
    await record_admin_audit(
        user,
        "personnel.delete_with_account",
        target_type="grid_member",
        target_name=str(member_id),
        detail={"account_deleted": account_id is not None},
        **request_audit_fields(request),
    )
    return {"message": "人员和关联账号已删除"}


@router.post("/extract")
async def extract_from_data(
    user: dict = Depends(require_permission(PERSONNEL_MANAGE)),
    conn=Depends(get_db),
):
    """从原始数据预览人员候选；人员必须在页面中连同账号创建。"""
    del user
    name_communities: dict[str, set[str]] = {}
    async with conn.cursor() as cur:
        for table in TABLES_WITH_INSPECTOR:
            try:
                await cur.execute(
                    f"SELECT `核查人`, `社区` FROM {table} WHERE `核查人` IS NOT NULL AND `核查人` != ''"
                )
                rows = await cur.fetchall()
                for row in rows:
                    name = str(row[0]).strip()
                    comm = str(row[1]).strip() if row[1] else ""
                    if name not in name_communities:
                        name_communities[name] = set()
                    if comm:
                        name_communities[name].add(comm)
            except Exception:
                continue

        await cur.execute("SELECT name FROM _grid_members")
        existing = {str(r[0]) for r in await cur.fetchall()}
        new_names = set(name_communities.keys()) - existing

    all_communities = set()
    for comms in name_communities.values():
        all_communities.update(comms)

    return {
        "new_count": len(new_names),
        "new_names": sorted(new_names),
        "communities": sorted(all_communities),
        "preview_only": True,
        "message": "候选人员未写入，请在人员管理中逐一关联账号后添加",
    }


@router.get("/export")
async def export_csv(
    user: dict = Depends(require_permission(PERSONNEL_SENSITIVE_VIEW)),
    conn=Depends(get_db),
):
    """导出 CSV"""
    async with conn.cursor() as cur:
        business_date = await get_business_date(cur)
        del user
        await cur.execute(
            "SELECT member.name, "
            "COALESCE(GROUP_CONCAT(DISTINCT department.name "
            "ORDER BY link.sort_order, department.name SEPARATOR '、'), "
            "member.community), "
            "member.position, member.phone, member.notes, member.status, "
            "leave_start_date, "
            "leave_end_date, leave_reason, leave_source "
            "FROM _grid_members AS member "
            "LEFT JOIN _grid_member_department_links AS link "
            "ON link.member_id=member.id "
            "LEFT JOIN _departments AS department "
            "ON department.id=link.department_id "
            "GROUP BY member.id, member.name, member.community, member.position, "
            "member.phone, member.notes, member.status, member.leave_start_date, "
            "member.leave_end_date, member.leave_reason, member.leave_source "
            "ORDER BY 2, member.name"
        )
        rows = await cur.fetchall()

    output = io.StringIO()
    output.write("\ufeff")  # BOM for Excel
    writer = csv.writer(output)
    writer.writerow([
        "姓名",
        "所属部门",
        "岗位",
        "电话",
        "备注",
        "请假类型",
        "当前情况",
        "请假开始",
        "请假结束",
        "请假原因",
        "请假来源",
    ])
    for r in rows:
        snapshot = get_status_snapshot(r[5], r[6], r[7], business_date)
        leave_type = (
            "长期"
            if r[5] == "离岗"
            else "临时请假"
            if r[6] and r[7]
            else "无"
        )
        writer.writerow([
            r[0],
            r[1],
            r[2],
            r[3],
            r[4],
            leave_type,
            snapshot["effective_status"],
            r[6] or "",
            r[7] or "",
            r[8],
            r[9],
        ])

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=personnel.csv"},
    )
