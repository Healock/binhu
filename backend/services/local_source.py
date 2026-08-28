"""本地业务来源层。

腾讯文档下线后，平台仍需要一个统一的来源身份供任务、登记、研判和
归档流程使用。本模块复用现有来源投影表的稳定接口，但来源行固定为
``spreadsheet_id=0``、``sheet_id=local:<parser>``，不再代表外部物理行。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from config import settings
from services.parsers import get_parser


LOCAL_SPREADSHEET_ID = 0


def local_sheet_id(parser_type: str) -> str:
    return f"local:{parser_type}"


def local_data_source_enabled() -> bool:
    return bool(settings.LOCAL_DATA_SOURCE_ENABLED)


def tencent_access_enabled() -> bool:
    return bool(settings.TXDOCS_ENABLED)


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
