"""数据同步引擎 - 增量比对 + 归档

流程：从腾讯文档读取 → 解析器解析 → 和数据库现有数据比对 → 新增/更新/归档移除的
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from services.txdocs_client import TxDocsClient
from services.parsers import get_parser
from services.business_time import get_business_date
from services.schema_compat import get_database_column_map, quote_identifier
from services.report_table_utils import table_exists
from services.online_source import (
    acquire_sheet_lock,
    cleanup_expired_writeback_audit,
    mark_writebacks_synced,
    rebuild_projection,
    release_sheet_lock,
    replace_source_cache,
    resolve_source_columns,
    source_row_hash,
)
from services.police_dispatch import reconcile_police_dispatch_publications
from services.qmf_source import MODEL_THREE_PARSER


@dataclass(frozen=True)
class SourceConflict:
    row_key: str
    physical_rows: tuple[int, ...]
    differing_columns: tuple[str, ...]


@dataclass
class DeduplicationResult:
    rows: dict[str, dict]
    duplicate_count: int
    conflicts: list[SourceConflict]

    def __iter__(self):
        """保留旧调用方的二元解包兼容。"""
        yield self.rows
        yield self.duplicate_count


def source_read_requires_confirmation(previous_count: int, current_count: int) -> bool:
    """来源突然清空或大幅骤降时要求第二次完整读取。"""
    if previous_count <= 0:
        return False
    if current_count == 0:
        return True
    return previous_count >= 20 and current_count * 2 < previous_count


def source_rows_digest(parser, source_rows: list[dict]) -> str:
    """生成不包含业务正文的完整读取摘要。"""
    digest = hashlib.sha256()
    for source in sorted(
        source_rows,
        key=lambda item: int(item.get("physical_row") or 0),
    ):
        values = parser.normalize_source_row(source.get("values", {}))
        digest.update(str(int(source.get("physical_row") or 0)).encode("ascii"))
        digest.update(b":")
        digest.update(source_row_hash(values).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


async def finalize_source_projection(
    conn,
    *,
    spreadsheet: dict,
    source_columns: list[str],
) -> None:
    """Publish the final projection without exposing a delete/insert gap."""
    await conn.begin()
    try:
        async with conn.cursor() as cur:
            if spreadsheet["parser_type"] == "全链条":
                await reconcile_police_dispatch_publications(
                    cur,
                    spreadsheet["id"],
                    source_columns,
                )
            await mark_writebacks_synced(cur, spreadsheet["id"])
            await rebuild_projection(cur, spreadsheet["parser_type"])
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


def deduplicate_rows(
    parser,
    raw_rows: list[dict],
    *,
    physical_rows: list[int] | None = None,
) -> DeduplicationResult:
    """按业务主键去重；只有解析器明确允许时才合并不同内容。"""
    online: dict[str, dict] = {}
    duplicate_count = 0
    conflict_details: dict[str, dict[str, set]] = {}
    representative_rows: dict[str, int] = {}

    for index, raw in enumerate(raw_rows):
        parsed = {
            key: "" if value is None else str(value).strip()
            for key, value in raw.items()
        }
        key = parser.make_row_key(parsed)
        if key not in online:
            online[key] = parsed
            representative_rows[key] = (
                int(physical_rows[index])
                if physical_rows is not None
                else index + 1
            )
            continue

        previous = online[key]
        if previous == parsed:
            duplicate_count += 1
            continue

        merged = parser.merge_duplicate_row(previous, parsed)
        if merged is not None:
            online[key] = merged
            duplicate_count += 1
            continue

        conflict = conflict_details.setdefault(
            key,
            {
                "physical_rows": {representative_rows[key]},
                "differing_columns": set(),
            },
        )
        conflict["physical_rows"].add(
            int(physical_rows[index])
            if physical_rows is not None
            else index + 1
        )
        conflict["differing_columns"].update(
            column
            for column in parser.COLUMNS
            if previous.get(column, "") != parsed.get(column, "")
        )
        if parser.ALLOW_SOURCE_CONFLICTS:
            duplicate_count += 1

    conflicts = [
        SourceConflict(
            row_key=row_key,
            physical_rows=tuple(sorted(detail["physical_rows"])),
            differing_columns=tuple(sorted(detail["differing_columns"])),
        )
        for row_key, detail in conflict_details.items()
    ]
    if conflicts and not parser.ALLOW_SOURCE_CONFLICTS:
        differing_columns = sorted({
            column
            for conflict in conflicts
            for column in conflict.differing_columns
        })
        raise ValueError(
            f"检测到 {sum(len(item.physical_rows) - 1 for item in conflicts)} 行主键相同但内容不同，"
            f"涉及字段: {', '.join(differing_columns)}；"
            "已停止该表同步，未覆盖任何冲突行"
        )

    return DeduplicationResult(online, duplicate_count, conflicts)


class SyncEngine:
    def __init__(self, db_pool):
        """db_pool: OnlineData 库的连接池"""
        self.db_pool = db_pool

    async def _sync_qmf_source(self, conn, result: dict) -> tuple[int, str | None]:
        """兼容旧测试/内部调用；正式入口已迁移到 qmf_source 后台任务。"""
        parser = get_parser(MODEL_THREE_PARSER)
        rows = [parser.normalize_source_row(row) for row in (result.get("rows") or [])]
        if not rows:
            return 0, None
        column_map = await get_database_column_map(conn, parser.table_name, parser)
        source_fields = ("截止时间", "核查人", "姓名", "身份证号", "联系方式", "地址", "下发社区")
        quoted_columns = ", ".join(quote_identifier(column_map[column]) for column in parser.COLUMNS)
        update_parts = []
        for column in source_fields:
            identifier = quote_identifier(column_map[column])
            update_parts.append(
                f"{identifier}=IF(TRIM(COALESCE({identifier},''))='',VALUES({identifier}),{identifier})"
                if column == "核查人" else f"{identifier}=VALUES({identifier})"
            )
        await conn.begin()
        try:
            async with conn.cursor() as cur:
                for row in rows:
                    await cur.execute(
                        f"INSERT INTO {parser.table_name} (_row_key,{quoted_columns}) VALUES ({','.join(['%s'] * (len(parser.COLUMNS) + 1))}) "
                        f"ON DUPLICATE KEY UPDATE {','.join(update_parts)}",
                        (parser.make_row_key(row), *[row.get(column, '') for column in parser.COLUMNS]),
                    )
            report_date = await self._save_snapshot(conn, parser.table_name, MODEL_THREE_PARSER)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        return len(rows), report_date

    async def run_full_sync(self, task_id: int):
        """执行全量同步"""
        conn = await self.db_pool.acquire()
        client = None
        try:
            await self._set_status(conn, task_id, "running", "syncing")
            await self._set_current(conn, task_id, "读取同步配置")

            # 全民防模型三来源已拆为数据上传中心的独立后台任务；本流程
            # 只负责腾讯在线表同步。
            # 1. 获取 OAuth 凭据。
            creds = await self._get_oauth_creds(conn)
            if creds:
                client = TxDocsClient(
                    creds["client_id"],
                    creds["access_token"],
                    creds["open_id"],
                    usage_source="full_sync",
                )

            # 2. 获取启用的腾讯在线表格。
            spreadsheets = await self._get_spreadsheets(conn)
            if not spreadsheets:
                if not creds:
                    await self._fail(conn, task_id, "未配置OAuth凭据，请先在设置页配置腾讯文档OAuth")
                    return
                await self._fail(conn, task_id, "没有已配置且启用的在线表格")
                return

            # 3. 先完成全部在线数据和快照，不在单表中途生成日报。
            total = 0
            errors = []
            report_jobs: list[tuple[str, str]] = []
            sync_spreadsheets = spreadsheets
            if client is None:
                await self._fail(conn, task_id, "未配置OAuth凭据，请先在设置页配置腾讯文档OAuth")
                return

            for sp in sync_spreadsheets:
                await self._set_current(
                    conn,
                    task_id,
                    f"同步数据：{sp['name']}",
                )
                try:
                    count, report_date = await self._sync_one(conn, client, sp)
                    total += count
                    if report_date:
                        job = (report_date, sp["parser_type"])
                        if job not in report_jobs:
                            report_jobs.append(job)
                    await self._set_progress(conn, task_id, total)
                except Exception as e:
                    errors.append(f"{sp['name']}: {e}")
                finally:
                    await self._advance_step(conn, task_id)

            # 4. 数据阶段结束后，再统一生成本轮成功业务的分汇总表。
            report_dates = sorted({date for date, _ in report_jobs})
            await self._set_total_steps(
                conn,
                task_id,
                len(sync_spreadsheets)
                + (1 if qmf_source_enabled else 0)
                + len(report_jobs)
                + len(report_dates),
            )
            built_reports: dict[str, set[str]] = {
                date: set() for date in report_dates
            }
            if report_jobs:
                from services.report_builders import BUILDERS

                await self._set_phase(conn, task_id, "building_reports")
                for report_date, parser_type in report_jobs:
                    await self._set_current(
                        conn,
                        task_id,
                        f"生成分汇总：{parser_type}",
                    )
                    try:
                        builder = BUILDERS[parser_type]
                        result = await builder.build(report_date)
                        if result.get("implemented") is False:
                            raise RuntimeError(
                                result.get(
                                    "message",
                                    f"{parser_type}分汇总表生成失败",
                                )
                            )
                        built_reports[report_date].add(parser_type)
                        print(
                            f"[SYNC] 日报已刷新: "
                            f"{report_date} {parser_type}"
                        )
                    except Exception as e:
                        errors.append(
                            f"{report_date} {parser_type}分汇总表: {e}"
                        )
                    finally:
                        await self._advance_step(conn, task_id)

            # 5. 只有数据和全部分汇总都成功，才更新总汇总表。
            if report_dates:
                from services.report_builders.summary import (
                    _load_summary_types,
                    build_summary,
                )

                for report_date in sorted(report_dates):
                    await self._set_current(
                        conn,
                        task_id,
                        "生成总汇总表" if not errors else "总汇总表未更新",
                    )
                    try:
                        if errors:
                            print(
                                f"[SYNC] 本轮存在错误，未更新总汇总表: "
                                f"{report_date}"
                            )
                            continue

                        summary_types = await _load_summary_types()
                        missing_types = [
                            parser_type
                            for parser_type in summary_types
                            if parser_type not in built_reports[report_date]
                        ]
                        if missing_types:
                            raise RuntimeError(
                                "以下总汇总配置未在本轮成功生成："
                                + "、".join(missing_types)
                            )

                        result = await build_summary(
                            report_date,
                            summary_types=summary_types,
                        )
                        if not result.get("implemented"):
                            errors.append(
                                f"{report_date} 总汇总表: "
                                f"{result.get('message', '生成失败')}"
                            )
                        else:
                            print(f"[SYNC] 总汇总表已刷新: {report_date}")
                    except Exception as e:
                        errors.append(f"{report_date} 总汇总表: {e}")
                    finally:
                        await self._advance_step(conn, task_id)

            async with conn.cursor() as cur:
                await cleanup_expired_writeback_audit(cur)

            if errors:
                await self._complete_with_errors(conn, task_id, total, "\n".join(errors))
            else:
                await self._complete(conn, task_id, total)

        except Exception as e:
            await self._fail(conn, task_id, str(e))
        finally:
            if client:
                try:
                    await client.close()
                except Exception as e:
                    print(f"[SYNC] 关闭腾讯文档客户端失败: {e}")
            self.db_pool.release(conn)

    async def _get_oauth_creds(self, conn) -> dict | None:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT client_id, access_token, open_id FROM _config_oauth_tokens ORDER BY id DESC LIMIT 1"
            )
            row = await cur.fetchone()
            if row and row[1] and row[2]:
                return {"client_id": row[0], "access_token": row[1], "open_id": row[2]}
            return None

    async def _get_spreadsheets(self, conn) -> list[dict]:
        async with conn.cursor() as cur:
            await cur.execute(
                """SELECT id, name, url, file_id, data_sheet_id, header_row, parser_type
                   FROM _config_spreadsheets WHERE enabled = 1 AND url != '' AND file_id != ''
                   AND parser_type <> '疑似未注销模型三'"""
            )
            rows = await cur.fetchall()
        return [
            {"id": r[0], "name": r[1], "url": r[2], "file_id": r[3],
             "data_sheet_id": r[4], "header_row": r[5], "parser_type": r[6]}
            for r in rows
        ]

    async def _sync_one(self, conn, client, sp: dict) -> tuple[int, str | None]:
        async with conn.cursor() as cur:
            locked = await acquire_sheet_lock(cur, sp["id"], timeout=5)
        if not locked:
            raise RuntimeError("该腾讯表格正在被平台编辑，请稍后重新同步")
        try:
            return await self._sync_one_locked(conn, client, sp)
        finally:
            async with conn.cursor() as cur:
                await release_sheet_lock(cur, sp["id"])

    async def _sync_one_locked(
        self,
        conn,
        client,
        sp: dict,
    ) -> tuple[int, str | None]:
        """同步单个表格：增量比对 + 归档"""
        parser = get_parser(sp["parser_type"])
        table = parser.table_name
        cols = parser.COLUMNS
        source_columns = await resolve_source_columns(client, sp, parser)

        # 1. 从腾讯文档读取。来源突然清空或大幅骤降时，必须由第二次
        # 完整读取确认；确认前不能替换缓存、生成快照或归档旧数据。
        source_rows = await self._read_source_rows_safely(
            conn,
            client,
            sp,
            source_columns,
            parser,
        )
        source_rows = sorted(
            source_rows,
            key=lambda item: int(item.get("physical_row") or 0),
        )
        raw_rows = [parser.normalize_source_row(source["values"]) for source in source_rows]

        # 2. 解析 + 生成业务主键。业务明确允许时可合并同一对象的重复行。
        deduplicated = deduplicate_rows(
            parser,
            raw_rows,
            physical_rows=[int(source["physical_row"]) for source in source_rows],
        )
        online, duplicate_count = deduplicated
        for conflict in deduplicated.conflicts:
            print(
                f"[SYNC] {sp['name']}: 保留多来源冲突 "
                f"row_key={conflict.row_key[:12]} "
                f"physical_rows={','.join(map(str, conflict.physical_rows))} "
                f"fields={','.join(conflict.differing_columns)}"
            )

        # 读取数量和业务主键校验都通过后，才原子替换来源缓存。全链条
        # 冲突的全部物理行会在这里保留，并由投影继续标记来源异常。
        await replace_source_cache(conn, sp, source_rows)
        print(
            f"[SYNC] {sp['name']}: API返回{len(raw_rows)}行, "
            f"有效{len(online)}行, 去重或多来源{duplicate_count}行, "
            f"冲突对象{len(deduplicated.conflicts)}个"
        )

        # 3. 读取数据库现有数据
        column_map = await get_database_column_map(conn, table, parser)
        db_data = await self._load_existing(conn, table, parser, column_map)

        # 4. 三向比对
        online_keys = set(online.keys())
        db_keys = set(db_data.keys())
        new_keys = online_keys - db_keys
        removed_keys = db_keys - online_keys
        common_keys = online_keys & db_keys
        modified_keys = {k for k in common_keys if online[k] != db_data[k]}

        # 调试日志
        print(f"[SYNC] {sp['name']}: 在线{len(online)}条 数据库{len(db_data)}条 新增{len(new_keys)} 修改{len(modified_keys)} 移除{len(removed_keys)}")
        if modified_keys:
            sample_key = next(iter(modified_keys))
            diffs = {c: f"db='{db_data[sample_key].get(c,'')}' -> online='{online[sample_key].get(c,'')}'"
                     for c in parser.COLUMNS if db_data[sample_key].get(c,'') != online[sample_key].get(c,'')}
            print(f"[SYNC] 修改样例: {diffs}")

        # 5. 执行变更（每个操作独立 try-except，不中断整体同步）
        insert_ok = 0
        insert_fail = 0
        for key in new_keys:
            try:
                await self._insert(
                    conn, table, key, online[key], parser, column_map
                )
                insert_ok += 1
            except Exception as e:
                insert_fail += 1
                if insert_fail <= 3:
                    print(f"[SYNC] INSERT失败: {e} | key={key} | data={ {k: online[key].get(k,'')[:50] for k in parser.COLUMNS[:3]} }")
        if insert_fail > 0:
            print(f"[SYNC] INSERT统计: 成功{insert_ok} 失败{insert_fail}")

        update_ok = 0
        update_fail = 0
        for key in modified_keys:
            try:
                await self._update(
                    conn, table, key, online[key], parser, column_map
                )
                update_ok += 1
            except Exception as e:
                update_fail += 1
                if update_fail <= 2:
                    print(f"[SYNC] UPDATE失败: {e} | key={key}")
        if update_fail > 0:
            print(f"[SYNC] UPDATE统计: 成功{update_ok} 失败{update_fail}")

        if insert_fail or update_fail:
            raise RuntimeError(
                f"数据库写入不完整：新增失败{insert_fail}条，"
                f"更新失败{update_fail}条"
            )

        # ★ 保存快照（归档前，包含即将被移除的数据）
        report_date = await self._save_snapshot(conn, table, sp["parser_type"])

        if removed_keys:
            archive_table = f"OnlineDataArchive.{table}_archive"
            archive_column_map = await get_database_column_map(
                conn, archive_table, parser
            )
            for key in removed_keys:
                await self._archive(
                    conn,
                    table,
                    key,
                    db_data[key],
                    parser,
                    archive_column_map,
                )

        # ``rebuild_projection`` replaces one parser projection with a
        # delete-and-insert sequence.  Keep that sequence atomic: registration
        # performs a final source/projection check immediately before its first
        # external write and must never observe the temporary empty projection.
        await finalize_source_projection(
            conn,
            spreadsheet=sp,
            source_columns=source_columns,
        )

        return len(online), report_date

    async def _read_source_rows_safely(
        self,
        conn,
        client,
        sp: dict,
        source_columns: list[str],
        parser,
    ) -> list[dict]:
        previous_count = await self._load_cached_source_count(conn, int(sp["id"]))
        first = await client.read_all_source_rows(
            sp["file_id"],
            sp["data_sheet_id"],
            sp["header_row"],
            source_columns,
        )
        if not source_read_requires_confirmation(previous_count, len(first)):
            return first

        second = await client.read_all_source_rows(
            sp["file_id"],
            sp["data_sheet_id"],
            sp["header_row"],
            source_columns,
        )
        if (
            len(first) != len(second)
            or source_rows_digest(parser, first) != source_rows_digest(parser, second)
        ):
            raise RuntimeError(
                "腾讯来源读取结果异常波动，已停止本表同步且未修改缓存或归档："
                f"原有{previous_count}行，首次{len(first)}行，复核{len(second)}行"
            )

        print(
            f"[SYNC] {sp['name']}: 来源数量显著变化已通过二次完整读取确认 "
            f"previous={previous_count} current={len(second)}"
        )
        return second

    async def _load_cached_source_count(self, conn, spreadsheet_id: int) -> int:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT row_count FROM _online_source_cache_state "
                "WHERE spreadsheet_id=%s",
                (spreadsheet_id,),
            )
            row = await cur.fetchone()
            if row is not None:
                return int(row[0] or 0)
            await cur.execute(
                "SELECT COUNT(*) FROM _online_source_rows WHERE spreadsheet_id=%s",
                (spreadsheet_id,),
            )
            row = await cur.fetchone()
            return int(row[0] or 0) if row else 0

    async def _load_existing(
        self, conn, table: str, parser, column_map: dict[str, str]
    ) -> dict[str, dict]:
        """加载现有数据，返回 _row_key → {列名: 值} 字典"""
        col_list = ", ".join(
            quote_identifier(column_map[column]) for column in parser.COLUMNS
        )
        async with conn.cursor() as cur:
            await cur.execute(f"SELECT _row_key, {col_list} FROM {table}")
            rows = await cur.fetchall()
        result = {}
        for row in rows:
            key = row[0]
            vals = row[1:]
            result[key] = {c: str(vals[i]).strip() if vals[i] is not None else "" for i, c in enumerate(parser.COLUMNS)}
        return result

    async def _insert(
        self,
        conn,
        table: str,
        key: str,
        data: dict,
        parser,
        column_map: dict[str, str],
    ):
        col_list = ", ".join(
            quote_identifier(column_map[column]) for column in parser.COLUMNS
        )
        placeholders = ", ".join(["%s"] * (len(parser.COLUMNS) + 1))
        values = [key] + [data.get(c, "") for c in parser.COLUMNS]
        async with conn.cursor() as cur:
            await cur.execute(
                f"INSERT INTO {table} (_row_key, {col_list}) VALUES ({placeholders})",
                values,
            )

    async def _update(
        self,
        conn,
        table: str,
        key: str,
        data: dict,
        parser,
        column_map: dict[str, str],
    ):
        set_clause = ", ".join(
            f"{quote_identifier(column_map[column])} = %s"
            for column in parser.COLUMNS
        )
        values = [data.get(c, "") for c in parser.COLUMNS] + [key]
        async with conn.cursor() as cur:
            await cur.execute(
                f"UPDATE {table} SET {set_clause} WHERE _row_key = %s",
                values,
            )

    async def _archive(
        self,
        conn,
        table: str,
        key: str,
        data: dict,
        parser,
        column_map: dict[str, str],
    ):
        """归档：INSERT 到 OnlineDataArchive + DELETE 原表"""
        archive_table = f"{table}_archive"
        col_list = ", ".join(
            quote_identifier(column_map[column]) for column in parser.COLUMNS
        )
        placeholders = ", ".join(["%s"] * (len(parser.COLUMNS) + 1))
        values = [key] + [data.get(c, "") for c in parser.COLUMNS]
        async with conn.cursor() as cur:
            await cur.execute(
                f"INSERT INTO OnlineDataArchive.{archive_table} (_row_key, {col_list}) VALUES ({placeholders})",
                values,
            )
            await cur.execute(f"DELETE FROM {table} WHERE _row_key = %s", (key,))

    async def _save_snapshot(
        self, conn, table: str, parser_type: str
    ) -> str | None:
        """保存快照，返回使用的业务日期；日报在全部数据同步后统一生成。"""
        from services.report_builders import BUILDERS

        builder = BUILDERS.get(parser_type)
        if not builder:
            return None
        async with conn.cursor() as cur:
            today = (await get_business_date(cur)).isoformat()
            snapshot_table = f"{today}_snapshot_{builder.table_suffix}"
            if await table_exists(cur, "daily_report", snapshot_table):
                await cur.execute(f"DROP TABLE daily_report.`{snapshot_table}`")
            await cur.execute(
                f"CREATE TABLE daily_report.`{snapshot_table}` AS SELECT * FROM OnlineData.`{table}`"
            )
            await cur.execute(
                "INSERT INTO daily_report._daily_report_meta (table_name, report_date, parser_type, generation_method) "
                "VALUES (%s, %s, %s, 'snapshot') ON DUPLICATE KEY UPDATE generated_at = NOW()",
                (snapshot_table, today, f"{parser_type}_snapshot"),
            )
            print(f"[SYNC] 快照已保存: {snapshot_table}")

        return today

    # --- 任务状态管理 ---
    async def _set_status(
        self,
        conn,
        task_id: int,
        status: str,
        phase: str,
    ):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _sync_log SET status=%s, phase=%s, "
                "started_at=UTC_TIMESTAMP() WHERE id=%s",
                (status, phase, task_id),
            )

    async def _set_phase(self, conn, task_id: int, phase: str):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _sync_log SET phase=%s WHERE id=%s",
                (phase, task_id),
            )

    async def _set_current(self, conn, task_id: int, current_item: str):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _sync_log SET current_item=%s WHERE id=%s",
                (current_item, task_id),
            )

    async def _advance_step(self, conn, task_id: int):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _sync_log "
                "SET completed_steps=completed_steps+1 WHERE id=%s",
                (task_id,),
            )

    async def _set_total_steps(self, conn, task_id: int, total_steps: int):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _sync_log SET total_steps=%s WHERE id=%s",
                (total_steps, task_id),
            )

    async def _set_progress(self, conn, task_id: int, processed: int):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _sync_log SET processed_rows=%s WHERE id=%s",
                (processed, task_id),
            )

    async def _complete(self, conn, task_id: int, total: int):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _sync_log SET status='success', phase='finished', "
                "current_item=NULL, total_rows=%s, processed_rows=%s, "
                "completed_steps=total_steps, finished_at=UTC_TIMESTAMP() "
                "WHERE id=%s",
                (total, total, task_id),
            )

    async def _complete_with_errors(self, conn, task_id: int, total: int, errors: str):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _sync_log SET status='partial', phase='finished', "
                "current_item=NULL, total_rows=%s, processed_rows=%s, "
                "error_message=%s, finished_at=UTC_TIMESTAMP() WHERE id=%s",
                (total, total, errors[:1000], task_id),
            )

    async def _fail(self, conn, task_id: int, error: str):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _sync_log SET status='failed', phase='finished', "
                "current_item=NULL, error_message=%s, "
                "finished_at=UTC_TIMESTAMP() WHERE id=%s",
                (error[:1000], task_id),
            )
