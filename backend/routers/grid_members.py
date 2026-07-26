"""网格员管理 API"""

import csv
import io
from fastapi import APIRouter, Depends, Query, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional
from database import get_db

router = APIRouter(prefix="/api/grid-members", tags=["网格员管理"])

# 有"核查人"列的业务表
TABLES_WITH_INSPECTOR = [
    "t_fullchain", "t_rental_check", "t_delivery_industry",
    "t_suspect_unrevoked", "t_suspect_return", "t_group_rental",
]


class GridMemberCreate(BaseModel):
    name: str
    community: str = ""
    phone: str = ""
    notes: str = ""
    status: str = "在岗"


class GridMemberUpdate(BaseModel):
    community: Optional[str] = None
    phone: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


@router.get("")
async def list_members(
    keyword: Optional[str] = Query(None),
    community: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    conn=Depends(get_db),
):
    """列表查询"""
    where_parts = []
    params = []
    if keyword:
        where_parts.append("(name LIKE %s OR phone LIKE %s OR notes LIKE %s)")
        params.extend([f"%{keyword}%"] * 3)
    if community:
        where_parts.append("community = %s")
        params.append(community)
    where = f" WHERE {' AND '.join(where_parts)}" if where_parts else ""

    async with conn.cursor() as cur:
        await cur.execute(f"SELECT COUNT(*) FROM _grid_members{where}", params)
        total = (await cur.fetchone())[0]
        offset = (page - 1) * page_size
        await cur.execute(
            f"SELECT id, name, community, phone, notes, status FROM _grid_members{where} ORDER BY community, name LIMIT %s OFFSET %s",
            params + [page_size, offset],
        )
        rows = await cur.fetchall()

    return {
        "data": [{"id": r[0], "name": r[1], "community": r[2], "phone": r[3], "notes": r[4], "status": r[5]} for r in rows],
        "total": total, "page": page, "page_size": page_size,
    }


@router.get("/communities")
async def list_communities(conn=Depends(get_db)):
    """获取社区列表（网格员人数由 _grid_members 表实时统计）"""
    async with conn.cursor() as cur:
        await cur.execute("""
            SELECT c.id, c.name, COUNT(g.id) as grid_count
            FROM _communities c
            LEFT JOIN _grid_members g ON g.community = c.name AND g.status = '在岗'
            GROUP BY c.id, c.name
            ORDER BY c.name
        """)
        rows = await cur.fetchall()
    return {"data": [{"id": r[0], "name": r[1], "grid_count": r[2]} for r in rows]}


@router.post("/communities")
async def add_community(name: str = Query(...), conn=Depends(get_db)):
    """添加社区"""
    name = name.strip()
    if not name:
        raise HTTPException(400, "社区名不能为空")
    async with conn.cursor() as cur:
        try:
            await cur.execute("INSERT INTO _communities (name) VALUES (%s)", (name,))
        except Exception as e:
            if "Duplicate" in str(e):
                raise HTTPException(400, "该社区已存在")
            raise
    return {"message": "添加成功"}


@router.delete("/communities/{community_id}")
async def delete_community(community_id: int, conn=Depends(get_db)):
    """删除社区"""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM _communities WHERE id=%s", (community_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "社区不存在")
    return {"message": "删除成功"}


@router.post("/communities/import-from-data")
async def import_communities_from_data(conn=Depends(get_db)):
    """从原始数据提取社区名，去重后导入 _communities 表"""
    raw = set()
    async with conn.cursor() as cur:
        for table in TABLES_WITH_INSPECTOR:
            try:
                await cur.execute(f"SELECT DISTINCT `社区` FROM {table} WHERE `社区` IS NOT NULL AND `社区` != ''")
                for r in await cur.fetchall():
                    raw.add(str(r[0]).strip())
            except Exception:
                continue
        # 已有的
        await cur.execute("SELECT name FROM _communities")
        existing = {str(r[0]) for r in await cur.fetchall()}
        new = raw - existing
        for name in new:
            await cur.execute("INSERT IGNORE INTO _communities (name) VALUES (%s)", (name,))
    return {"message": f"导入完成，新增 {len(new)} 个社区", "new_count": len(new), "new_names": sorted(new)}


@router.post("")
async def create_member(data: GridMemberCreate, conn=Depends(get_db)):
    """手动添加"""
    async with conn.cursor() as cur:
        try:
            await cur.execute(
                "INSERT INTO _grid_members (name, community, phone, notes, status) VALUES (%s, %s, %s, %s, %s)",
                (data.name, data.community, data.phone, data.notes, data.status),
            )
        except Exception as e:
            if "Duplicate" in str(e):
                raise HTTPException(400, "该网格员已存在")
            raise
    return {"message": "添加成功"}


@router.put("/{member_id}")
async def update_member(member_id: int, data: GridMemberUpdate, conn=Depends(get_db)):
    """修改"""
    updates = {}
    if data.community is not None: updates["community"] = data.community
    if data.phone is not None: updates["phone"] = data.phone
    if data.notes is not None: updates["notes"] = data.notes
    if data.status is not None: updates["status"] = data.status
    if not updates:
        raise HTTPException(400, "没有要更新的字段")
    set_clause = ", ".join(f"{k}=%s" for k in updates)
    async with conn.cursor() as cur:
        await cur.execute(f"UPDATE _grid_members SET {set_clause} WHERE id=%s", list(updates.values()) + [member_id])
        if cur.rowcount == 0:
            raise HTTPException(404, "网格员不存在")
    return {"message": "修改成功"}


@router.delete("/{member_id}")
async def delete_member(member_id: int, conn=Depends(get_db)):
    """删除"""
    async with conn.cursor() as cur:
        await cur.execute("DELETE FROM _grid_members WHERE id=%s", (member_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "网格员不存在")
    return {"message": "删除成功"}


@router.post("/extract")
async def extract_from_data(conn=Depends(get_db)):
    """从原始数据提取网格员，同时提取社区候选列表"""
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
        "message": f"提取完成，新增 {len(new_names)} 名网格员",
        "new_count": len(new_names),
        "new_names": sorted(new_names),
        "communities": sorted(all_communities),
    }


@router.get("/export")
async def export_csv(conn=Depends(get_db)):
    """导出 CSV"""
    async with conn.cursor() as cur:
        await cur.execute("SELECT name, community, phone, notes, status FROM _grid_members ORDER BY community, name")
        rows = await cur.fetchall()

    output = io.StringIO()
    output.write("\ufeff")  # BOM for Excel
    writer = csv.writer(output)
    writer.writerow(["姓名", "所属社区", "电话", "备注", "状态"])
    for r in rows:
        writer.writerow(r)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=grid_members.csv"},
    )
