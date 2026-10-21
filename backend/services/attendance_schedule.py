"""Canonical daily attendance and transactional, versioned changes.

Dates are business dates. Change bodies belong only in protected attendance
storage; ordinary application/audit logs must never contain leave reasons.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from typing import Any

from fastapi import HTTPException
from services.business_time import get_business_date
from services.personnel_positions import (
    get_configured_positions,
    WEEKEND_DUTY_POSITION_CONFIG_KEY,
    personnel_table,
)

MIGRATION_KEY = "attendance_daily_schedule_v1"


def table(name: str) -> str:
    return personnel_table(name)


def dates(start: date, end: date):
    while start <= end:
        yield start
        start += timedelta(days=1)


async def ensure_schema(cur):
    for name, definition in {
        "_personnel_work_schedule": """
            member_id INT NOT NULL, work_date DATE NOT NULL,
            state VARCHAR(10) NOT NULL, source VARCHAR(30) NOT NULL,
            revision BIGINT NOT NULL, updated_by INT NULL,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY(member_id, work_date), INDEX idx_work_date(work_date)
        """,
        "_personnel_attendance_changes": """
            id BIGINT AUTO_INCREMENT PRIMARY KEY, member_id INT NOT NULL,
            revision BIGINT NOT NULL, action VARCHAR(40) NOT NULL,
            actor_id INT NULL, reason VARCHAR(200) NOT NULL,
            before_json JSON NOT NULL, after_json JSON NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uk_member_revision(member_id, revision)
        """,
        "_personnel_attendance_operations": """
            request_id VARCHAR(64) PRIMARY KEY, actor_id INT NOT NULL,
            payload_hash CHAR(64) NOT NULL, response_json JSON NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        """,
    }.items():
        await cur.execute(
            f"CREATE TABLE IF NOT EXISTS {table(name)} ({definition}) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
        )
    columns = {
        "_grid_members": {"attendance_revision": "BIGINT NOT NULL DEFAULT 0"},
        "_personnel_attendance_history": {
            "superseded_by": "BIGINT NULL",
            "correction_action": "VARCHAR(20) NOT NULL DEFAULT ''",
            "correction_reason": "VARCHAR(200) NOT NULL DEFAULT ''",
            "corrected_by": "INT NULL",
            "corrected_at": "DATETIME NULL",
            "workflow_ticket_id": "BIGINT NULL",
        },
    }
    for name, definitions in columns.items():
        for column, definition in definitions.items():
            await cur.execute(f"SHOW COLUMNS FROM {table(name)} LIKE %s", (column,))
            if not await cur.fetchone():
                await cur.execute(
                    f"ALTER TABLE {table(name)} ADD COLUMN {column} {definition}"
                )


async def seed_legacy_once(cur):
    # Startup owns no business transaction. The unique marker row serializes
    # concurrent workers; the seed and marker commit together or neither does.
    await cur.execute("START TRANSACTION")
    try:
        await cur.execute(
            f"INSERT IGNORE INTO {table('_system_config')} (config_key,config_value) "
            "VALUES ('attendance_legacy_seed_v1','pending')"
        )
        await cur.execute(
            f"SELECT config_value FROM {table('_system_config')} "
            "WHERE config_key='attendance_legacy_seed_v1' FOR UPDATE"
        )
        if str((await cur.fetchone())[0]) != "1":
            await cur.execute(f"""
                INSERT INTO {table('_personnel_attendance_history')}
                (member_id,absence_type,start_date,end_date,reason,source)
                SELECT g.id,CASE WHEN g.status='离岗' THEN 'long_term_leave' ELSE 'temporary_leave' END,
                       CASE WHEN g.status='离岗' THEN COALESCE(DATE(g.updated_at),CURRENT_DATE) ELSE g.leave_start_date END,
                       CASE WHEN g.status='离岗' THEN NULL ELSE g.leave_end_date END,
                       COALESCE(g.leave_reason,''),'legacy_current_state'
                FROM {table('_grid_members')} g
                WHERE (g.status='离岗' OR (g.leave_start_date IS NOT NULL AND g.leave_end_date IS NOT NULL))
                  AND NOT EXISTS (
                    SELECT 1 FROM {table('_personnel_attendance_history')} history
                    WHERE history.member_id=g.id
                      AND ((g.status='离岗' AND history.absence_type='long_term_leave' AND history.end_date IS NULL)
                        OR (g.status<>'离岗' AND history.start_date=g.leave_start_date AND history.end_date=g.leave_end_date))
                  )
            """)
            await cur.execute(
                f"UPDATE {table('_system_config')} SET config_value='1' "
                "WHERE config_key='attendance_legacy_seed_v1'"
            )
        await cur.execute("COMMIT")
    except Exception:
        await cur.execute("ROLLBACK")
        raise


async def ready(cur) -> bool:
    await cur.execute(
        f"SELECT config_value FROM {table('_system_config')} WHERE config_key=%s",
        (MIGRATION_KEY,),
    )
    row = await cur.fetchone()
    return bool(row and str(row[0]) == "1")


async def require_ready(cur):
    if not await ready(cur):
        raise HTTPException(
            409,
            "逐日排班尚未迁移。请管理员完成出勤迁移的 measure → migrate --apply → verify 后重试",
        )


async def load_daily(cur, member_ids: list[int], start: date, end: date):
    if not member_ids:
        return {}
    marks = ",".join(["%s"] * len(member_ids))
    await cur.execute(
        f"SELECT member_id,work_date,state FROM {table('_personnel_work_schedule')} "
        f"WHERE member_id IN ({marks}) AND work_date BETWEEN %s AND %s",
        [*member_ids, start, end],
    )
    result = {(int(m), d): str(s) for m, d, s in await cur.fetchall()}
    if not await ready(cur):
        # Compatibility is read-only. Once migrated the legacy table is never read.
        monday = start - timedelta(days=start.weekday())
        await cur.execute(
            f"SELECT member_id,week_start,duty_date FROM {table('_personnel_weekend_duty')} "
            f"WHERE member_id IN ({marks}) AND week_start BETWEEN %s AND %s",
            [*member_ids, monday, end],
        )
        for member_id, week, duty in await cur.fetchall():
            for offset in (5, 6):
                day = week + timedelta(days=offset)
                if start <= day <= end:
                    result.setdefault(
                        (int(member_id), day), "work" if day == duty else "rest"
                    )
    return result


async def load_periods(cur, member_ids: list[int], start: date, end: date):
    if not member_ids:
        return {}
    marks = ",".join(["%s"] * len(member_ids))
    await cur.execute(
        f"SELECT id,member_id,absence_type,start_date,end_date,source,workflow_ticket_id "
        f"FROM {table('_personnel_attendance_history')} WHERE is_active=1 "
        f"AND member_id IN ({marks}) AND start_date<=%s AND (end_date IS NULL OR end_date>=%s) "
        "ORDER BY start_date,id",
        [*member_ids, end, start],
    )
    result = {}
    for row in await cur.fetchall():
        result.setdefault(int(row[1]), []).append(
            dict(
                id=int(row[0]),
                absence_type=row[2],
                start_date=row[3],
                end_date=row[4],
                source=row[5],
                workflow_ticket_id=row[6],
            )
        )
    return result


def resolve_day(member, day, daily, periods, duty_positions):
    overlaps = [
        p
        for p in periods
        if p.get("is_active", True)
        and p["start_date"] <= day
        and (p.get("end_date") is None or day <= p["end_date"])
    ]
    explicit = daily.get((int(member["id"]), day))
    if overlaps:
        leave = next(
            (p for p in overlaps if p.get("absence_type") == "long_term_leave"),
            overlaps[0],
        )
        return dict(
            state=(
                "long_term_leave"
                if leave.get("absence_type") == "long_term_leave"
                else "leave"
            ),
            source=leave.get("source", "manual"),
            leave_id=leave.get("id"),
            conflict=explicit == "work",
            overlapping=len(overlaps) > 1,
        )
    state = explicit or (
        "work"
        if day.weekday() < 5
        else "unassigned" if member["position"] in duty_positions else "rest"
    )
    return dict(
        state=state,
        source="schedule" if explicit else "default",
        leave_id=None,
        conflict=False,
        overlapping=False,
    )


async def lock_members(cur, expected: dict[int, int] | None, member_ids: list[int]):
    ids = sorted(set(member_ids))
    if not ids:
        raise HTTPException(400, "请选择人员")
    marks = ",".join(["%s"] * len(ids))
    await cur.execute(
        f"SELECT id,attendance_revision FROM {table('_grid_members')} "
        f"WHERE id IN ({marks}) ORDER BY id FOR UPDATE",
        ids,
    )
    versions = {int(m): int(v) for m, v in await cur.fetchall()}
    if set(versions) != set(ids):
        raise HTTPException(404, "人员不存在，请刷新人员列表")
    if expected is not None and (
        set(expected) != set(ids) or any(expected[m] != v for m, v in versions.items())
    ):
        raise HTTPException(
            409, "出勤安排已被修改；草稿已保留，请刷新版本、核对差异后重新预览"
        )
    return versions


async def record_change(cur, member_id, revision, action, actor, reason, before, after):
    await cur.execute(
        f"INSERT INTO {table('_personnel_attendance_changes')} "
        "(member_id,revision,action,actor_id,reason,before_json,after_json) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (
            member_id,
            revision + 1,
            action,
            actor,
            reason,
            json.dumps(before, default=str, ensure_ascii=False),
            json.dumps(after, default=str, ensure_ascii=False),
        ),
    )
    await cur.execute(
        f"UPDATE {table('_grid_members')} SET attendance_revision=attendance_revision+1 WHERE id=%s",
        (member_id,),
    )


async def sync_current(cur, member_id):
    today = await get_business_date(cur)
    await cur.execute(
        f"SELECT absence_type,start_date,end_date,reason,source FROM {table('_personnel_attendance_history')} "
        "WHERE member_id=%s AND is_active=1 AND (end_date IS NULL OR end_date>=%s) "
        "ORDER BY (start_date<=%s) DESC,start_date,id LIMIT 1 FOR UPDATE",
        (member_id, today, today),
    )
    row = await cur.fetchone()
    values = ("在岗", None, None, "", "manual")
    if row:
        values = (
            "离岗" if row[0] == "long_term_leave" and row[1] <= today else "在岗",
            row[1],
            row[2],
            row[3] or "",
            row[4],
        )
    await cur.execute(
        f"UPDATE {table('_grid_members')} SET status=%s,leave_start_date=%s,leave_end_date=%s,"
        "leave_reason=%s,leave_source=%s WHERE id=%s",
        (*values, member_id),
    )


async def check_overlap(cur, member_id, start, end, exclude=None):
    await cur.execute(
        f"SELECT id FROM {table('_personnel_attendance_history')} WHERE member_id=%s AND is_active=1 "
        "AND start_date<=%s AND (end_date IS NULL OR end_date>=%s) AND id<>%s LIMIT 1 FOR UPDATE",
        (member_id, end or date.max, start, exclude or 0),
    )
    if await cur.fetchone():
        raise HTTPException(
            409, "与有效请假区间重叠，请先在出勤历史中更正或撤销相关记录"
        )


async def insert_leave(
    cur, member_id, kind, start, end, reason, source, actor, ticket_id=None
):
    if (
        start is None
        or (end is not None and end < start)
        or kind
        not in {"temporary_leave", "long_term_leave", "sick_leave", "annual_leave"}
    ):
        raise HTTPException(400, "请假类型或日期无效")
    if kind != "long_term_leave" and end is None:
        raise HTTPException(400, "临时请假必须有结束日期")
    await check_overlap(cur, member_id, start, end)
    await cur.execute(
        f"INSERT INTO {table('_personnel_attendance_history')} "
        "(member_id,absence_type,start_date,end_date,reason,source,created_by,workflow_ticket_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (member_id, kind, start, end, reason, source, actor, ticket_id),
    )
    return int(cur.lastrowid)


async def replace_leave(
    cur, record_id, member_id, start, end, reason, actor, *, action="correct", kind=None
):
    await cur.execute(
        f"SELECT id,absence_type,start_date,end_date,reason,source,is_active,workflow_ticket_id "
        f"FROM {table('_personnel_attendance_history')} WHERE id=%s AND member_id=%s FOR UPDATE",
        (record_id, member_id),
    )
    row = await cur.fetchone()
    if not row:
        raise HTTPException(404, "请假记录不存在")
    if not row[6]:
        raise HTTPException(409, "该记录已失效，请查看最新版本后重试")
    next_id = None
    if action != "revoke":
        if row[1] == "long_term_leave" and action != "resume" and end != row[3]:
            raise HTTPException(400, "长期离岗结束日期请使用恢复上班操作")
        if end is not None and end < start:
            raise HTTPException(400, "结束日期不能早于开始日期")
        await check_overlap(cur, member_id, start, end, record_id)
        await cur.execute(
            f"INSERT INTO {table('_personnel_attendance_history')} "
            "(member_id,absence_type,start_date,end_date,reason,source,created_by,workflow_ticket_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (member_id, kind or row[1], start, end, row[4], row[5], actor, row[7]),
        )
        next_id = int(cur.lastrowid)
    await cur.execute(
        f"UPDATE {table('_personnel_attendance_history')} SET is_active=0,superseded_by=%s,"
        "correction_action=%s,correction_reason=%s,corrected_by=%s,corrected_at=UTC_TIMESTAMP() WHERE id=%s",
        (next_id, action, reason, actor, record_id),
    )
    return (
        dict(id=record_id, start_date=row[2], end_date=row[3], source=row[5]),
        next_id,
    )


async def mutation(
    conn,
    *,
    actor: int,
    request_id: str,
    payload: dict,
    member_ids: list[int],
    expected: dict[int, int],
    callback,
    preview=False,
):
    fingerprint = hashlib.sha256(
        json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    async with conn.cursor() as cur:
        await cur.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            await require_ready(cur)
            # Serialize request retries before reading their recorded result.
            await cur.execute(
                f"INSERT IGNORE INTO {table('_personnel_attendance_operations')} "
                "(request_id,actor_id,payload_hash,response_json) VALUES (%s,%s,%s,'null')",
                (request_id, actor, fingerprint),
            )
            await cur.execute(
                f"SELECT actor_id,payload_hash,response_json FROM {table('_personnel_attendance_operations')} WHERE request_id=%s FOR UPDATE",
                (request_id,),
            )
            operation = await cur.fetchone()
            if int(operation[0]) != actor or operation[1] != fingerprint:
                raise HTTPException(409, "请求编号已用于其他操作，请重新预览")
            previous = (
                json.loads(operation[2])
                if isinstance(operation[2], str)
                else operation[2]
            )
            if previous is not None and not preview:
                await conn.rollback()
                return previous
            versions = await lock_members(cur, expected, member_ids)
            result = await callback(cur, versions, preview)
            if not preview:
                await cur.execute(
                    f"UPDATE {table('_personnel_attendance_operations')} SET response_json=%s WHERE request_id=%s",
                    (json.dumps(result, default=str, ensure_ascii=False), request_id),
                )
        if preview:
            await conn.rollback()
        else:
            await conn.commit()
        return result
    except Exception:
        await conn.rollback()
        raise


def build_schedule_changes(
    members, start, end, state, original_start=None, original_end=None
):
    if start > end or (end - start).days > 366:
        raise HTTPException(400, "安排日期无效或超过 366 天")
    result = {}
    if original_start is not None:
        if (
            original_end is None
            or original_start > original_end
            or (original_end - original_start).days > 366
            or state != "work"
        ):
            raise HTTPException(400, "只有上班横条可移动，原日期范围必须完整")
        for m in members:
            for day in dates(original_start, original_end):
                if not start <= day <= end:
                    result[m, day] = "rest"
    for m in members:
        for day in dates(start, end):
            if original_start is None or not original_start <= day <= original_end:
                result[m, day] = state
    if len(result) > 10000:
        raise HTTPException(400, "一次最多修改 10000 个人日，请缩小范围")
    return result


async def schedule_mutation(conn, data: dict, actor: int, preview=False):
    members = data["member_ids"]
    changes = build_schedule_changes(
        members,
        data["start_date"],
        data["end_date"],
        data["state"],
        data.get("original_start"),
        data.get("original_end"),
    )

    async def apply(cur, versions, dry):
        all_dates = [d for _, d in changes]
        if not all_dates:
            return dict(
                changes=[],
                versions=versions,
                preview=dry,
                message="日期未变化，不新增安排记录",
            )
        daily = await load_daily(cur, members, min(all_dates), max(all_dates))
        periods = await load_periods(cur, members, min(all_dates), max(all_dates))
        positions = set(
            await get_configured_positions(cur, WEEKEND_DUTY_POSITION_CONFIG_KEY)
        )
        await cur.execute(
            f"SELECT id,position FROM {table('_grid_members')} WHERE id IN ({','.join(['%s']*len(members))})",
            members,
        )
        member_map = {
            int(m): dict(id=int(m), position=p) for m, p in await cur.fetchall()
        }
        diffs = []
        for (m, day), state in sorted(changes.items()):
            current = resolve_day(
                member_map[m], day, daily, periods.get(m, []), positions
            )
            if state == "work" and current["state"] in {"leave", "long_term_leave"}:
                raise HTTPException(
                    409, f"人员 #{m} 在 {day} 有有效请假，请在出勤历史中先处理该请假"
                )
            if (
                data.get("original_start")
                and data["original_start"] <= day <= data["original_end"]
                and current["state"] != "work"
            ):
                raise HTTPException(
                    409, "原上班区间已变化或包含非上班日期，请刷新并重新选择横条"
                )
            if daily.get((m, day)) != state:
                diffs.append(
                    dict(
                        member_id=m,
                        date=day.isoformat(),
                        before=current["state"],
                        previous_explicit_state=daily.get((m, day)),
                        after=state,
                    )
                )
        if not dry:
            for m in sorted(members):
                own = [d for d in diffs if d["member_id"] == m]
                if not own:
                    continue
                for d in own:
                    await cur.execute(
                        f"INSERT INTO {table('_personnel_work_schedule')} "
                        "(member_id,work_date,state,source,revision,updated_by) VALUES (%s,%s,%s,'manual',%s,%s) "
                        "ON DUPLICATE KEY UPDATE state=VALUES(state),source=VALUES(source),revision=VALUES(revision),updated_by=VALUES(updated_by),updated_at=UTC_TIMESTAMP()",
                        (m, d["date"], d["after"], versions[m] + 1, actor),
                    )
                await record_change(
                    cur,
                    m,
                    versions[m],
                    "schedule",
                    actor,
                    data["reason"],
                    [{k: v for k, v in d.items() if k != "after"} for d in own],
                    own,
                )
        return dict(
            changes=diffs,
            versions={
                str(m): v + (not dry and any(d["member_id"] == m for d in diffs))
                for m, v in versions.items()
            },
            message="确认后将影响这些日期的动态出勤统计；已保存工作日志需显式刷新",
            preview=dry,
        )

    return await mutation(
        conn,
        actor=actor,
        request_id=data["request_id"],
        payload=data,
        member_ids=members,
        expected=data["expected_versions"],
        callback=apply,
        preview=preview,
    )


async def leave_mutation(conn, data: dict, actor: int, preview=False):
    m = data["member_id"]

    async def apply(cur, versions, dry):
        action = data["action"]
        before = None
        record_id = data.get("record_id")
        if record_id:
            await cur.execute(
                f"SELECT start_date,end_date,absence_type,is_active FROM {table('_personnel_attendance_history')} WHERE id=%s AND member_id=%s",
                (record_id, m),
            )
            row = await cur.fetchone()
            if not row or not row[3]:
                raise HTTPException(409, "请假记录不存在或已失效，请刷新历史")
            before = dict(
                id=record_id, start_date=row[0], end_date=row[1], absence_type=row[2]
            )
        start, end = data.get("start_date"), data.get("end_date")
        if action == "resume":
            if (
                not before
                or before["absence_type"] != "long_term_leave"
                or start is None
                or start <= before["start_date"]
            ):
                raise HTTPException(
                    400, "请选择长期离岗开始日期之后的恢复上班日期；整段错误请使用撤销"
                )
            end, start = start - timedelta(days=1), before["start_date"]
        kind = (
            before["absence_type"]
            if before
            else data.get("absence_type", "temporary_leave")
        )
        if action != "revoke":
            if (
                start is None
                or (end is not None and end < start)
                or (kind != "long_term_leave" and end is None)
            ):
                raise HTTPException(400, "请假日期无效")
            if (
                kind == "long_term_leave"
                and action != "resume"
                and end is not None
                and (not before or end != before["end_date"])
            ):
                raise HTTPException(400, "长期离岗请使用恢复上班操作设置结束日期")
            await check_overlap(cur, m, start, end, record_id)
        if action in {"correct", "revoke", "resume"} and before is None:
            raise HTTPException(400, "请选择需要更正的历史记录")
        after = dict(start_date=start, end_date=end, action=action, absence_type=kind)
        next_id = None
        if not dry:
            if record_id:
                _, next_id = await replace_leave(
                    cur, record_id, m, start, end, data["reason"], actor, action=action
                )
            else:
                next_id = await insert_leave(
                    cur,
                    m,
                    kind,
                    start,
                    end,
                    data.get("leave_reason", ""),
                    "manual",
                    actor,
                )
            await record_change(
                cur,
                m,
                versions[m],
                "leave_" + action,
                actor,
                data["reason"],
                before,
                after,
            )
            await sync_current(cur, m)
        affected_start = (
            min(x for x in [start, (before or {}).get("start_date")] if x is not None)
            if action != "revoke"
            else before["start_date"]
        )
        affected_ends = [end, (before or {}).get("end_date")]
        return dict(
            record_id=next_id,
            original_record_id=record_id,
            before=before,
            after=None if action == "revoke" else after,
            start_date=affected_start,
            end_date=(
                None
                if (before and before["end_date"] is None)
                or (action != "revoke" and end is None)
                else max(x for x in affected_ends if x is not None)
            ),
            version=versions[m] + (not dry),
            preview=dry,
            message="动态出勤统计将重新计算；原审批结果及已保存工作日志保留",
        )

    return await mutation(
        conn,
        actor=actor,
        request_id=data["request_id"],
        payload=data,
        member_ids=[m],
        expected={m: data["expected_version"]},
        callback=apply,
        preview=preview,
    )
