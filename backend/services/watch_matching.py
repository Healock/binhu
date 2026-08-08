"""人员标记的身份证精确命中和任务快照回填。"""

from __future__ import annotations

from datetime import datetime
import re

from config import settings
from services.parsers import PARSER_REGISTRY, get_parser
from services.registry_security import hmac_digest, normalize_identity
from services.task_workflow import TASK_WORKFLOWS


SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]+$")


def _identity_fields(parser_type: str, columns: list[str]) -> list[str]:
    workflow = TASK_WORKFLOWS.get(parser_type)
    if workflow:
        return [field for field in workflow.identity_fields if field in columns]
    return [field for field in columns if "身份证" in field]


def _dispatch_fields(parser_type: str, columns: list[str]) -> list[str]:
    workflow = TASK_WORKFLOWS.get(parser_type)
    candidates = list(workflow.date_fields) if workflow else []
    candidates.extend(["下发日期", "下发时间", "创建时间", "日期"])
    return list(dict.fromkeys(field for field in candidates if field in columns))


def parse_dispatch_time(values: dict, fields: list[str], fallback: datetime | None = None) -> datetime:
    for field in fields:
        text = str(values.get(field) or "").strip()
        if not text:
            continue
        normalized = text.replace("年", "-").replace("月", "-").replace("日", " ").replace("/", "-")
        normalized = re.sub(r"\s+", " ", normalized).strip()
        for candidate in (normalized, normalized[:19], normalized[:10]):
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                continue
        match = re.search(r"(20\d{2})[.\-](\d{1,2})[.\-](\d{1,2})", normalized)
        if match:
            try:
                return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                pass
    return fallback or datetime.utcnow()


def projection_identity(parser_type: str, values: dict, columns: list[str]) -> str | None:
    for field in _identity_fields(parser_type, columns):
        identity = normalize_identity(str(values.get(field) or ""))
        if identity:
            return hmac_digest(identity, kind="identity")[0]
    return None


async def sync_current_task_snapshots(cur, parser_type: str) -> None:
    """用当前投影幂等补齐当时有效的人员标记。"""
    if not settings.REGISTRY_FEATURE_ENABLED:
        return
    registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
    await cur.execute(
        f"""
        INSERT IGNORE INTO `{registry}`.online_task_watch_snapshots (
            parser_type, row_key, identity_hmac, first_dispatch_at,
            assignment_id, assignment_version, snapshot_status,
            snapshot_reason, captured_at
        )
        SELECT projection.parser_type, projection.row_key,
               projection.identity_hmac,
               COALESCE(projection.first_dispatch_at, projection.updated_at),
               assignment.id,
               COALESCE(version_info.version_no, 1),
               'active', 'initial_match', UTC_TIMESTAMP()
        FROM _online_source_projection projection
        JOIN `{registry}`.watch_people person
          ON person.identity_hmac=projection.identity_hmac
         AND person.status='active'
        JOIN `{registry}`.watch_assignments assignment
          ON assignment.person_id=person.id
         AND assignment.valid_from<=COALESCE(
             projection.first_dispatch_at, projection.updated_at
         )
         AND (assignment.valid_to IS NULL OR assignment.valid_to>=COALESCE(
             projection.first_dispatch_at, projection.updated_at
         ))
         AND (assignment.released_at IS NULL OR assignment.released_at>COALESCE(
             projection.first_dispatch_at, projection.updated_at
         ))
        LEFT JOIN (
            SELECT assignment_id, MAX(version_no) AS version_no
            FROM `{registry}`.watch_assignment_versions
            GROUP BY assignment_id
        ) version_info ON version_info.assignment_id=assignment.id
        WHERE projection.parser_type=%s
          AND projection.identity_hmac IS NOT NULL
        """,
        (parser_type,),
    )


async def _snapshot_assignment_row(
    cur,
    *,
    parser_type: str,
    row_key: str,
    identity_hmac: str,
    dispatch_at: datetime,
    assignment_id: int,
    assignment_version: int,
    reason: str,
) -> None:
    await cur.execute(
        "INSERT IGNORE INTO online_task_watch_snapshots "
        "(parser_type, row_key, identity_hmac, first_dispatch_at, assignment_id, assignment_version, "
        "snapshot_status, snapshot_reason) VALUES (%s,%s,%s,%s,%s,%s,'active',%s)",
        (parser_type, row_key, identity_hmac, dispatch_at, assignment_id, assignment_version, reason),
    )


async def backfill_assignment_snapshots(conn, assignment_id: int) -> int:
    """补录过去生效标记时回填当前、归档和近 90 日快照。"""
    inserted = 0
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT assignment.id, person.identity_hmac, assignment.valid_from, assignment.valid_to, "
            "assignment.released_at, COALESCE(MAX(version.version_no),1) "
            "FROM watch_assignments assignment JOIN watch_people person ON person.id=assignment.person_id "
            "LEFT JOIN watch_assignment_versions version ON version.assignment_id=assignment.id "
            "WHERE assignment.id=%s GROUP BY assignment.id, person.identity_hmac, assignment.valid_from, "
            "assignment.valid_to, assignment.released_at",
            (assignment_id,),
        )
        assignment = await cur.fetchone()
        if not assignment or not assignment[1]:
            return 0
        identity_hmac = str(assignment[1])
        valid_from, valid_to, released_at = assignment[2], assignment[3], assignment[4]
        version_no = int(assignment[5])

        await cur.execute(
            f"SELECT parser_type, row_key, values_json, first_dispatch_at, updated_at "
            f"FROM `{settings.MYSQL_ONLINE_DATA_DB}`._online_source_projection "
            "WHERE identity_hmac=%s",
            (identity_hmac,),
        )
        current_rows = await cur.fetchall()
        for parser_type, row_key, raw_values, first_dispatch_at, updated_at in current_rows:
            dispatch_at = first_dispatch_at or updated_at or datetime.utcnow()
            if dispatch_at < valid_from or (valid_to and dispatch_at > valid_to) or (released_at and dispatch_at >= released_at):
                continue
            await _snapshot_assignment_row(
                cur, parser_type=str(parser_type), row_key=str(row_key), identity_hmac=identity_hmac,
                dispatch_at=dispatch_at, assignment_id=assignment_id,
                assignment_version=version_no, reason="historical_backfill",
            )
            inserted += int(cur.rowcount or 0)

        # 归档库同样是历史任务来源。按解析器白名单逐表检查真实列，
        # 不拼接任何外部输入，也不依赖姓名或手机号命中。
        for parser_type, parser_class in PARSER_REGISTRY.items():
            if parser_type == "default":
                continue
            table_name = f"{parser_class.table_name}_archive"
            if not SAFE_IDENTIFIER.fullmatch(table_name):
                continue
            await cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s",
                (settings.MYSQL_ARCHIVE_DB, table_name),
            )
            archive_columns = [str(row[0]) for row in await cur.fetchall()]
            identity_fields = _identity_fields(parser_type, archive_columns)
            if not identity_fields or "_row_key" not in archive_columns:
                continue
            dispatch_fields = _dispatch_fields(parser_type, archive_columns)
            fallback_field = next(
                (field for field in ("_first_seen_at", "_archived_at", "updated_at") if field in archive_columns),
                None,
            )
            selected_fields = [*identity_fields, *dispatch_fields]
            if fallback_field:
                selected_fields.append(fallback_field)
            selected_fields = list(dict.fromkeys(selected_fields))
            quoted = ", ".join(f"`{field}`" for field in selected_fields)
            await cur.execute(
                f"SELECT `_row_key`, {quoted} FROM "
                f"`{settings.MYSQL_ARCHIVE_DB}`.`{table_name}`"
            )
            for item in await cur.fetchall():
                values = dict(zip(selected_fields, item[1:]))
                digest = None
                for field in identity_fields:
                    identity = normalize_identity(str(values.get(field) or ""))
                    if identity:
                        digest = hmac_digest(identity, kind="identity")[0]
                        break
                if digest != identity_hmac:
                    continue
                fallback = values.get(fallback_field) if fallback_field else None
                dispatch_at = parse_dispatch_time(
                    values,
                    dispatch_fields,
                    fallback if isinstance(fallback, datetime) else valid_from,
                )
                if dispatch_at < valid_from or (valid_to and dispatch_at > valid_to) or (
                    released_at and dispatch_at >= released_at
                ):
                    continue
                await _snapshot_assignment_row(
                    cur,
                    parser_type=parser_type,
                    row_key=str(item[0]),
                    identity_hmac=identity_hmac,
                    dispatch_at=dispatch_at,
                    assignment_id=assignment_id,
                    assignment_version=version_no,
                    reason="historical_backfill",
                )
                inserted += int(cur.rowcount or 0)

        await cur.execute(
            f"SELECT table_name, parser_type, report_date FROM `{settings.MYSQL_DAILY_REPORT_DB}`._daily_report_meta "
            "WHERE report_date>=DATE_SUB(CURDATE(), INTERVAL 90 DAY) ORDER BY report_date",
        )
        snapshot_tables = await cur.fetchall()
        for table_name, parser_type, report_date in snapshot_tables:
            table_name = str(table_name)
            parser_type = str(parser_type)
            if not SAFE_IDENTIFIER.fullmatch(table_name):
                continue
            parser = get_parser(parser_type)
            await cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
                (settings.MYSQL_DAILY_REPORT_DB, table_name),
            )
            columns = [str(row[0]) for row in await cur.fetchall()]
            identity_fields = _identity_fields(parser_type, columns)
            if not identity_fields or "_row_key" not in columns:
                continue
            quoted = ", ".join(f"`{field}`" for field in identity_fields)
            await cur.execute(
                f"SELECT `_row_key`, {quoted} FROM `{settings.MYSQL_DAILY_REPORT_DB}`.`{table_name}`"
            )
            for item in await cur.fetchall():
                digest = None
                for value in item[1:]:
                    identity = normalize_identity(str(value or ""))
                    if identity:
                        digest = hmac_digest(identity, kind="identity")[0]
                        break
                if digest != identity_hmac:
                    continue
                dispatch_at = datetime.combine(report_date, datetime.min.time())
                if dispatch_at < valid_from or (valid_to and dispatch_at > valid_to) or (released_at and dispatch_at >= released_at):
                    continue
                await _snapshot_assignment_row(
                    cur, parser_type=parser_type, row_key=str(item[0]), identity_hmac=identity_hmac,
                    dispatch_at=dispatch_at, assignment_id=assignment_id,
                    assignment_version=version_no, reason="historical_backfill",
                )
                inserted += int(cur.rowcount or 0)
    return inserted


async def task_watch_payload(cur, parser_type: str, row_keys: list[str]) -> dict[str, dict]:
    if not row_keys:
        return {}
    placeholders = ",".join(["%s"] * len(row_keys))
    registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
    await cur.execute(
        f"""
        SELECT snapshot.row_key, snapshot.first_dispatch_at, category.id,
               category.name, category.color, category.alert_level,
               assignment.status, assignment.source_type,
               snapshot.snapshot_status, snapshot.snapshot_reason
        FROM `{registry}`.online_task_watch_snapshots snapshot
        JOIN `{registry}`.watch_assignments assignment
          ON assignment.id=snapshot.assignment_id
        JOIN `{registry}`.watch_categories category
          ON category.id=assignment.category_id
        WHERE snapshot.parser_type=%s
          AND snapshot.row_key IN ({placeholders})
        ORDER BY category.sort_order, category.id
        """,
        (parser_type, *row_keys),
    )
    result: dict[str, dict] = {}
    for row in await cur.fetchall():
        item = result.setdefault(str(row[0]), {"first_dispatch_at": row[1], "watch_marks": []})
        item["watch_marks"].append({
            "category_id": int(row[2]), "name": str(row[3]), "color": str(row[4]),
            "alert_level": str(row[5]), "assignment_status": str(row[6]),
            "source_type": str(row[7]), "snapshot_status": str(row[8]),
            "snapshot_reason": str(row[9]),
        })
    return result
