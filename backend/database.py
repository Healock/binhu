"""MySQL 多数据库连接池管理（OnlineData / OnlineDataArchive / daily_report）"""

import aiomysql
from config import settings

# 数据库名称映射
DB_NAMES = {
    "online_data": settings.MYSQL_ONLINE_DATA_DB,
    "archive": settings.MYSQL_ARCHIVE_DB,
    "daily_report": settings.MYSQL_DAILY_REPORT_DB,
}


async def ensure_bootstrap_admin(cur) -> bool:
    """Create the first super administrator only from explicit environment values."""
    await cur.execute("SELECT COUNT(*) FROM _users")
    if (await cur.fetchone())[0] > 0:
        return False

    username = settings.BOOTSTRAP_ADMIN_USERNAME.strip()
    password = settings.BOOTSTRAP_ADMIN_PASSWORD
    if not username or not password:
        raise RuntimeError(
            "用户表为空。请临时设置 BOOTSTRAP_ADMIN_USERNAME 和 "
            "BOOTSTRAP_ADMIN_PASSWORD 创建首个超级管理员。"
        )

    import bcrypt

    password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    await cur.execute(
        "INSERT INTO _users (username, password_hash, role) "
        "VALUES (%s, %s, 'super_admin')",
        (username, password_hash),
    )
    print(f"[DB] 初始超级管理员已创建: {username}")
    return True


class DatabaseManager:
    """管理三个数据库的连接池"""
    _pools: dict[str, aiomysql.Pool] = {}

    @classmethod
    async def init_all(cls):
        """创建三个数据库的连接池"""
        for key, db_name in DB_NAMES.items():
            cls._pools[key] = await aiomysql.create_pool(
                host=settings.MYSQL_HOST,
                port=settings.MYSQL_PORT,
                user=settings.MYSQL_USER,
                password=settings.MYSQL_PASSWORD,
                db=db_name,
                minsize=2,
                maxsize=settings.MYSQL_POOL_SIZE,
                charset="utf8mb4",
                autocommit=True,
            )
        # 确保新表存在（不需要删 volume 重建）
        async with cls._pools["online_data"].acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _grid_members (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        name VARCHAR(100) NOT NULL,
                        community VARCHAR(200) DEFAULT '',
                        position VARCHAR(20) NOT NULL DEFAULT '组员',
                        phone VARCHAR(50) DEFAULT '',
                        notes VARCHAR(500) DEFAULT '',
                        status VARCHAR(10) NOT NULL DEFAULT '在岗',
                        leave_start_date DATE DEFAULT NULL,
                        leave_end_date DATE DEFAULT NULL,
                        leave_reason VARCHAR(200) DEFAULT '',
                        leave_source VARCHAR(30) NOT NULL DEFAULT 'manual',
                        id_card_number VARCHAR(50) DEFAULT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_name (name),
                        UNIQUE KEY uk_grid_id_card (id_card_number)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _communities (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        name VARCHAR(200) NOT NULL UNIQUE,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _community_aliases (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        community_id INT NOT NULL,
                        alias VARCHAR(200) NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_community_alias (alias),
                        INDEX idx_community_alias_owner (community_id),
                        CONSTRAINT fk_community_alias_owner
                            FOREIGN KEY (community_id)
                            REFERENCES _communities(id)
                            ON DELETE CASCADE
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _system_config (
                        config_key VARCHAR(100) PRIMARY KEY,
                        config_value TEXT
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute(
                    "INSERT IGNORE INTO _system_config (config_key, config_value) VALUES ('timezone', 'Asia/Shanghai')"
                )
                await cur.execute(
                    "INSERT IGNORE INTO _system_config "
                    "(config_key, config_value) VALUES "
                    "('online_summary_positions', '[\"组长\", \"组员\"]'), "
                    "('visit_summary_positions', '[\"组长\", \"组员\"]')"
                )
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _sync_schedule (
                        id TINYINT NOT NULL PRIMARY KEY,
                        enabled TINYINT(1) NOT NULL DEFAULT 1,
                        interval_minutes INT NOT NULL DEFAULT 5,
                        next_run_at DATETIME DEFAULT NULL,
                        last_triggered_at DATETIME DEFAULT NULL,
                        updated_by INT DEFAULT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute(
                    "INSERT IGNORE INTO _sync_schedule "
                    "(id, enabled, interval_minutes, next_run_at) "
                    "VALUES (1, 1, 5, DATE_ADD(UTC_TIMESTAMP(), INTERVAL 5 MINUTE))"
                )
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _notifications (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT NOT NULL,
                        category VARCHAR(30) NOT NULL DEFAULT 'sync',
                        severity VARCHAR(20) NOT NULL DEFAULT 'error',
                        title VARCHAR(100) NOT NULL,
                        content TEXT NOT NULL,
                        related_task_id INT DEFAULT NULL,
                        is_read TINYINT(1) NOT NULL DEFAULT 0,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        read_at DATETIME DEFAULT NULL,
                        UNIQUE KEY uk_notification_user_task (
                            user_id, category, related_task_id
                        ),
                        INDEX idx_notification_unread (
                            user_id, is_read, created_at
                        )
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _backup_schedule (
                        id TINYINT NOT NULL PRIMARY KEY,
                        enabled TINYINT(1) NOT NULL DEFAULT 1,
                        run_hour TINYINT NOT NULL DEFAULT 2,
                        run_minute TINYINT NOT NULL DEFAULT 0,
                        retention_days INT NOT NULL DEFAULT 7,
                        next_run_at DATETIME DEFAULT NULL,
                        last_triggered_at DATETIME DEFAULT NULL,
                        updated_by INT DEFAULT NULL,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute(
                    "INSERT IGNORE INTO _backup_schedule "
                    "(id, enabled, run_hour, run_minute, retention_days) "
                    "VALUES (1, 1, 2, 0, 7)"
                )
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _backup_jobs (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        trigger_source VARCHAR(20) NOT NULL DEFAULT 'manual',
                        status VARCHAR(20) NOT NULL DEFAULT 'pending',
                        requested_by INT DEFAULT NULL,
                        filename VARCHAR(255) DEFAULT NULL,
                        size_bytes BIGINT DEFAULT NULL,
                        sha256 CHAR(64) DEFAULT NULL,
                        error_message TEXT DEFAULT NULL,
                        started_at DATETIME DEFAULT NULL,
                        finished_at DATETIME DEFAULT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_backup_status (status),
                        INDEX idx_backup_created (created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _admin_audit_log (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT DEFAULT NULL,
                        username VARCHAR(50) NOT NULL DEFAULT '',
                        action VARCHAR(80) NOT NULL,
                        target_type VARCHAR(50) NOT NULL DEFAULT '',
                        target_name VARCHAR(200) NOT NULL DEFAULT '',
                        result VARCHAR(20) NOT NULL DEFAULT 'success',
                        detail_json JSON DEFAULT NULL,
                        ip_address VARCHAR(45) NOT NULL DEFAULT '',
                        user_agent VARCHAR(300) NOT NULL DEFAULT '',
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_audit_user_time (user_id, created_at),
                        INDEX idx_audit_action_time (action, created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _visit_import_batches (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        import_type VARCHAR(20) NOT NULL DEFAULT 'detail',
                        filename VARCHAR(255) NOT NULL,
                        file_sha256 CHAR(64) NOT NULL,
                        file_size_bytes BIGINT NOT NULL DEFAULT 0,
                        status VARCHAR(20) NOT NULL DEFAULT 'running',
                        uploader_id INT DEFAULT NULL,
                        sheet_name VARCHAR(100) DEFAULT NULL,
                        total_rows INT NOT NULL DEFAULT 0,
                        valid_rows INT NOT NULL DEFAULT 0,
                        inserted_rows INT NOT NULL DEFAULT 0,
                        updated_rows INT NOT NULL DEFAULT 0,
                        unchanged_rows INT NOT NULL DEFAULT 0,
                        ignored_rows INT NOT NULL DEFAULT 0,
                        error_count INT NOT NULL DEFAULT 0,
                        warning_count INT NOT NULL DEFAULT 0,
                        file_start_date DATE DEFAULT NULL,
                        file_end_date DATE DEFAULT NULL,
                        overlap_start_date DATE DEFAULT NULL,
                        overlap_end_date DATE DEFAULT NULL,
                        error_message TEXT DEFAULT NULL,
                        started_at DATETIME DEFAULT NULL,
                        finished_at DATETIME DEFAULT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_visit_batch_hash (file_sha256),
                        INDEX idx_visit_batch_type_hash (
                            import_type, file_sha256, status
                        ),
                        INDEX idx_visit_batch_status (status),
                        INDEX idx_visit_batch_created (created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS t_visit_details (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        _row_key CHAR(64) NOT NULL,
                        派出所名称 VARCHAR(200) DEFAULT '',
                        村社区 VARCHAR(200) NOT NULL,
                        社区 VARCHAR(200) NOT NULL,
                        进入方式 VARCHAR(20) NOT NULL,
                        地址 VARCHAR(1000) NOT NULL,
                        _normalized_address VARCHAR(1000) NOT NULL,
                        _address_key CHAR(64) NOT NULL,
                        操作人 VARCHAR(100) NOT NULL,
                        操作人账号 VARCHAR(50) DEFAULT '',
                        入户时间 DATETIME NOT NULL,
                        业务日期 DATE NOT NULL,
                        _raw_visit_time VARCHAR(100) DEFAULT '',
                        房间核查数量 INT UNSIGNED NOT NULL DEFAULT 0,
                        新增 INT UNSIGNED NOT NULL DEFAULT 0,
                        变更 INT UNSIGNED NOT NULL DEFAULT 0,
                        注销 INT UNSIGNED NOT NULL DEFAULT 0,
                        星级派出所名称 VARCHAR(200) DEFAULT NULL,
                        星级所属社区 VARCHAR(200) DEFAULT NULL,
                        星级社区 VARCHAR(200) DEFAULT NULL,
                        星级地址 VARCHAR(1000) DEFAULT NULL,
                        得分 DECIMAL(18, 6) DEFAULT NULL,
                        星级 VARCHAR(50) DEFAULT NULL,
                        星级采集时间 DATETIME DEFAULT NULL,
                        星级采集日期 DATE DEFAULT NULL,
                        _raw_star_time VARCHAR(100) DEFAULT NULL,
                        隐患详情 MEDIUMTEXT DEFAULT NULL,
                        星级时间差秒 INT UNSIGNED DEFAULT NULL,
                        star_import_batch_id BIGINT DEFAULT NULL,
                        star_source_row_number INT DEFAULT NULL,
                        import_batch_id BIGINT NOT NULL,
                        source_row_number INT NOT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_visit_row_key (_row_key),
                        INDEX idx_visit_date (业务日期),
                        INDEX idx_visit_community_date (社区, 业务日期),
                        INDEX idx_visit_operator_date (操作人, 业务日期),
                        INDEX idx_visit_address_date (_address_key, 业务日期),
                        INDEX idx_visit_batch (import_batch_id),
                        INDEX idx_visit_star_time (星级采集时间),
                        INDEX idx_visit_star_batch (star_import_batch_id)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute(
                    "SHOW COLUMNS FROM _visit_import_batches "
                    "LIKE 'import_type'"
                )
                if not await cur.fetchone():
                    await cur.execute(
                        "ALTER TABLE _visit_import_batches "
                        "ADD COLUMN import_type VARCHAR(20) NOT NULL "
                        "DEFAULT 'detail' AFTER id"
                    )
                await cur.execute(
                    "SHOW INDEX FROM _visit_import_batches "
                    "WHERE Key_name='idx_visit_batch_type_hash'"
                )
                if not await cur.fetchone():
                    await cur.execute(
                        "CREATE INDEX idx_visit_batch_type_hash "
                        "ON _visit_import_batches "
                        "(import_type, file_sha256, status)"
                    )
                visit_star_columns = [
                    ("星级派出所名称", "VARCHAR(200) DEFAULT NULL"),
                    ("星级所属社区", "VARCHAR(200) DEFAULT NULL"),
                    ("星级社区", "VARCHAR(200) DEFAULT NULL"),
                    ("星级地址", "VARCHAR(1000) DEFAULT NULL"),
                    ("得分", "DECIMAL(18, 6) DEFAULT NULL"),
                    ("星级", "VARCHAR(50) DEFAULT NULL"),
                    ("星级采集时间", "DATETIME DEFAULT NULL"),
                    ("星级采集日期", "DATE DEFAULT NULL"),
                    ("_raw_star_time", "VARCHAR(100) DEFAULT NULL"),
                    ("隐患详情", "MEDIUMTEXT DEFAULT NULL"),
                    ("星级时间差秒", "INT UNSIGNED DEFAULT NULL"),
                    ("star_import_batch_id", "BIGINT DEFAULT NULL"),
                    ("star_source_row_number", "INT DEFAULT NULL"),
                ]
                for column_name, column_definition in visit_star_columns:
                    await cur.execute(
                        "SHOW COLUMNS FROM t_visit_details LIKE %s",
                        (column_name,),
                    )
                    if not await cur.fetchone():
                        await cur.execute(
                            f"ALTER TABLE t_visit_details "
                            f"ADD COLUMN `{column_name}` {column_definition}"
                        )
                for index_name, index_sql in [
                    (
                        "idx_visit_star_time",
                        "CREATE INDEX idx_visit_star_time "
                        "ON t_visit_details (`星级采集时间`)",
                    ),
                    (
                        "idx_visit_star_batch",
                        "CREATE INDEX idx_visit_star_batch "
                        "ON t_visit_details (star_import_batch_id)",
                    ),
                ]:
                    await cur.execute(
                        "SHOW INDEX FROM t_visit_details "
                        "WHERE Key_name=%s",
                        (index_name,),
                    )
                    if not await cur.fetchone():
                        await cur.execute(index_sql)
                # 兼容已经导入的数据：正式匹配值去掉“社区”或“村”后缀，
                # 原始“村社区”字段保持不变。
                await cur.execute("""
                    UPDATE t_visit_details
                    SET 社区 = CASE
                        WHEN RIGHT(TRIM(村社区), 2) = '社区'
                            THEN TRIM(LEFT(
                                TRIM(村社区),
                                CHAR_LENGTH(TRIM(村社区)) - 2
                            ))
                        WHEN RIGHT(TRIM(村社区), 1) = '村'
                            THEN TRIM(LEFT(
                                TRIM(村社区),
                                CHAR_LENGTH(TRIM(村社区)) - 1
                            ))
                        ELSE TRIM(村社区)
                    END
                    WHERE 村社区 IS NOT NULL
                """)
                # 去重主键包含网格员姓名：同日同地址由不同网格员走访时，
                # 每名网格员各保留一条。
                await cur.execute("""
                    UPDATE t_visit_details
                    SET _row_key = LOWER(SHA2(CONCAT(
                        DATE_FORMAT(业务日期, '%Y-%m-%d'),
                        '|',
                        _normalized_address,
                        '|',
                        TRIM(操作人)
                    ), 256))
                """)
                await cur.execute("""
                    UPDATE t_visit_details AS v
                    JOIN _community_aliases AS a
                      ON a.alias = v.社区
                    JOIN _communities AS c
                      ON c.id = a.community_id
                    SET v.社区 = c.name
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _visit_import_issues (
                        id BIGINT AUTO_INCREMENT PRIMARY KEY,
                        batch_id BIGINT NOT NULL,
                        severity VARCHAR(20) NOT NULL,
                        code VARCHAR(60) NOT NULL,
                        source_row_number INT NOT NULL DEFAULT 0,
                        message VARCHAR(500) NOT NULL,
                        row_preview JSON DEFAULT NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_visit_issue_batch (
                            batch_id, severity, source_row_number
                        )
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                for column_name, column_definition in [
                    (
                        "trigger_source",
                        "VARCHAR(20) NOT NULL DEFAULT 'manual'",
                    ),
                    ("requested_by", "INT DEFAULT NULL"),
                    ("phase", "VARCHAR(30) NOT NULL DEFAULT 'queued'"),
                    ("current_item", "VARCHAR(200) DEFAULT NULL"),
                    ("total_steps", "INT NOT NULL DEFAULT 0"),
                    ("completed_steps", "INT NOT NULL DEFAULT 0"),
                ]:
                    await cur.execute(
                        "SHOW COLUMNS FROM _sync_log LIKE %s",
                        (column_name,),
                    )
                    if not await cur.fetchone():
                        await cur.execute(
                            f"ALTER TABLE _sync_log "
                            f"ADD COLUMN `{column_name}` {column_definition}"
                        )
                await cur.execute(
                    "UPDATE _sync_log SET phase='finished' "
                    "WHERE status IN ('success', 'completed', 'partial', 'failed') "
                    "AND phase='queued'"
                )
                # 疑似返苏表列名修正（身份证号→身份证号码，二次反馈→二次核查结果）
                for old_name, new_name, col_type in [
                    ("身份证号", "身份证号码", "VARCHAR(50)"),
                    ("二次反馈", "二次核查结果", "VARCHAR(500)"),
                ]:
                    try:
                        await cur.execute(
                            f"ALTER TABLE t_suspect_return CHANGE COLUMN `{old_name}` `{new_name}` {col_type}"
                        )
                    except Exception:
                        pass  # 列名已改过或表不存在
                # 电话号码列扩展为 VARCHAR(500)（多选号码拼接后可能超过50字符）
                for table, col in [
                    ("t_fullchain", "电话号码"),
                    ("t_rental_check", "手机号码"),
                    ("t_delivery_industry", "手机号码"),
                    ("t_suspect_unrevoked", "联系方式"),
                    ("t_suspect_return", "联系号码"),
                    ("t_suspect_return", "身份证号码"),
                ]:
                    try:
                        await cur.execute(f"ALTER TABLE {table} MODIFY COLUMN `{col}` VARCHAR(500)")
                    except Exception:
                        pass
                # 旧数据库平滑补齐网格员状态和请假字段
                for column_name, column_definition in [
                    ("position", "VARCHAR(20) NOT NULL DEFAULT '组员'"),
                    ("status", "VARCHAR(10) NOT NULL DEFAULT '在岗'"),
                    ("leave_start_date", "DATE DEFAULT NULL"),
                    ("leave_end_date", "DATE DEFAULT NULL"),
                    ("leave_reason", "VARCHAR(200) DEFAULT ''"),
                    ("leave_source", "VARCHAR(30) NOT NULL DEFAULT 'manual'"),
                    ("id_card_number", "VARCHAR(50) DEFAULT NULL"),
                ]:
                    await cur.execute(
                        "SHOW COLUMNS FROM _grid_members LIKE %s", (column_name,)
                    )
                    if not await cur.fetchone():
                        await cur.execute(
                            f"ALTER TABLE _grid_members "
                            f"ADD COLUMN `{column_name}` {column_definition}"
                        )
                await cur.execute(
                    "SHOW INDEX FROM _grid_members "
                    "WHERE Key_name='uk_grid_id_card'"
                )
                if not await cur.fetchone():
                    await cur.execute(
                        "ALTER TABLE _grid_members "
                        "ADD UNIQUE KEY uk_grid_id_card (id_card_number)"
                    )
                await cur.execute(
                    "SHOW INDEX FROM _grid_members "
                    "WHERE Key_name='idx_grid_position'"
                )
                if not await cur.fetchone():
                    await cur.execute(
                        "ALTER TABLE _grid_members "
                        "ADD INDEX idx_grid_position (position)"
                    )
                # 测试数据表（用于验证工作量统计逻辑）
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS t_test_mock (
                        _row_key VARCHAR(200) NOT NULL,
                        社区 VARCHAR(100) DEFAULT '',
                        核查人 VARCHAR(100) DEFAULT '',
                        姓名 VARCHAR(100) DEFAULT '',
                        现住址 VARCHAR(500) DEFAULT '',
                        核查结果 VARCHAR(500) DEFAULT '',
                        下发日期 VARCHAR(50) DEFAULT '',
                        截止日期 VARCHAR(50) DEFAULT '',
                        _first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        _last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uk_row_key (_row_key)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                # 用户表
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _users (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        username VARCHAR(50) NOT NULL UNIQUE,
                        password_hash VARCHAR(255) NOT NULL,
                        role ENUM('super_admin','admin','leader','member') NOT NULL DEFAULT 'member',
                        table_display_mode VARCHAR(10) NOT NULL DEFAULT 'table',
                        report_column_mode VARCHAR(10) NOT NULL DEFAULT 'three',
                        mobile_navigation_mode VARCHAR(10) NOT NULL DEFAULT 'dock',
                        mobile_dock_config JSON DEFAULT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                # 旧用户表平滑补齐账号级个性化设置
                for column_name, column_definition in [
                    (
                        "table_display_mode",
                        "VARCHAR(10) NOT NULL DEFAULT 'table'",
                    ),
                    (
                        "report_column_mode",
                        "VARCHAR(10) NOT NULL DEFAULT 'three'",
                    ),
                    (
                        "mobile_navigation_mode",
                        "VARCHAR(10) NOT NULL DEFAULT 'dock'",
                    ),
                    (
                        "mobile_dock_config",
                        "JSON DEFAULT NULL",
                    ),
                ]:
                    await cur.execute(
                        "SHOW COLUMNS FROM _users LIKE %s",
                        (column_name,),
                    )
                    if not await cur.fetchone():
                        await cur.execute(
                            f"ALTER TABLE _users "
                            f"ADD COLUMN `{column_name}` {column_definition}"
                        )
                # Session 表
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _sessions (
                        session_id VARCHAR(64) PRIMARY KEY,
                        user_id INT NOT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        expires_at DATETIME NOT NULL,
                        INDEX idx_user (user_id),
                        INDEX idx_expires (expires_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                """)
                await ensure_bootstrap_admin(cur)

        async with cls._pools["daily_report"].acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _daily_task_ledger (
                        report_date DATE NOT NULL,
                        parser_type VARCHAR(50) NOT NULL,
                        row_key VARCHAR(200) NOT NULL,
                        source VARCHAR(20) NOT NULL,
                        included TINYINT(1) NOT NULL DEFAULT 1,
                        online_present TINYINT(1) NOT NULL DEFAULT 1,
                        community VARCHAR(200) DEFAULT '',
                        inspector VARCHAR(100) DEFAULT '',
                        task_state VARCHAR(20) NOT NULL,
                        unable_to_verify TINYINT(1) NOT NULL DEFAULT 0,
                        reached_bottom TINYINT(1) NOT NULL DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                            ON UPDATE CURRENT_TIMESTAMP,
                        PRIMARY KEY (report_date, parser_type, row_key),
                        INDEX idx_ledger_type_date (parser_type, report_date),
                        INDEX idx_ledger_person (report_date, community, inspector)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
                await cur.execute("""
                    CREATE TABLE IF NOT EXISTS _daily_task_ledger_runs (
                        report_date DATE NOT NULL,
                        parser_type VARCHAR(50) NOT NULL,
                        snapshot_table VARCHAR(100) NOT NULL,
                        previous_snapshot_table VARCHAR(100) DEFAULT NULL,
                        ledger_rows INT NOT NULL DEFAULT 0,
                        included_rows INT NOT NULL DEFAULT 0,
                        generation_method VARCHAR(20) NOT NULL DEFAULT 'sync',
                        generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (report_date, parser_type),
                        INDEX idx_ledger_run_date (report_date)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                      COLLATE=utf8mb4_unicode_ci
                """)
        return cls

    @classmethod
    async def close_all(cls):
        """关闭所有连接池"""
        for pool in cls._pools.values():
            pool.close()
            await pool.wait_closed()
        cls._pools.clear()

    @classmethod
    def get_pool(cls, name: str = "online_data") -> aiomysql.Pool:
        """获取指定数据库的连接池"""
        if name not in cls._pools:
            raise ValueError(f"未知的数据库: {name}，可用: {list(cls._pools.keys())}")
        return cls._pools[name]


# 全局实例
db_manager = DatabaseManager()

# 兼容旧代码
async def init_db():
    await db_manager.init_all()

async def close_db():
    await db_manager.close_all()


# 依赖注入：默认 online_data 库（兼容现有 Depends(get_db)）
async def get_db():
    """默认获取 OnlineData 库的连接"""
    async with db_manager.get_pool("online_data").acquire() as conn:
        yield conn


def get_db_pool(db_name: str = "online_data"):
    """指定数据库的依赖注入工厂

    用法：conn = Depends(get_db_pool("archive"))
    """
    async def _dependency():
        async with db_manager.get_pool(db_name).acquire() as conn:
            yield conn
    return _dependency
