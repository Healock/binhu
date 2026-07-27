"""数据同步引擎 - 增量比对 + 归档

流程：从腾讯文档读取 → 解析器解析 → 和数据库现有数据比对 → 新增/更新/归档移除的
"""

from services.txdocs_client import TxDocsClient
from services.parsers import get_parser
from services.business_time import get_business_date
from services.schema_compat import get_database_column_map, quote_identifier


class SyncEngine:
    def __init__(self, db_pool):
        """db_pool: OnlineData 库的连接池"""
        self.db_pool = db_pool

    async def run_full_sync(self, task_id: int):
        """执行全量同步"""
        conn = await self.db_pool.acquire()
        try:
            await self._set_status(conn, task_id, "running")

            # 1. 获取 OAuth 凭据
            creds = await self._get_oauth_creds(conn)
            if not creds:
                await self._fail(conn, task_id, "未配置OAuth凭据，请先在设置页配置腾讯文档OAuth")
                return

            client = TxDocsClient(creds["client_id"], creds["access_token"], creds["open_id"])

            # 2. 获取启用的表格
            spreadsheets = await self._get_spreadsheets(conn)
            if not spreadsheets:
                await self._fail(conn, task_id, "没有已配置且启用的在线表格")
                return

            # 3. 逐表同步
            total = 0
            errors = []
            for sp in spreadsheets:
                try:
                    count = await self._sync_one(conn, client, sp)
                    total += count
                    await self._set_progress(conn, task_id, total)
                except Exception as e:
                    errors.append(f"{sp['name']}: {e}")

            await client.close()

            if errors:
                await self._complete_with_errors(conn, task_id, total, "\n".join(errors))
            else:
                await self._complete(conn, task_id, total)

        except Exception as e:
            await self._fail(conn, task_id, str(e))
        finally:
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
                   FROM _config_spreadsheets WHERE enabled = 1 AND url != '' AND file_id != ''"""
            )
            rows = await cur.fetchall()
        return [
            {"id": r[0], "name": r[1], "url": r[2], "file_id": r[3],
             "data_sheet_id": r[4], "header_row": r[5], "parser_type": r[6]}
            for r in rows
        ]

    async def _sync_one(self, conn, client, sp: dict) -> int:
        """同步单个表格：增量比对 + 归档"""
        parser = get_parser(sp["parser_type"])
        table = parser.table_name
        cols = parser.COLUMNS

        # 1. 从腾讯文档读取
        raw_rows = await client.read_all_data(
            sp["file_id"], sp["data_sheet_id"], sp["header_row"], cols
        )

        # 2. 解析 + 生成业务主键（直接用 dict，跳过 parse_row 的位置映射避免顺序问题）
        online: dict[str, dict] = {}
        dup_count = 0
        for raw in raw_rows:
            parsed = {k: str(v).strip() if v else "" for k, v in raw.items()}
            key = parser.make_row_key(parsed)
            if key in online:
                dup_count += 1
                # 打印主键冲突详情
                bk = parser.get_business_key()
                print(f"[SYNC] 主键冲突! key={key} 旧:{[online[key].get(k,'') for k in bk]} 新:{[parsed.get(k,'') for k in bk]}")
            online[key] = parsed
        print(f"[SYNC] {sp['name']}: API返回{len(raw_rows)}行, 去重后{len(online)}行, 重复{dup_count}行")

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

        # ★ 保存快照（归档前，包含即将被移除的数据）
        await self._save_snapshot(conn, table, sp["parser_type"])

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

        return len(online)

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

    async def _save_snapshot(self, conn, table: str, parser_type: str):
        """保存原始表全量快照到 daily_report 库（归档前调用，包含即将移除的数据）"""
        from services.report_builders import BUILDERS
        builder = BUILDERS.get(parser_type)
        if not builder:
            return
        async with conn.cursor() as cur:
            today = (await get_business_date(cur)).isoformat()
            snapshot_table = f"{today}_snapshot_{builder.table_suffix}"
            await cur.execute(f"DROP TABLE IF EXISTS daily_report.`{snapshot_table}`")
            await cur.execute(
                f"CREATE TABLE daily_report.`{snapshot_table}` AS SELECT * FROM OnlineData.`{table}`"
            )
            await cur.execute(
                "INSERT INTO daily_report._daily_report_meta (table_name, report_date, parser_type, generation_method) "
                "VALUES (%s, %s, %s, 'snapshot') ON DUPLICATE KEY UPDATE generated_at = NOW()",
                (snapshot_table, today, f"{parser_type}_snapshot"),
            )
            print(f"[SYNC] 快照已保存: {snapshot_table}")

    # --- 任务状态管理 ---
    async def _set_status(self, conn, task_id: int, status: str):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _sync_log SET status=%s, started_at=NOW() WHERE id=%s",
                (status, task_id),
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
                "UPDATE _sync_log SET status='success', total_rows=%s, processed_rows=%s, finished_at=NOW() WHERE id=%s",
                (total, total, task_id),
            )

    async def _complete_with_errors(self, conn, task_id: int, total: int, errors: str):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _sync_log SET status='partial', total_rows=%s, processed_rows=%s, error_message=%s, finished_at=NOW() WHERE id=%s",
                (total, total, errors[:1000], task_id),
            )

    async def _fail(self, conn, task_id: int, error: str):
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE _sync_log SET status='failed', error_message=%s, finished_at=NOW() WHERE id=%s",
                (error[:1000], task_id),
            )
