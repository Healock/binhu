SET NAMES utf8mb4;
-- 滨湖智慧平台 - 三库初始化脚本
-- MySQL 容器首次启动时自动执行（root 身份）
-- OnlineData 由 docker-compose MYSQL_DATABASE 自动创建，这里建另外两个库

CREATE DATABASE IF NOT EXISTS OnlineDataArchive CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS daily_report CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

GRANT ALL PRIVILEGES ON OnlineDataArchive.* TO 'binhu'@'%';
GRANT ALL PRIVILEGES ON daily_report.* TO 'binhu'@'%';
FLUSH PRIVILEGES;

-- ============================================================
-- OnlineData 库：配置表 + 腾讯文档业务表 + 走访导入表
-- ============================================================
USE OnlineData;

CREATE TABLE IF NOT EXISTS _system_config (
    config_key   VARCHAR(100) PRIMARY KEY,
    config_value TEXT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO _system_config (config_key, config_value) VALUES
    ('timezone', 'Asia/Shanghai'),
    ('online_summary_positions', '["组长", "组员"]'),
    ('visit_summary_positions', '["组长", "组员"]');

CREATE TABLE IF NOT EXISTS _grid_members (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    name             VARCHAR(100) NOT NULL,
    community        VARCHAR(200) DEFAULT '',
    position         VARCHAR(20) NOT NULL DEFAULT '组员',
    phone            VARCHAR(50) DEFAULT '',
    notes            VARCHAR(500) DEFAULT '',
    status           VARCHAR(10) NOT NULL DEFAULT '在岗',
    leave_start_date DATE DEFAULT NULL,
    leave_end_date   DATE DEFAULT NULL,
    leave_reason     VARCHAR(200) DEFAULT '',
    leave_source     VARCHAR(30) NOT NULL DEFAULT 'manual',
    id_card_number   VARCHAR(50) DEFAULT NULL,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP
                     ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_name (name),
    UNIQUE KEY uk_grid_id_card (id_card_number),
    INDEX idx_grid_position (position)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 配置表（从 binhu 库迁移）
CREATE TABLE IF NOT EXISTS _config_spreadsheets (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(100) NOT NULL,
    url             TEXT NOT NULL,
    file_id         VARCHAR(100) NOT NULL DEFAULT '',
    data_sheet_id   VARCHAR(20) NOT NULL DEFAULT '000001',
    summary_sheet_id VARCHAR(50) DEFAULT '汇总',
    header_row      INT DEFAULT 1,
    parser_type     VARCHAR(50) DEFAULT 'default',
    enabled         TINYINT(1) DEFAULT 1,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _config_oauth_tokens (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    client_id       VARCHAR(200) NOT NULL,
    client_secret   TEXT NOT NULL,
    access_token    TEXT,
    refresh_token   TEXT,
    open_id         VARCHAR(200),
    expires_at      DATETIME,
    updated_at      DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _sync_log (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    status          VARCHAR(20) DEFAULT 'pending',
    trigger_source  VARCHAR(20) NOT NULL DEFAULT 'manual',
    requested_by    INT DEFAULT NULL,
    phase           VARCHAR(30) NOT NULL DEFAULT 'queued',
    current_item    VARCHAR(200) DEFAULT NULL,
    total_steps     INT NOT NULL DEFAULT 0,
    completed_steps INT NOT NULL DEFAULT 0,
    total_rows      INT DEFAULT 0,
    processed_rows  INT DEFAULT 0,
    error_message   TEXT,
    started_at      DATETIME,
    finished_at     DATETIME,
    INDEX idx_sync_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _sync_schedule (
    id                TINYINT NOT NULL PRIMARY KEY,
    enabled           TINYINT(1) NOT NULL DEFAULT 1,
    interval_minutes  INT NOT NULL DEFAULT 5,
    next_run_at       DATETIME DEFAULT NULL,
    last_triggered_at DATETIME DEFAULT NULL,
    updated_by        INT DEFAULT NULL,
    updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO _sync_schedule (
    id, enabled, interval_minutes, next_run_at
) VALUES (
    1, 1, 5, DATE_ADD(UTC_TIMESTAMP(), INTERVAL 5 MINUTE)
);

CREATE TABLE IF NOT EXISTS _notifications (
    id               BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id          INT NOT NULL,
    category         VARCHAR(30) NOT NULL DEFAULT 'sync',
    severity         VARCHAR(20) NOT NULL DEFAULT 'error',
    title            VARCHAR(100) NOT NULL,
    content          TEXT NOT NULL,
    related_task_id  INT DEFAULT NULL,
    is_read          TINYINT(1) NOT NULL DEFAULT 0,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    read_at          DATETIME DEFAULT NULL,
    UNIQUE KEY uk_notification_user_task (user_id, category, related_task_id),
    INDEX idx_notification_unread (user_id, is_read, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _backup_schedule (
    id                TINYINT NOT NULL PRIMARY KEY,
    enabled           TINYINT(1) NOT NULL DEFAULT 1,
    run_hour          TINYINT NOT NULL DEFAULT 2,
    run_minute        TINYINT NOT NULL DEFAULT 0,
    retention_days    INT NOT NULL DEFAULT 7,
    next_run_at       DATETIME DEFAULT NULL,
    last_triggered_at DATETIME DEFAULT NULL,
    updated_by        INT DEFAULT NULL,
    updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO _backup_schedule (
    id, enabled, run_hour, run_minute, retention_days
) VALUES (1, 1, 2, 0, 7);

CREATE TABLE IF NOT EXISTS _backup_jobs (
    id             BIGINT AUTO_INCREMENT PRIMARY KEY,
    trigger_source VARCHAR(20) NOT NULL DEFAULT 'manual',
    status         VARCHAR(20) NOT NULL DEFAULT 'pending',
    requested_by   INT DEFAULT NULL,
    filename       VARCHAR(255) DEFAULT NULL,
    size_bytes     BIGINT DEFAULT NULL,
    sha256         CHAR(64) DEFAULT NULL,
    error_message  TEXT DEFAULT NULL,
    started_at     DATETIME DEFAULT NULL,
    finished_at    DATETIME DEFAULT NULL,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_backup_status (status),
    INDEX idx_backup_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _admin_audit_log (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id      INT DEFAULT NULL,
    username     VARCHAR(50) NOT NULL DEFAULT '',
    action       VARCHAR(80) NOT NULL,
    target_type  VARCHAR(50) NOT NULL DEFAULT '',
    target_name  VARCHAR(200) NOT NULL DEFAULT '',
    result       VARCHAR(20) NOT NULL DEFAULT 'success',
    detail_json  JSON DEFAULT NULL,
    ip_address   VARCHAR(45) NOT NULL DEFAULT '',
    user_agent   VARCHAR(300) NOT NULL DEFAULT '',
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_audit_user_time (user_id, created_at),
    INDEX idx_audit_action_time (action, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _communities (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    name       VARCHAR(200) NOT NULL UNIQUE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _community_aliases (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    community_id INT NOT NULL,
    alias        VARCHAR(200) NOT NULL,
    created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_community_alias (alias),
    INDEX idx_community_alias_owner (community_id),
    CONSTRAINT fk_community_alias_owner
        FOREIGN KEY (community_id)
        REFERENCES _communities(id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _visit_import_batches (
    id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
    import_type        VARCHAR(20) NOT NULL DEFAULT 'detail',
    filename           VARCHAR(255) NOT NULL,
    file_sha256        CHAR(64) NOT NULL,
    file_size_bytes    BIGINT NOT NULL DEFAULT 0,
    status             VARCHAR(20) NOT NULL DEFAULT 'running',
    uploader_id        INT DEFAULT NULL,
    sheet_name         VARCHAR(100) DEFAULT NULL,
    total_rows         INT NOT NULL DEFAULT 0,
    valid_rows         INT NOT NULL DEFAULT 0,
    inserted_rows      INT NOT NULL DEFAULT 0,
    updated_rows       INT NOT NULL DEFAULT 0,
    unchanged_rows     INT NOT NULL DEFAULT 0,
    ignored_rows       INT NOT NULL DEFAULT 0,
    error_count        INT NOT NULL DEFAULT 0,
    warning_count      INT NOT NULL DEFAULT 0,
    file_start_date    DATE DEFAULT NULL,
    file_end_date      DATE DEFAULT NULL,
    overlap_start_date DATE DEFAULT NULL,
    overlap_end_date   DATE DEFAULT NULL,
    error_message      TEXT DEFAULT NULL,
    started_at         DATETIME DEFAULT NULL,
    finished_at        DATETIME DEFAULT NULL,
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_visit_batch_hash (file_sha256),
    INDEX idx_visit_batch_type_hash (import_type, file_sha256, status),
    INDEX idx_visit_batch_status (status),
    INDEX idx_visit_batch_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS t_visit_details (
    id                    BIGINT AUTO_INCREMENT PRIMARY KEY,
    _row_key              CHAR(64) NOT NULL,
    派出所名称            VARCHAR(200) DEFAULT '',
    村社区                VARCHAR(200) NOT NULL,
    社区                  VARCHAR(200) NOT NULL,
    进入方式              VARCHAR(20) NOT NULL,
    地址                  VARCHAR(1000) NOT NULL,
    _normalized_address   VARCHAR(1000) NOT NULL,
    _address_key          CHAR(64) NOT NULL,
    操作人                VARCHAR(100) NOT NULL,
    操作人账号            VARCHAR(50) DEFAULT '',
    入户时间              DATETIME NOT NULL,
    业务日期              DATE NOT NULL,
    _raw_visit_time       VARCHAR(100) DEFAULT '',
    房间核查数量          INT UNSIGNED NOT NULL DEFAULT 0,
    新增                  INT UNSIGNED NOT NULL DEFAULT 0,
    变更                  INT UNSIGNED NOT NULL DEFAULT 0,
    注销                  INT UNSIGNED NOT NULL DEFAULT 0,
    星级派出所名称        VARCHAR(200) DEFAULT NULL,
    星级所属社区          VARCHAR(200) DEFAULT NULL,
    星级社区              VARCHAR(200) DEFAULT NULL,
    星级地址              VARCHAR(1000) DEFAULT NULL,
    得分                  DECIMAL(18, 6) DEFAULT NULL,
    星级                  VARCHAR(50) DEFAULT NULL,
    星级采集时间          DATETIME DEFAULT NULL,
    星级采集日期          DATE DEFAULT NULL,
    _raw_star_time        VARCHAR(100) DEFAULT NULL,
    隐患详情              MEDIUMTEXT DEFAULT NULL,
    星级时间差秒          INT UNSIGNED DEFAULT NULL,
    star_import_batch_id  BIGINT DEFAULT NULL,
    star_source_row_number INT DEFAULT NULL,
    import_batch_id       BIGINT NOT NULL,
    source_row_number     INT NOT NULL,
    created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                          ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_visit_row_key (_row_key),
    INDEX idx_visit_date (业务日期),
    INDEX idx_visit_community_date (社区, 业务日期),
    INDEX idx_visit_operator_date (操作人, 业务日期),
    INDEX idx_visit_address_date (_address_key, 业务日期),
    INDEX idx_visit_batch (import_batch_id),
    INDEX idx_visit_star_time (星级采集时间),
    INDEX idx_visit_star_batch (star_import_batch_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _visit_import_issues (
    id           BIGINT AUTO_INCREMENT PRIMARY KEY,
    batch_id     BIGINT NOT NULL,
    severity     VARCHAR(20) NOT NULL,
    code         VARCHAR(60) NOT NULL,
    source_row_number INT NOT NULL DEFAULT 0,
    message      VARCHAR(500) NOT NULL,
    row_preview  JSON DEFAULT NULL,
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_visit_issue_batch (batch_id, severity, source_row_number)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 1. 全链条（14列业务数据）
CREATE TABLE IF NOT EXISTS t_fullchain (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    _row_key        VARCHAR(200) NOT NULL,
    下发日期        VARCHAR(50),
    截止日期        VARCHAR(50),
    核查人          VARCHAR(100),
    社区            VARCHAR(200),
    来源            VARCHAR(200),
    姓名            VARCHAR(100),
    身份证号        VARCHAR(50),
    电话号码        VARCHAR(50),
    地址            VARCHAR(500),
    创建时间        VARCHAR(50),
    现住址          VARCHAR(500),
    核查结果        VARCHAR(500),
    研判            VARCHAR(500),
    二次反馈        VARCHAR(500),
    _first_seen_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    _last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_row_key (_row_key),
    INDEX idx_fc_inspector (核查人),
    INDEX idx_fc_community (社区)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 2. 出租房屋核查（13列）
CREATE TABLE IF NOT EXISTS t_rental_check (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    _row_key        VARCHAR(200) NOT NULL,
    下发时间        VARCHAR(50),
    截止时间        VARCHAR(50),
    核查人          VARCHAR(100),
    社区            VARCHAR(200),
    姓名            VARCHAR(100),
    身份证号        VARCHAR(50),
    手机号码        VARCHAR(50),
    房屋地址        VARCHAR(500),
    现住址          VARCHAR(500),
    核查结果        VARCHAR(500),
    入住方式        VARCHAR(100),
    研判            VARCHAR(500),
    二次反馈        VARCHAR(500),
    _first_seen_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    _last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_row_key (_row_key),
    INDEX idx_rc_inspector (核查人),
    INDEX idx_rc_community (社区)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 3. 寄递业（14列）
CREATE TABLE IF NOT EXISTS t_delivery_industry (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    _row_key        VARCHAR(200) NOT NULL,
    下发时间        VARCHAR(50),
    截止时间        VARCHAR(50),
    核查人          VARCHAR(100),
    姓名            VARCHAR(100),
    身份证号        VARCHAR(50),
    地址1           VARCHAR(500),
    手机号码        VARCHAR(50),
    社区            VARCHAR(200),
    参考姓名        VARCHAR(100),
    参考身份证号码  VARCHAR(50),
    现住址          VARCHAR(500),
    核查结果        VARCHAR(500),
    研判            VARCHAR(500),
    二次反馈        VARCHAR(500),
    _first_seen_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    _last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_row_key (_row_key),
    INDEX idx_di_inspector (核查人),
    INDEX idx_di_community (社区)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 4. 涉警统计（12列，仅raw入库）
CREATE TABLE IF NOT EXISTS t_police_stats (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    _row_key        VARCHAR(200) NOT NULL,
    序号            VARCHAR(50),
    日期            VARCHAR(50),
    社区            VARCHAR(200),
    简要警情及处理结果 TEXT,
    是否开户        VARCHAR(100),
    现住址          VARCHAR(500),
    房屋属性        VARCHAR(200),
    居住时间        VARCHAR(100),
    房东信息        VARCHAR(500),
    二房东信息      VARCHAR(500),
    备注            VARCHAR(500),
    房东是否处罚    VARCHAR(200),
    _first_seen_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    _last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_row_key (_row_key),
    INDEX idx_ps_community (社区)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 5. 疑似未注销模型三（9列）
CREATE TABLE IF NOT EXISTS t_suspect_unrevoked (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    _row_key        VARCHAR(200) NOT NULL,
    截止时间        VARCHAR(50),
    核查人          VARCHAR(100),
    姓名            VARCHAR(100),
    身份证号        VARCHAR(50),
    联系方式        VARCHAR(50),
    地址            VARCHAR(500),
    下发社区        VARCHAR(200),
    核查结果        VARCHAR(500),
    备注            VARCHAR(500),
    _first_seen_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    _last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_row_key (_row_key),
    INDEX idx_su_inspector (核查人),
    INDEX idx_su_community (下发社区)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 6. 疑似返苏（12列）
CREATE TABLE IF NOT EXISTS t_suspect_return (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    _row_key        VARCHAR(200) NOT NULL,
    下发日期        VARCHAR(50),
    截止日期        VARCHAR(50),
    核查人          VARCHAR(100),
    社区            VARCHAR(200),
    姓名            VARCHAR(100),
    身份证号码      VARCHAR(500),
    联系号码        VARCHAR(500),
    高频抓拍小区    VARCHAR(200),
    现住址          VARCHAR(500),
    核查反馈        VARCHAR(500),
    研判            VARCHAR(500),
    二次核查结果    VARCHAR(500),
    _first_seen_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    _last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_row_key (_row_key),
    INDEX idx_sr_inspector (核查人),
    INDEX idx_sr_community (社区)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 7. 群租房核查（16列，仅raw入库）
CREATE TABLE IF NOT EXISTS t_group_rental (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    _row_key        VARCHAR(200) NOT NULL,
    核查人          VARCHAR(100),
    社区            VARCHAR(200),
    出租屋编号      VARCHAR(100),
    出租屋地址      VARCHAR(500),
    更新时间        VARCHAR(50),
    居住证_居住人数 VARCHAR(20),
    居住证_间数     VARCHAR(20),
    居住证_床位数   VARCHAR(20),
    核查_人数       VARCHAR(20),
    核查_房间数     VARCHAR(20),
    核查_床位数     VARCHAR(20),
    入户走访        VARCHAR(500),
    走访日期        VARCHAR(50),
    星级评定        VARCHAR(100),
    责任书签订      VARCHAR(200),
    实际情况        VARCHAR(500),
    _first_seen_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    _last_updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_row_key (_row_key),
    INDEX idx_gr_inspector (核查人),
    INDEX idx_gr_community (社区)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ============================================================
-- OnlineDataArchive 库：7张归档表（结构同业务表 + 归档元数据）
-- ============================================================
USE OnlineDataArchive;

CREATE TABLE IF NOT EXISTS t_fullchain_archive LIKE OnlineData.t_fullchain;
ALTER TABLE t_fullchain_archive ADD COLUMN _archived_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                ADD COLUMN _archive_reason VARCHAR(100) DEFAULT 'online_removed',
                                DROP INDEX uk_row_key,
                                ADD INDEX idx_row_key (_row_key);

CREATE TABLE IF NOT EXISTS t_rental_check_archive LIKE OnlineData.t_rental_check;
ALTER TABLE t_rental_check_archive ADD COLUMN _archived_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                   ADD COLUMN _archive_reason VARCHAR(100) DEFAULT 'online_removed',
                                   DROP INDEX uk_row_key,
                                   ADD INDEX idx_row_key (_row_key);

CREATE TABLE IF NOT EXISTS t_delivery_industry_archive LIKE OnlineData.t_delivery_industry;
ALTER TABLE t_delivery_industry_archive ADD COLUMN _archived_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                        ADD COLUMN _archive_reason VARCHAR(100) DEFAULT 'online_removed',
                                        DROP INDEX uk_row_key,
                                        ADD INDEX idx_row_key (_row_key);

CREATE TABLE IF NOT EXISTS t_police_stats_archive LIKE OnlineData.t_police_stats;
ALTER TABLE t_police_stats_archive ADD COLUMN _archived_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                   ADD COLUMN _archive_reason VARCHAR(100) DEFAULT 'online_removed',
                                   DROP INDEX uk_row_key,
                                   ADD INDEX idx_row_key (_row_key);

CREATE TABLE IF NOT EXISTS t_suspect_unrevoked_archive LIKE OnlineData.t_suspect_unrevoked;
ALTER TABLE t_suspect_unrevoked_archive ADD COLUMN _archived_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                        ADD COLUMN _archive_reason VARCHAR(100) DEFAULT 'online_removed',
                                        DROP INDEX uk_row_key,
                                        ADD INDEX idx_row_key (_row_key);

CREATE TABLE IF NOT EXISTS t_suspect_return_archive LIKE OnlineData.t_suspect_return;
ALTER TABLE t_suspect_return_archive ADD COLUMN _archived_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                     ADD COLUMN _archive_reason VARCHAR(100) DEFAULT 'online_removed',
                                     DROP INDEX uk_row_key,
                                     ADD INDEX idx_row_key (_row_key);

CREATE TABLE IF NOT EXISTS t_group_rental_archive LIKE OnlineData.t_group_rental;
ALTER TABLE t_group_rental_archive ADD COLUMN _archived_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                                   ADD COLUMN _archive_reason VARCHAR(100) DEFAULT 'online_removed',
                                   DROP INDEX uk_row_key,
                                   ADD INDEX idx_row_key (_row_key);

-- ============================================================
-- daily_report 库：元数据表（日报表后续动态创建）
-- ============================================================
USE daily_report;

CREATE TABLE IF NOT EXISTS _daily_report_meta (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    table_name      VARCHAR(100) NOT NULL,
    report_date     DATE NOT NULL,
    parser_type     VARCHAR(50) NOT NULL,
    generation_method VARCHAR(20) DEFAULT 'auto',
    generated_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_table_name (table_name),
    INDEX idx_date (report_date),
    INDEX idx_type (parser_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
