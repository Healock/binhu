"""人员标签的身份证精确命中和任务快照回填。"""

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


async def sync_current_task_snapshots(
    cur,
    parser_type: str,
    row_keys: list[str] | None = None,
) -> None:
    """用当前投影幂等补齐当时有效的人员标签。"""
    if not settings.REGISTRY_FEATURE_ENABLED:
        return
    registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
    normalized_keys = list(dict.fromkeys(
        str(row_key).strip() for row_key in (row_keys or [])
        if str(row_key).strip()
    ))
    row_filter = ""
    params: list[object] = [parser_type]
    if normalized_keys:
        placeholders = ",".join(["%s"] * len(normalized_keys))
        row_filter = f" AND projection.row_key IN ({placeholders})"
        params.extend(normalized_keys)
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
          {row_filter}
        """,
        params,
    )


async def sync_current_task_snapshots_for_keys(cur, parser_type: str, row_keys: list[str]) -> int:
    """增量投影时只同步受影响任务的人员标签快照。"""
    if not settings.REGISTRY_FEATURE_ENABLED or not row_keys:
        return 0
    placeholders = ",".join(["%s"] * len(row_keys))
    registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
    online = settings.MYSQL_ONLINE_DATA_DB.replace("`", "")
    if not SAFE_IDENTIFIER.fullmatch(registry) or not SAFE_IDENTIFIER.fullmatch(online):
        raise ValueError("task view state database identity invalid")
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
               assignment.id, COALESCE(version_info.version_no, 1),
               'active', 'incremental_match', UTC_TIMESTAMP()
        FROM `{online}`._online_source_projection projection
        JOIN `{registry}`.watch_people person
          ON person.identity_hmac=projection.identity_hmac
         AND person.status='active'
        JOIN `{registry}`.watch_assignments assignment
          ON assignment.person_id=person.id
         AND (
             (assignment.valid_from<=COALESCE(projection.first_dispatch_at, projection.updated_at)
              AND (assignment.valid_to IS NULL OR assignment.valid_to>=COALESCE(projection.first_dispatch_at, projection.updated_at))
              AND (assignment.released_at IS NULL OR assignment.released_at>COALESCE(projection.first_dispatch_at, projection.updated_at)))
             OR (projection.parser_type='疑似未注销模型三'
                 AND assignment.status='active'
                 AND assignment.valid_from<=UTC_TIMESTAMP()
                 AND (assignment.valid_to IS NULL OR assignment.valid_to>=UTC_TIMESTAMP())
                 AND (assignment.released_at IS NULL OR assignment.released_at>UTC_TIMESTAMP()))
         )
        LEFT JOIN (
            SELECT assignment_id, MAX(version_no) AS version_no
            FROM `{registry}`.watch_assignment_versions GROUP BY assignment_id
        ) version_info ON version_info.assignment_id=assignment.id
        WHERE projection.parser_type=%s AND projection.row_key IN ({placeholders})
          AND projection.identity_hmac IS NOT NULL
        """,
        (parser_type, *row_keys),
    )
    inserted = max(0, int(cur.rowcount or 0))
    await emit_watch_view_states_for_tasks(cur, parser_type, row_keys)
    return inserted


async def sync_current_watch_snapshots_for_identities(
    cur, identity_hmacs: list[str],
) -> int:
    """Refresh only live task snapshots affected by a batch of people identities."""
    unique = list(dict.fromkeys(
        str(value).strip() for value in identity_hmacs if str(value).strip()
    ))
    if not unique or not settings.REGISTRY_FEATURE_ENABLED:
        return 0
    online = settings.MYSQL_ONLINE_DATA_DB.replace("`", "")
    if not SAFE_IDENTIFIER.fullmatch(online):
        raise ValueError("task view state database identity invalid")
    affected: dict[str, list[str]] = {}
    for offset in range(0, len(unique), 400):
        batch = unique[offset:offset + 400]
        placeholders = ",".join(["%s"] * len(batch))
        await cur.execute(
            f"SELECT parser_type,row_key FROM `{online}`.`_online_source_projection` "
            f"WHERE identity_hmac IN ({placeholders}) ORDER BY parser_type,row_key",
            batch,
        )
        for parser_type, row_key in await cur.fetchall():
            affected.setdefault(str(parser_type), []).append(str(row_key))
    refreshed = 0
    for parser_type, row_keys in affected.items():
        for offset in range(0, len(row_keys), 300):
            refreshed += await sync_current_task_snapshots_for_keys(
                cur, parser_type, row_keys[offset:offset + 300],
            )
    return refreshed


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
            in_dispatch_window = not (
                dispatch_at < valid_from
                or (valid_to and dispatch_at > valid_to)
                or (released_at and dispatch_at >= released_at)
            )
            current_model_three_assignment = (
                str(parser_type) == "疑似未注销模型三"
                and valid_from <= datetime.utcnow()
                and (valid_to is None or valid_to >= datetime.utcnow())
                and (released_at is None or released_at > datetime.utcnow())
            )
            if not in_dispatch_window and not current_model_three_assignment:
                continue
            await _snapshot_assignment_row(
                cur, parser_type=str(parser_type), row_key=str(row_key), identity_hmac=identity_hmac,
                dispatch_at=dispatch_at, assignment_id=assignment_id,
                assignment_version=version_no,
                reason=("current_active_assignment" if current_model_three_assignment and not in_dispatch_window
                        else "historical_backfill"),
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

        await emit_watch_view_states_for_assignment(cur, assignment_id)
    return inserted


async def emit_watch_view_states_for_assignment(cur, assignment_id: int) -> None:
    """Publish the complete watch state for tasks affected by one assignment."""
    from services.task_view_state import (
        enqueue_source_task_view_state, task_view_state_events_enabled,
    )

    if not task_view_state_events_enabled():
        return
    await cur.execute(
        "SELECT DISTINCT parser_type,row_key FROM online_task_watch_snapshots "
        "WHERE assignment_id=%s ORDER BY parser_type,row_key",
        (assignment_id,),
    )
    affected_by_type: dict[str, list[str]] = {}
    for parser_type, row_key in await cur.fetchall():
        affected_by_type.setdefault(str(parser_type), []).append(str(row_key))
    for parser_type, row_keys in affected_by_type.items():
        await emit_watch_view_states_for_tasks(cur, parser_type, row_keys)


async def emit_watch_view_states_for_tasks(
    cur, parser_type: str, row_keys: list[str],
) -> None:
    """Publish complete watch state after task or assignment snapshots change."""
    from services.task_view_state import (
        enqueue_source_task_view_state, task_view_state_events_enabled,
    )

    if not task_view_state_events_enabled() or not row_keys:
        return
    online = settings.MYSQL_ONLINE_DATA_DB.replace("`", "")
    registry = settings.MYSQL_REGISTRY_DB.replace("`", "")
    if not SAFE_IDENTIFIER.fullmatch(online) or not SAFE_IDENTIFIER.fullmatch(registry):
        raise ValueError("task view state database identity invalid")
    for offset in range(0, len(row_keys), 300):
        batch = list(dict.fromkeys(row_keys[offset:offset + 300]))
        placeholders = ",".join(["%s"] * len(batch))
        await cur.execute(
            f"SELECT source.id,source.row_key,snapshot.first_dispatch_at,"
            f"category.id,category.name,category.color,category.alert_level,"
            f"assignment.status,assignment.source_type,snapshot.snapshot_status,"
            f"snapshot.snapshot_reason FROM `{online}`.`_online_source_rows` source "
            f"LEFT JOIN `{registry}`.`online_task_watch_snapshots` snapshot "
            "ON snapshot.parser_type=source.parser_type AND snapshot.row_key=source.row_key "
            f"LEFT JOIN `{registry}`.`watch_assignments` assignment "
            "ON assignment.id=snapshot.assignment_id "
            f"LEFT JOIN `{registry}`.`watch_categories` category "
            "ON category.id=assignment.category_id "
            "WHERE source.parser_type=%s AND source.row_key IN (" + placeholders + ") "
            "AND source.archived_at IS NULL ORDER BY source.id,category.sort_order,category.id",
            (parser_type, *batch),
        )
        states: dict[int, dict] = {}
        for row in await cur.fetchall():
            source_id = int(row[0])
            state = states.setdefault(source_id, {
                "row_key": str(row[1]), "first_dispatch_at": "", "watch_marks": [],
            })
            if row[3] is None:
                continue
            state["first_dispatch_at"] = state["first_dispatch_at"] or str(row[2] or "")
            state["watch_marks"].append({
                "category_id": int(row[3]), "name": str(row[4] or ""),
                "color": str(row[5] or ""), "alert_level": str(row[6] or ""),
                "assignment_status": str(row[7] or ""), "source_type": str(row[8] or ""),
                "snapshot_status": str(row[9] or ""), "snapshot_reason": str(row[10] or ""),
            })
        for source_id, state in states.items():
            marks = state.pop("watch_marks")
            await enqueue_source_task_view_state(
                cur, source_id=source_id, domain="watch",
                state={
                    "status": "active" if marks else "none",
                    "first_dispatch_at": state["first_dispatch_at"],
                    "watch_marks": marks,
                },
            )


async def task_watch_payload(
    cur,
    parser_type: str,
    row_keys: list[str],
    *,
    task_values_by_row: dict[str, dict] | None = None,
) -> dict[str, dict]:
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
    rows = await cur.fetchall()
    for row in rows:
        item = result.setdefault(str(row[0]), {"first_dispatch_at": row[1], "watch_marks": []})
        item["watch_marks"].append({
            "category_id": int(row[2]), "name": str(row[3]), "color": str(row[4]),
            "alert_level": str(row[5]), "assignment_status": str(row[6]),
            "source_type": str(row[7]), "snapshot_status": str(row[8]),
            "snapshot_reason": str(row[9]),
        })

    # Model-three self-owned imports may be added after a task was dispatched.
    # In that case the historical validity window intentionally prevents a
    # snapshot from being backfilled, but the current personnel-archive tag
    # still needs to be visible on the live task.  Fill only missing
    # row/assignment pairs from the current active assignment state.
    if parser_type == "疑似未注销模型三":
        if settings.TASK_READ_MODEL_ENABLED:
            from services.parsers import get_parser

            parser = get_parser(parser_type)
            identities: dict[str, tuple[str, datetime]] = {}
            for row_key in row_keys:
                values = (task_values_by_row or {}).get(row_key)
                if not isinstance(values, dict):
                    continue
                identity_hmac = projection_identity(parser_type, values, parser.COLUMNS)
                if identity_hmac:
                    identities[row_key] = (
                        identity_hmac,
                        parse_dispatch_time(values, _dispatch_fields(parser_type, parser.COLUMNS)),
                    )
            if identities:
                identity_values = list(dict.fromkeys(value[0] for value in identities.values()))
                identity_placeholders = ",".join(["%s"] * len(identity_values))
                await cur.execute(
                    f"""
                    SELECT person.identity_hmac, category.id, category.name,
                           category.color, category.alert_level, assignment.status,
                           assignment.source_type, assignment.id
                    FROM `{registry}`.watch_people person
                    JOIN `{registry}`.watch_assignments assignment
                      ON assignment.person_id=person.id
                     AND assignment.status='active'
                     AND assignment.valid_from<=UTC_TIMESTAMP()
                     AND (assignment.valid_to IS NULL OR assignment.valid_to>=UTC_TIMESTAMP())
                     AND (assignment.released_at IS NULL OR assignment.released_at>UTC_TIMESTAMP())
                    JOIN `{registry}`.watch_categories category
                      ON category.id=assignment.category_id AND category.is_active=1
                    WHERE person.identity_hmac IN ({identity_placeholders})
                      AND person.status='active'
                    ORDER BY category.sort_order, category.id
                    """,
                    tuple(identity_values),
                )
                by_identity: dict[str, list[str]] = {}
                for row_key, (identity_hmac, _dispatch_at) in identities.items():
                    by_identity.setdefault(identity_hmac, []).append(row_key)
                existing = {
                    (row_key, int(mark["category_id"]))
                    for row_key, payload in result.items()
                    for mark in payload["watch_marks"]
                }
                for row in await cur.fetchall():
                    identity_hmac = str(row[0] or "")
                    category_id = int(row[1])
                    for row_key in by_identity.get(identity_hmac, []):
                        if (row_key, category_id) in existing:
                            continue
                        item = result.setdefault(
                            row_key,
                            {"first_dispatch_at": identities[row_key][1], "watch_marks": []},
                        )
                        item["watch_marks"].append({
                            "category_id": int(row[1]), "name": str(row[2]),
                            "color": str(row[3]), "alert_level": str(row[4]),
                            "assignment_status": str(row[5]),
                            "source_type": str(row[6]),
                            "snapshot_status": "active",
                            "snapshot_reason": "current_registry_tag",
                        })
                        existing.add((row_key, category_id))
            return result

        await cur.execute(
            f"""
            SELECT projection.row_key, projection.first_dispatch_at,
                   category.id, category.name, category.color, category.alert_level,
                   assignment.status, assignment.source_type,
                   'active', 'current_registry_tag'
            FROM _online_source_projection projection
            JOIN `{registry}`.watch_people person
              ON person.identity_hmac=projection.identity_hmac
             AND person.status='active'
            JOIN `{registry}`.watch_assignments assignment
              ON assignment.person_id=person.id
             AND assignment.status='active'
             AND assignment.valid_from<=UTC_TIMESTAMP()
             AND (assignment.valid_to IS NULL OR assignment.valid_to>=UTC_TIMESTAMP())
             AND (assignment.released_at IS NULL OR assignment.released_at>UTC_TIMESTAMP())
            JOIN `{registry}`.watch_categories category
              ON category.id=assignment.category_id
             AND category.is_active=1
            WHERE projection.parser_type=%s
              AND projection.row_key IN ({placeholders})
              AND NOT EXISTS (
                SELECT 1
                FROM `{registry}`.online_task_watch_snapshots snapshot
                WHERE snapshot.parser_type=projection.parser_type
                  AND snapshot.row_key=projection.row_key
                  AND snapshot.assignment_id=assignment.id
              )
            ORDER BY category.sort_order, category.id
            """,
            (parser_type, *row_keys),
        )
        for row in await cur.fetchall():
            item = result.setdefault(str(row[0]), {"first_dispatch_at": row[1], "watch_marks": []})
            item["watch_marks"].append({
                "category_id": int(row[2]), "name": str(row[3]), "color": str(row[4]),
                "alert_level": str(row[5]), "assignment_status": str(row[6]),
                "source_type": str(row[7]), "snapshot_status": str(row[8]),
                "snapshot_reason": str(row[9]),
            })
    return result
