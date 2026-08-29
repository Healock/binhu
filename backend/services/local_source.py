"""本地业务来源层。

腾讯文档下线后，平台仍需要一个统一的来源身份供任务、登记、研判和
归档流程使用。本模块复用现有来源投影表的稳定接口，但来源行固定为
``spreadsheet_id=0``、``sheet_id=local:<parser>``，不再代表外部物理行。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from services.parsers import get_parser


LOCAL_SPREADSHEET_ID = 0


def local_sheet_id(parser_type: str) -> str:
    return f"local:{parser_type}"


def local_data_source_enabled() -> bool:
    """Return the permanent production data-source mode.

    The environment flag remains in configuration so old deployment files do
    not fail to load, but it can no longer reactivate the retired Tencent path.
    """
    return True


def tencent_access_enabled() -> bool:
    """Tencent Docs runtime access is permanently disabled."""
    return False


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def local_row_hash(values: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(stable_json(values).encode("utf-8")).hexdigest()


async def ensure_local_source_schema(cur) -> None:
    """幂等增加本地来源元数据和迁移问题表。"""
    for column, definition in (
        ("source_kind", "VARCHAR(40) NOT NULL DEFAULT 'tencent_legacy'"),
        ("source_ref", "VARCHAR(190) NOT NULL DEFAULT ''"),
        ("archived_at", "DATETIME DEFAULT NULL"),
    ):
        await cur.execute("SHOW COLUMNS FROM `_online_source_rows` LIKE %s", (column,))
        if not await cur.fetchone():
            await cur.execute(
                f"ALTER TABLE `_online_source_rows` ADD COLUMN `{column}` {definition}"
            )
    await cur.execute(
        "SHOW INDEX FROM `_online_source_rows` WHERE Key_name=%s",
        ("idx_local_source_kind",),
    )
    if not await cur.fetchone():
        await cur.execute(
            "ALTER TABLE `_online_source_rows` ADD INDEX "
            "idx_local_source_kind (source_kind, parser_type, row_key)"
        )
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _local_source_records (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            parser_type VARCHAR(50) NOT NULL,
            local_task_id BIGINT DEFAULT NULL,
            business_key CHAR(32) NOT NULL,
            source_kind VARCHAR(40) NOT NULL,
            source_ref VARCHAR(190) NOT NULL,
            values_json JSON NOT NULL,
            content_hash CHAR(64) NOT NULL,
            revision BIGINT UNSIGNED NOT NULL DEFAULT 1,
            status VARCHAR(20) NOT NULL DEFAULT 'active',
            archived_at DATETIME DEFAULT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uk_local_source_ref (source_kind, source_ref),
            INDEX idx_local_source_business (parser_type, business_key),
            INDEX idx_local_source_status (status, parser_type)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _local_source_migration_runs (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            mode VARCHAR(30) NOT NULL DEFAULT 'cache_snapshot',
            status VARCHAR(20) NOT NULL DEFAULT 'running',
            started_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            finished_at DATETIME DEFAULT NULL,
            total_count INT NOT NULL DEFAULT 0,
            migrated_count INT NOT NULL DEFAULT 0,
            issue_count INT NOT NULL DEFAULT 0,
            detail_json JSON NOT NULL,
            UNIQUE KEY uk_local_source_migration_mode (mode)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)
    await cur.execute("""
        CREATE TABLE IF NOT EXISTS _local_source_migration_issues (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            run_id BIGINT NOT NULL,
            parser_type VARCHAR(50) NOT NULL,
            row_key VARCHAR(200) NOT NULL,
            issue_code VARCHAR(50) NOT NULL,
            detail_json JSON NOT NULL,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_local_source_issue_run (run_id, parser_type),
            INDEX idx_local_source_issue_key (parser_type, row_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
    """)


async def create_local_source_row(
    cur,
    parser_type: str,
    values: dict[str, Any],
    *,
    source_kind: str = "local_dispatch",
    source_ref: str,
) -> dict[str, Any]:
    """在本地业务表创建一条来源行，并同步来源投影与统一来源记录。

    该函数是阶段二下发流程的唯一写入入口之一：业务表、来源投影和
    ``_local_source_records`` 必须在调用方同一个事务中完成。相同业务键
    且内容一致时返回已有记录；相同业务键但内容不同则抛出受控冲突，
    不会悄悄覆盖网格员已经填写的字段。
    """
    parser = get_parser(parser_type)
    normalized = {
        column: str(values.get(column, "") or "").strip()
        for column in parser.COLUMNS
    }
    parser.validate_new_row(normalized)
    row_key = parser.make_row_key(normalized)
    content_hash = local_row_hash(normalized)
    table_name = parser.table_name.replace("`", "")

    await cur.execute(
        f"SELECT id, _row_key, "
        + ", ".join(f"`{column}`" for column in parser.COLUMNS)
        + f" FROM `{table_name}` WHERE `_row_key`=%s LIMIT 1 FOR UPDATE",
        (row_key,),
    )
    existing = await cur.fetchone()
    if existing:
        existing_values = {
            column: str(existing[index + 2] or "")
            for index, column in enumerate(parser.COLUMNS)
        }
        existing_hash = local_row_hash(existing_values)
        if existing_hash != content_hash:
            raise ValueError("local_business_key_conflict")
        local_task_id = int(existing[0])
    else:
        columns = ["_row_key", *parser.COLUMNS]
        quoted = ", ".join(f"`{column}`" for column in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        await cur.execute(
            f"INSERT INTO `{table_name}` ({quoted}) VALUES ({placeholders})",
            [row_key, *(normalized[column] for column in parser.COLUMNS)],
        )
        local_task_id = int(cur.lastrowid)

    source_ref = str(source_ref or f"{table_name}:{local_task_id}")[:190]
    await cur.execute(
        "SELECT id, revision, row_key, row_hash FROM _online_source_rows "
        "WHERE source_kind=%s AND source_ref=%s LIMIT 1 FOR UPDATE",
        (source_kind, source_ref),
    )
    source_row = await cur.fetchone()
    if source_row:
        if str(source_row[2] or "") != row_key or str(source_row[3] or "") != content_hash:
            raise ValueError("local_business_key_conflict")
        source_id = int(source_row[0])
        revision = int(source_row[1] or 1)
        await cur.execute(
            "UPDATE _online_source_rows SET spreadsheet_id=0, parser_type=%s, "
            "sheet_id=%s, physical_row=%s, row_key=%s, row_hash=%s, "
            "values_json=%s, revision=%s, source_kind=%s, source_ref=%s, "
            "archived_at=NULL, refreshed_at=UTC_TIMESTAMP() WHERE id=%s",
            (
                parser_type, local_sheet_id(parser_type), local_task_id, row_key,
                content_hash, stable_json(normalized), revision, source_kind,
                source_ref, source_id,
            ),
        )
    else:
        await cur.execute(
            "SELECT id FROM _online_source_rows "
            "WHERE spreadsheet_id=0 AND parser_type=%s AND row_key=%s "
            "AND archived_at IS NULL LIMIT 1 FOR UPDATE",
            (parser_type, row_key),
        )
        matching = await cur.fetchone()
        if matching:
            source_id = int(matching[0])
            await cur.execute(
                "SELECT row_hash, values_json, physical_row, source_ref, revision "
                "FROM _online_source_rows "
                "WHERE id=%s FOR UPDATE",
                (source_id,),
            )
            matched_source = await cur.fetchone()
            if not matched_source or str(matched_source[0] or "") != content_hash:
                raise ValueError("local_business_key_conflict")
            revision = int(matched_source[4] or 1)
            previous_source_ref = str(matched_source[3] or "")[:190]
            if previous_source_ref and previous_source_ref != source_ref:
                # A mirrored legacy row can later become the source of a
                # dispatch task.  Keep the new source reference authoritative
                # and leave the old mirror record as an auditable superseded
                # record instead of creating a second active source row.
                await cur.execute(
                    "UPDATE _local_source_records SET status='superseded',"
                    "updated_at=UTC_TIMESTAMP() WHERE source_ref=%s",
                    (previous_source_ref,),
                )
            await cur.execute(
                "UPDATE _online_source_rows SET spreadsheet_id=0, parser_type=%s, "
                "sheet_id=%s, physical_row=%s, row_key=%s, row_hash=%s, "
                "values_json=%s, revision=%s, source_kind=%s, source_ref=%s, "
                "archived_at=NULL, refreshed_at=UTC_TIMESTAMP() WHERE id=%s",
                (
                    parser_type, local_sheet_id(parser_type), local_task_id, row_key,
                    content_hash, stable_json(normalized), revision, source_kind,
                    source_ref, source_id,
                ),
            )
        else:
            await cur.execute(
                "INSERT INTO _online_source_rows ("
                "spreadsheet_id,parser_type,sheet_id,physical_row,row_key,row_hash,"
                "values_json,cell_meta_json,revision,refreshed_at,source_kind,source_ref"
                ") VALUES (0,%s,%s,%s,%s,%s,%s,%s,1,UTC_TIMESTAMP(),%s,%s)",
                (
                    parser_type, local_sheet_id(parser_type), local_task_id, row_key,
                    content_hash, stable_json(normalized),
                    stable_json({column: {"type": "text"} for column in parser.COLUMNS}),
                    source_kind, source_ref,
                ),
            )
            source_id = int(cur.lastrowid)

    await cur.execute(
        "SELECT id FROM _local_source_records "
        "WHERE source_kind=%s AND source_ref=%s LIMIT 1 FOR UPDATE",
        (source_kind, source_ref),
    )
    local_record = await cur.fetchone()
    if local_record:
        await cur.execute(
            "UPDATE _local_source_records SET parser_type=%s, local_task_id=%s, "
            "business_key=%s, values_json=%s, content_hash=%s, status='active', "
            "archived_at=NULL, updated_at=UTC_TIMESTAMP() WHERE id=%s",
            (
                parser_type, local_task_id, row_key, stable_json(normalized),
                content_hash, int(local_record[0]),
            ),
        )
    else:
        await cur.execute(
            "INSERT INTO _local_source_records ("
            "parser_type,local_task_id,business_key,source_kind,source_ref,"
            "values_json,content_hash,status) VALUES (%s,%s,%s,%s,%s,%s,%s,'active')",
            (
                parser_type, local_task_id, row_key, source_kind, source_ref,
                stable_json(normalized), content_hash,
            ),
        )
    return {
        "id": source_id,
        "local_task_id": local_task_id,
        "row_key": row_key,
        "row_hash": content_hash,
        "values": normalized,
    }

async def mirror_business_tables_to_local_sources(
    conn,
    parser_types: Iterable[str] | None = None,
    *,
    migration_run_id: int | None = None,
) -> dict[str, int]:
    """把现有本地业务表幂等镜像为统一来源记录。

    该过程不访问任何外部服务，可在维护窗口反复执行。业务表的自增 id
    作为稳定的本地来源位置，仅用于兼容旧任务流程，不再表示腾讯物理行号。
    """
    selected = list(parser_types or [
        "全链条", "出租房屋核查", "涉警统计", "疑似未注销模型三",
        "疑似返苏", "寄递业", "群租房核查", "苏州涉警", "交通涉警",
    ])
    counts: dict[str, int] = {}
    async with conn.cursor() as cur:
        for parser_type in selected:
            parser = get_parser(parser_type)
            columns = list(parser.COLUMNS)
            quoted = ", ".join(f"`{column}`" for column in columns)
            await cur.execute(
                f"SELECT id, _row_key, {quoted} FROM `{parser.table_name}`"
            )
            rows = await cur.fetchall()
            count = 0
            seen_business_keys: dict[str, int] = {}
            for row in rows:
                values = {
                    column: str(row[index + 2] or "")
                    for index, column in enumerate(columns)
                }
                row_key = str(row[1] or parser.make_row_key(values))
                source_ref = f"{parser.table_name}:{int(row[0])}"
                seen_business_keys[row_key] = seen_business_keys.get(row_key, 0) + 1
                content_hash = local_row_hash(values)
                await cur.execute(
                    "SELECT id, status FROM _local_source_records "
                    "WHERE source_kind='local_table' AND source_ref=%s LIMIT 1",
                    (source_ref,),
                )
                local_record = await cur.fetchone()
                if local_record:
                    await cur.execute(
                        "UPDATE _local_source_records SET parser_type=%s, "
                        "local_task_id=%s, business_key=%s, values_json=%s, "
                        "content_hash=%s, status='active', archived_at=NULL, "
                        "updated_at=UTC_TIMESTAMP() WHERE id=%s",
                        (
                            parser_type, int(row[0]), row_key, stable_json(values),
                            content_hash, int(local_record[0]),
                        ),
                    )
                else:
                    await cur.execute(
                        "INSERT INTO _local_source_records ("
                        "parser_type,local_task_id,business_key,source_kind,source_ref,"
                        "values_json,content_hash,status) VALUES (%s,%s,%s,'local_table',%s,%s,%s,'active')",
                        (
                            parser_type, int(row[0]), row_key, source_ref,
                            stable_json(values), content_hash,
                        ),
                    )
                await cur.execute(
                    "SELECT id FROM _online_source_rows WHERE source_kind='local_table' "
                    "AND source_ref=%s AND archived_at IS NULL LIMIT 1",
                    (source_ref,),
                )
                existing = await cur.fetchone()
                if not existing:
                    await cur.execute(
                        "SELECT id, source_ref FROM _online_source_rows WHERE parser_type=%s "
                        "AND row_key=%s AND archived_at IS NULL "
                        "ORDER BY id",
                        (parser_type, row_key),
                    )
                    same_key_rows = await cur.fetchall()
                    if len(same_key_rows) == 1:
                        existing = same_key_rows[0]
                    elif len(same_key_rows) > 1:
                        matching = [item for item in same_key_rows if str(item[1] or '') == source_ref]
                        existing = matching[0] if len(matching) == 1 else None
                        await _record_migration_issue(
                            cur,
                            run_id=migration_run_id,
                            parser_type=parser_type,
                            row_key=row_key,
                            issue_code="duplicate_business_key",
                            detail={"source_ref": source_ref, "count": len(same_key_rows)},
                        )
                if existing:
                    await cur.execute(
                        "UPDATE _online_source_rows SET spreadsheet_id=0, parser_type=%s, "
                        "sheet_id=%s, physical_row=%s, row_key=%s, row_hash=%s, "
                        "values_json=%s, source_kind='local_table', source_ref=%s, "
                        "archived_at=NULL, refreshed_at=UTC_TIMESTAMP() "
                        "WHERE id=%s",
                        (
                            parser_type, local_sheet_id(parser_type), int(row[0]), row_key,
                            content_hash, stable_json(values), source_ref, int(existing[0]),
                        ),
                    )
                else:
                    await cur.execute(
                        "INSERT INTO _online_source_rows ("
                        "spreadsheet_id, parser_type, sheet_id, physical_row, row_key, row_hash, "
                        "values_json, cell_meta_json, revision, refreshed_at, source_kind, source_ref"
                        ") VALUES (0,%s,%s,%s,%s,%s,%s,%s,1,UTC_TIMESTAMP(),'local_table',%s)",
                        (
                            parser_type, local_sheet_id(parser_type), int(row[0]), row_key,
                            content_hash, stable_json(values),
                            stable_json({column: {"type": "text"} for column in columns}), source_ref,
                        ),
                    )
                count += 1
            duplicate_count = sum(1 for value in seen_business_keys.values() if value > 1)
            if duplicate_count:
                await _record_migration_issue(
                    cur,
                    run_id=migration_run_id,
                    parser_type=parser_type,
                    row_key="*",
                    issue_code="duplicate_business_key_count",
                    detail={"count": duplicate_count},
                )
            counts[parser_type] = count
            from services.online_source import rebuild_projection
            await rebuild_projection(cur, parser_type, reconcile_graph=False)
    return counts


async def cleanup_duplicate_local_sources(conn, *, apply: bool = False) -> dict[str, Any]:
    """Audit and optionally merge identical active local sources.

    The cleanup is deliberately conservative: rows with different content hashes
    remain active and continue to surface as source exceptions.  Identical rows
    keep the oldest source as the canonical identity; duplicate rows are marked
    superseded/archived so their audit history remains available.
    """
    from services.online_source import rebuild_projection

    result: dict[str, Any] = {
        "groups": 0,
        "identical_groups": 0,
        "conflict_groups": 0,
        "merged_rows": 0,
        "conflicts": [],
        "dry_run": not apply,
    }
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id,parser_type,row_key,row_hash,revision,source_ref FROM _online_source_rows "
            "WHERE spreadsheet_id=0 AND archived_at IS NULL "
            "ORDER BY parser_type,row_key,id FOR UPDATE"
        )
        rows = await cur.fetchall()
        groups: dict[tuple[str, str], list[tuple]] = {}
        for row in rows:
            groups.setdefault((str(row[1]), str(row[2])), []).append(row)
        result["groups"] = sum(1 for values in groups.values() if len(values) > 1)
        touched: set[str] = set()

        async def table_exists(name: str) -> bool:
            await cur.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE() "
                "AND table_name=%s LIMIT 1",
                (name,),
            )
            return bool(await cur.fetchone())

        # Discover references instead of keeping a brittle hand-maintained list.
        # Several workflow tables are created by optional modules and their names
        # differ across older deployments.  Every source_id foreign reference in
        # this database is safe to repoint; the source tables themselves are
        # explicitly excluded below.
        await cur.execute(
            "SELECT DISTINCT c.table_name FROM information_schema.columns c "
            "WHERE c.table_schema=DATABASE() AND c.column_name='source_id'"
        )
        existing_reference_tables = {
            str(row[0]) for row in await cur.fetchall()
            if str(row[0]) not in {
                "_online_source_rows", "_local_source_records",
                "_local_source_migration_issues",
            }
        }
        for (parser_type, row_key), source_rows in groups.items():
            if len(source_rows) <= 1:
                continue
            hashes = {str(row[3] or "") for row in source_rows}
            if len(hashes) != 1:
                result["conflict_groups"] += 1
                result["conflicts"].append({
                    "parser_type": parser_type,
                    "row_key": row_key,
                    "source_ids": [int(row[0]) for row in source_rows],
                    "count": len(source_rows),
                })
                continue
            result["identical_groups"] += 1
            canonical = source_rows[0]
            canonical_ref = str(canonical[5] or "")
            if not apply:
                continue
            for duplicate_row in source_rows[1:]:
                duplicate_id = int(duplicate_row[0])
                for table in existing_reference_tables:
                    try:
                        await cur.execute(
                            f"UPDATE `{table}` SET source_id=%s WHERE source_id=%s",
                            (int(canonical[0]), duplicate_id),
                        )
                    except Exception:
                        # A legacy reference table may not expose source_id;
                        # cleanup must remain auditable and never abort the group.
                        continue
                if await table_exists("task_graph_nodes"):
                    duplicate_ref = str(duplicate_row[5] or "")
                    await cur.execute(
                        "UPDATE task_graph_nodes node "
                        "JOIN task_graph_nodes canonical_node ON "
                        "canonical_node.task_type=node.task_type "
                        "AND canonical_node.provider=node.provider "
                        "AND canonical_node.parser_type=node.parser_type "
                        "AND canonical_node.source_ref=%s "
                        "SET node.status='superseded', node.archived_at=UTC_TIMESTAMP() "
                        "WHERE node.source_ref=%s AND node.source_ref<>%s",
                        (canonical_ref, duplicate_ref, canonical_ref),
                    )
                    await cur.execute(
                        "UPDATE task_graph_nodes node SET node.source_ref=%s "
                        "WHERE node.source_ref=%s AND NOT EXISTS ("
                        "SELECT 1 FROM task_graph_nodes canonical_node "
                        "WHERE canonical_node.task_type=node.task_type "
                        "AND canonical_node.provider=node.provider "
                        "AND canonical_node.parser_type=node.parser_type "
                        "AND canonical_node.source_ref=%s)",
                        (canonical_ref, duplicate_ref, canonical_ref),
                    )
                await cur.execute(
                    "UPDATE _online_source_rows SET archived_at=UTC_TIMESTAMP(), "
                    "source_kind='superseded' WHERE id=%s AND archived_at IS NULL",
                    (duplicate_id,),
                )
                await cur.execute(
                    "UPDATE _local_source_records SET status='superseded', "
                    "archived_at=UTC_TIMESTAMP(),updated_at=UTC_TIMESTAMP() "
                    "WHERE source_kind='local_table' AND source_ref IN "
                    "(SELECT source_ref FROM _online_source_rows WHERE id=%s)",
                    (duplicate_id,),
                )
                result["merged_rows"] += 1
            touched.add(parser_type)
        if apply:
            for parser_type in touched:
                await rebuild_projection(cur, parser_type, reconcile_graph=False)
            await conn.commit()
    return result


async def _record_migration_issue(
    cur,
    *,
    run_id: int | None,
    parser_type: str,
    row_key: str,
    issue_code: str,
    detail: dict[str, Any],
) -> None:
    """Record a safe, repeatable migration issue without sensitive row values."""
    if run_id is None:
        await cur.execute(
            "SELECT id FROM _local_source_migration_runs "
            "WHERE mode='cache_snapshot' LIMIT 1"
        )
        row = await cur.fetchone()
        run_id = int(row[0]) if row else 0
    if not run_id:
        return
    await cur.execute(
        "SELECT id FROM _local_source_migration_issues WHERE run_id=%s "
        "AND parser_type=%s AND row_key=%s AND issue_code=%s LIMIT 1",
        (run_id, parser_type, row_key, issue_code),
    )
    if await cur.fetchone():
        return
    await cur.execute(
        "INSERT INTO _local_source_migration_issues "
        "(run_id,parser_type,row_key,issue_code,detail_json) VALUES (%s,%s,%s,%s,%s)",
        (run_id, parser_type, row_key, issue_code, stable_json(detail)),
    )


async def run_local_source_migration(conn) -> dict[str, Any]:
    """执行一次幂等的本地来源迁移快照。

    迁移只读取已经落库的业务表，不访问腾讯 API；重复启动只更新校验统计。
    """
    already_completed = False
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id,status FROM _local_source_migration_runs "
            "WHERE mode='cache_snapshot' LIMIT 1"
        )
        row = await cur.fetchone()
        if row:
            run_id = int(row[0])
            if str(row[1]) == "completed":
                already_completed = True
            else:
                await cur.execute(
                    "UPDATE _local_source_migration_runs SET status='running', "
                    "started_at=UTC_TIMESTAMP(), finished_at=NULL WHERE id=%s",
                    (run_id,),
                )
        else:
            await cur.execute(
                "INSERT INTO _local_source_migration_runs "
                "(mode,status,detail_json) VALUES ('cache_snapshot','running','{}')"
            )
            run_id = int(cur.lastrowid)
    if already_completed:
        return await local_source_migration_status(conn)
    counts = await mirror_business_tables_to_local_sources(
        conn,
        migration_run_id=run_id,
    )
    total = sum(counts.values())
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT COUNT(*) FROM _local_source_migration_issues WHERE run_id=%s",
            (run_id,),
        )
        issue_count = int((await cur.fetchone())[0] or 0)
        await cur.execute(
            "UPDATE _local_source_migration_runs SET status='completed', "
            "finished_at=UTC_TIMESTAMP(), total_count=%s, migrated_count=%s, "
            "issue_count=%s, detail_json=%s WHERE id=%s",
            (
                total,
                total,
                issue_count,
                stable_json({
                    "counts": counts,
                    "external_snapshot": "not_requested",
                    "requires_readonly_cutover_check": True,
                }),
                run_id,
            ),
        )
    return {
        "run_id": run_id,
        "status": "completed",
        "total_count": total,
        "migrated_count": total,
        "issue_count": issue_count,
        "counts": counts,
        "external_snapshot": "not_requested",
        "requires_readonly_cutover_check": True,
    }


async def local_source_migration_status(conn) -> dict[str, Any]:
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id,mode,status,started_at,finished_at,total_count,"
            "migrated_count,issue_count,detail_json FROM _local_source_migration_runs "
            "ORDER BY id DESC LIMIT 1"
        )
        row = await cur.fetchone()
    if not row:
        return {"status": "not_started", "total_count": 0, "migrated_count": 0, "issue_count": 0}
    return {
        "run_id": int(row[0]),
        "mode": str(row[1]),
        "status": str(row[2]),
        "started_at": row[3].isoformat() + "Z" if row[3] else None,
        "finished_at": row[4].isoformat() + "Z" if row[4] else None,
        "total_count": int(row[5] or 0),
        "migrated_count": int(row[6] or 0),
        "issue_count": int(row[7] or 0),
        "detail": json.loads(row[8]) if isinstance(row[8], str) else (row[8] or {}),
    }


async def local_source_migration_issues(
    conn,
    *,
    parser_type: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Return paged, non-sensitive migration issues for admin review."""
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 200)
    async with conn.cursor() as cur:
        await cur.execute(
            "SELECT id FROM _local_source_migration_runs "
            "WHERE mode='cache_snapshot' ORDER BY id DESC LIMIT 1"
        )
        run = await cur.fetchone()
        if not run:
            return {"data": [], "total": 0, "page": page, "page_size": page_size}
        where = "run_id=%s"
        params: list[Any] = [int(run[0])]
        if parser_type:
            where += " AND parser_type=%s"
            params.append(parser_type)
        await cur.execute(
            f"SELECT COUNT(*) FROM _local_source_migration_issues WHERE {where}",
            tuple(params),
        )
        total = int((await cur.fetchone())[0] or 0)
        await cur.execute(
            f"SELECT id,parser_type,row_key,issue_code,detail_json,created_at "
            f"FROM _local_source_migration_issues WHERE {where} "
            "ORDER BY id LIMIT %s OFFSET %s",
            tuple(params + [page_size, (page - 1) * page_size]),
        )
        rows = await cur.fetchall()
    return {
        "data": [
            {
                "id": int(row[0]),
                "parser_type": str(row[1]),
                "row_key": str(row[2]),
                "issue_code": str(row[3]),
                "detail": json.loads(row[4]) if isinstance(row[4], str) else (row[4] or {}),
                "created_at": row[5].isoformat() + "Z" if row[5] else None,
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }
