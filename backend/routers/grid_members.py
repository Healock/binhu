"""人员管理 API"""

import csv
import io
import json
from datetime import date, timedelta
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal, Optional
from database import get_db
from deps import require_permission
from services.grid_member_status import (
    get_business_date,
    get_status_snapshot,
    validate_leave_period,
)
from services.personnel_positions import POSITION_CATEGORIES, normalize_position
from services.privacy import mask_identity_number
from services.visit_import import normalize_community
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
    position: str = "组员"
    phone: str = ""
    notes: str = ""
    status: Literal["在岗", "离岗"] = "在岗"
    leave_start_date: Optional[date] = None
    leave_end_date: Optional[date] = None
    leave_reason: str = Field(default="", max_length=200)
    leave_source: str = Field(default="manual", max_length=30)

    @field_validator("position")
    @classmethod
    def validate_position(cls, value: str) -> str:
        return normalize_position(value)

    @model_validator(mode="after")
    def validate_leave_dates(self):
        validate_leave_period(self.leave_start_date, self.leave_end_date)
        return self


class GridMemberUpdate(BaseModel):
    community: Optional[str] = None
    department_id: Optional[int] = None
    position: Optional[str] = None
    phone: Optional[str] = None
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

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = normalize_community(value)
        if not normalized:
            raise ValueError("社区名不能为空")
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


async def _refresh_inherited_account_group(cur, member_id: int) -> None:
    await cur.execute(
        """
        UPDATE _users AS user
        JOIN _grid_members AS member ON member.id=user.member_id
        JOIN _position_permission_groups AS mapping
          ON mapping.position=member.position
        SET user.permission_group_id=mapping.permission_group_id,
            user.role=CASE
                WHEN user.role='super_admin' THEN user.role
                WHEN (SELECT code FROM _permission_groups
                      WHERE id=mapping.permission_group_id)='admin' THEN 'admin'
                ELSE 'member'
            END
        WHERE user.member_id=%s
          AND user.group_assignment_mode='inherited'
        """,
        (member_id,),
    )


def _member_to_dict(row, business_date: date, *, sensitive: bool) -> dict:
    snapshot = get_status_snapshot(row[6], row[7], row[8], business_date)
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
        "department": (
            {
                "id": row[12],
                "name": row[13],
                "type": row[14],
                "community_name": row[15],
            }
            if row[12] is not None
            else None
        ),
        "department_id": row[12],
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
            "has_id_card": bool(row[11]),
            "id_card_masked": mask_identity_number(row[11]),
        })
    else:
        for field in (
            "phone", "notes", "leave_start_date", "leave_end_date",
            "leave_reason", "leave_source", "has_id_card", "id_card_masked",
        ):
            result.pop(field, None)
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
    if keyword:
        if can_view_sensitive:
            where_parts.append(
                "(member.name LIKE %s OR member.phone LIKE %s OR "
                "member.notes LIKE %s OR member.position LIKE %s)"
            )
            params.extend([f"%{keyword}%"] * 4)
        else:
            where_parts.append(
                "(member.name LIKE %s OR member.position LIKE %s OR "
                "department.name LIKE %s)"
            )
            params.extend([f"%{keyword}%"] * 3)
    if community:
        where_parts.append("COALESCE(community.name, member.community) = %s")
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

    return {
        "data": [
            _member_to_dict(
                row,
                business_date,
                sensitive=can_view_sensitive,
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
                   department.department_type, community.name
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
                   COALESCE(g.grid_count, 0) AS grid_count
            FROM _communities c
            LEFT JOIN (
                SELECT department.community_id, COUNT(*) AS grid_count
                FROM _grid_members AS member
                JOIN _departments AS department
                  ON department.id=member.department_id
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
    aliases_by_community: dict[int, list[str]] = {}
    for community_id, alias in alias_rows:
        aliases_by_community.setdefault(community_id, []).append(alias)
    return {
        "data": [
            {
                "id": row[0],
                "name": row[1],
                "police_officers": _parse_police_officers(row[2]),
                "grid_count": row[3],
                "aliases": aliases_by_community.get(row[0], []),
            }
            for row in rows
        ]
    }


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
            FROM _grid_members AS member
            JOIN _departments AS department
              ON department.id=member.department_id
            WHERE department.community_id=%s
            """,
            (community_id,),
        )
        if int((await cur.fetchone())[0] or 0):
            raise HTTPException(409, "该社区部门仍有人员，不能删除")
        await cur.execute(
            "DELETE FROM _departments WHERE community_id=%s",
            (community_id,),
        )
        await cur.execute("DELETE FROM _communities WHERE id=%s", (community_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "社区不存在")
    return {"message": "删除成功"}


@router.put("/communities/{community_id}/aliases")
async def update_community_aliases(
    community_id: int,
    data: CommunityAliasesUpdate,
    user: dict = Depends(require_permission(COMMUNITY_MANAGE)),
    conn=Depends(get_db),
):
    """设置社区别名和民警，并把已导入走访数据归到正式名称。"""
    del user
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

            if data.police_officers is not None:
                await cur.execute(
                    "UPDATE _communities SET police_officers=%s WHERE id=%s",
                    (
                        json.dumps(
                            data.police_officers,
                            ensure_ascii=False,
                        ),
                        community_id,
                    ),
                )

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
        "police_officers": data.police_officers,
        "matched_visit_rows": matched_rows,
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


@router.post("")
async def create_member(
    data: GridMemberCreate,
    user: dict = Depends(require_permission(PERSONNEL_MANAGE)),
    conn=Depends(get_db),
):
    """手动添加"""
    del user
    async with conn.cursor() as cur:
        try:
            department_id, community = await _resolve_department(
                cur,
                data.position,
                data.department_id,
            )
            await cur.execute(
                "INSERT INTO _grid_members "
                "(name, community, department_id, position, phone, notes, status, "
                "leave_start_date, "
                "leave_end_date, leave_reason, leave_source) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    data.name,
                    community,
                    department_id,
                    data.position,
                    data.phone,
                    data.notes,
                    data.status,
                    data.leave_start_date,
                    data.leave_end_date,
                    data.leave_reason,
                    data.leave_source,
                ),
            )
        except Exception as e:
            if "Duplicate" in str(e):
                raise HTTPException(400, "该人员已存在")
            raise
    return {"message": "添加成功"}


@router.put("/{member_id}")
async def update_member(
    member_id: int,
    data: GridMemberUpdate,
    user: dict = Depends(require_permission(PERSONNEL_MANAGE)),
    conn=Depends(get_db),
):
    """修改"""
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT status, leave_start_date, leave_end_date, "
            "position, department_id "
            "FROM _grid_members WHERE id=%s",
            (member_id,),
        )
        existing = await cur.fetchone()
        if not existing:
            raise HTTPException(404, "人员不存在")

    fields_set = data.model_fields_set
    del user
    next_start = (
        data.leave_start_date
        if "leave_start_date" in fields_set
        else existing[1]
    )
    next_end = (
        data.leave_end_date
        if "leave_end_date" in fields_set
        else existing[2]
    )
    try:
        validate_leave_period(next_start, next_end)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    updates = {}
    for field in ["position", "phone", "notes", "leave_reason"]:
        if field in fields_set:
            updates[field] = getattr(data, field) or ""
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
    if not updates:
        if not ({"department_id", "community"} & fields_set):
            raise HTTPException(400, "没有要更新的字段")
    if {"department_id", "community", "position"} & fields_set:
        next_position = str(updates.get("position") or existing[3])
        requested_department_id = (
            data.department_id
            if "department_id" in fields_set
            else existing[4]
        )
        async with conn.cursor() as cur:
            department_id, community = await _resolve_department(
                cur,
                next_position,
                requested_department_id,
            )
        updates["department_id"] = department_id
        updates["community"] = community
    set_clause = ", ".join(f"{k}=%s" for k in updates)
    async with conn.cursor() as cur:
        await cur.execute(f"UPDATE _grid_members SET {set_clause} WHERE id=%s", list(updates.values()) + [member_id])
        await _refresh_inherited_account_group(cur, member_id)
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
    user: dict = Depends(require_permission(PERSONNEL_MANAGE)),
    conn=Depends(get_db),
):
    """删除"""
    del user
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM _users WHERE member_id=%s",
            (member_id,),
        )
        if int((await cur.fetchone())[0] or 0):
            raise HTTPException(409, "该人员已关联账号，请先解除关联")
        await cur.execute("DELETE FROM _grid_members WHERE id=%s", (member_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "人员不存在")
    return {"message": "删除成功"}


@router.post("/extract")
async def extract_from_data(
    user: dict = Depends(require_permission(PERSONNEL_MANAGE)),
    conn=Depends(get_db),
):
    """从原始数据提取人员，同时提取社区候选列表"""
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

        for name in new_names:
            await cur.execute("INSERT IGNORE INTO _grid_members (name) VALUES (%s)", (name,))

    all_communities = set()
    for comms in name_communities.values():
        all_communities.update(comms)

    return {
        "message": f"提取完成，新增 {len(new_names)} 名人员",
        "new_count": len(new_names),
        "new_names": sorted(new_names),
        "communities": sorted(all_communities),
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
            "SELECT member.name, COALESCE(department.name, member.community), "
            "member.position, member.phone, member.notes, member.status, "
            "leave_start_date, "
            "leave_end_date, leave_reason, leave_source "
            "FROM _grid_members AS member "
            "LEFT JOIN _departments AS department "
            "ON department.id=member.department_id "
            "ORDER BY department.name, member.name"
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
