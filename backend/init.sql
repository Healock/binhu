SET NAMES utf8mb4;
-- 滨湖智慧平台 - 八库初始化脚本
-- MySQL 容器首次启动时自动执行（root 身份）
-- OnlineData 由 docker-compose MYSQL_DATABASE 自动创建，其余业务域库在这里创建。

CREATE DATABASE IF NOT EXISTS OnlineDataArchive CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS daily_report CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS PlatformData CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS VisitData CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS DispatchData CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS RegistryData CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS WorkflowData CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

GRANT ALL PRIVILEGES ON OnlineDataArchive.* TO 'binhu'@'%';
GRANT ALL PRIVILEGES ON daily_report.* TO 'binhu'@'%';
GRANT ALL PRIVILEGES ON PlatformData.* TO 'binhu'@'%';
GRANT ALL PRIVILEGES ON VisitData.* TO 'binhu'@'%';
GRANT ALL PRIVILEGES ON DispatchData.* TO 'binhu'@'%';
GRANT ALL PRIVILEGES ON RegistryData.* TO 'binhu'@'%';
GRANT ALL PRIVILEGES ON WorkflowData.* TO 'binhu'@'%';
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
    ('visit_summary_positions', '["组长", "组员"]'),
    ('weekend_duty_positions', '["组长", "组员"]'),
    ('session_idle_minutes', '30'),
    ('permission_enforcement_enabled', '0'),
    ('online_writeback_enabled', '0'),
    ('maintenance_enabled', '0'),
    ('maintenance_start_at', ''),
    ('maintenance_end_at', ''),
    ('maintenance_message', '平台正在维护中，请稍后再试');
CREATE TABLE IF NOT EXISTS _grid_members (
    id               INT AUTO_INCREMENT PRIMARY KEY,
    name             VARCHAR(100) NOT NULL,
    community        VARCHAR(200) DEFAULT '',
    department_id    INT DEFAULT NULL,
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
    INDEX idx_grid_position (position),
    INDEX idx_grid_department (department_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _personnel_attendance_history (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    member_id       INT NOT NULL,
    absence_type    VARCHAR(30) NOT NULL,
    start_date      DATE NOT NULL,
    end_date        DATE DEFAULT NULL,
    reason          VARCHAR(200) DEFAULT '',
    source          VARCHAR(30) NOT NULL DEFAULT 'manual',
    created_by      INT DEFAULT NULL,
    is_active       TINYINT(1) NOT NULL DEFAULT 1,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_attendance_member_dates (member_id, start_date, end_date),
    INDEX idx_attendance_active (is_active, start_date, end_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _personnel_weekend_duty (
    id                  BIGINT AUTO_INCREMENT PRIMARY KEY,
    week_start          DATE NOT NULL,
    member_id           INT NOT NULL,
    duty_date           DATE DEFAULT NULL,
    member_name         VARCHAR(100) NOT NULL,
    community_snapshot  VARCHAR(200) DEFAULT '',
    position_snapshot   VARCHAR(20) NOT NULL,
    updated_by          INT DEFAULT NULL,
    created_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_weekend_member (week_start, member_id),
    INDEX idx_weekend_duty_date (duty_date),
    INDEX idx_weekend_week (week_start)
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
    interval_minutes  INT NOT NULL DEFAULT 10,
    next_run_at       DATETIME DEFAULT NULL,
    last_triggered_at DATETIME DEFAULT NULL,
    updated_by        INT DEFAULT NULL,
    updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO _sync_schedule (
    id, enabled, interval_minutes, next_run_at
) VALUES (
    1, 1, 10, DATE_ADD(UTC_TIMESTAMP(), INTERVAL 10 MINUTE)
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

CREATE TABLE IF NOT EXISTS _txdocs_api_usage_hourly (
    bucket_hour          DATETIME NOT NULL,
    request_source       VARCHAR(40) NOT NULL DEFAULT 'unknown',
    endpoint             VARCHAR(40) NOT NULL,
    method               VARCHAR(10) NOT NULL,
    attempt_count        INT UNSIGNED NOT NULL DEFAULT 0,
    success_count        INT UNSIGNED NOT NULL DEFAULT 0,
    failure_count        INT UNSIGNED NOT NULL DEFAULT 0,
    retry_count          INT UNSIGNED NOT NULL DEFAULT 0,
    quota_exhausted_count INT UNSIGNED NOT NULL DEFAULT 0,
    last_http_status     SMALLINT DEFAULT NULL,
    last_error_code      VARCHAR(40) NOT NULL DEFAULT '',
    updated_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                         ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (bucket_hour, request_source, endpoint, method),
    INDEX idx_txdocs_usage_hour (bucket_hour)
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

CREATE TABLE IF NOT EXISTS _work_log_drafts (
    id                BIGINT AUTO_INCREMENT PRIMARY KEY,
    report_type       VARCHAR(20) NOT NULL DEFAULT 'daily',
    business_date     DATE NOT NULL,
    owner_user_id     INT NOT NULL,
    owner_username    VARCHAR(50) NOT NULL DEFAULT '',
    template_version  VARCHAR(30) NOT NULL DEFAULT 'daily-v2',
    system_snapshot   JSON NOT NULL,
    manual_values     JSON NOT NULL,
    override_values   JSON NOT NULL,
    version           INT UNSIGNED NOT NULL DEFAULT 1,
    last_export_at    DATETIME DEFAULT NULL,
    created_by        INT NOT NULL,
    updated_by        INT NOT NULL,
    created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                      ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_work_log_type_date (report_type, business_date),
    INDEX idx_work_log_owner_date (owner_user_id, business_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _communities (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(200) NOT NULL UNIQUE,
    police_officers JSON DEFAULT NULL,
    area_id         INT DEFAULT NULL,
    qmf_community_code VARCHAR(20) DEFAULT NULL,
    is_active       TINYINT(1) NOT NULL DEFAULT 1,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _administrative_areas (
    source_row INT NOT NULL PRIMARY KEY,
    code CHAR(6) NOT NULL,
    name VARCHAR(100) NOT NULL,
    level VARCHAR(20) NOT NULL,
    province VARCHAR(100) NOT NULL DEFAULT '',
    city VARCHAR(100) NOT NULL DEFAULT '',
    parent_code CHAR(6) NOT NULL DEFAULT '',
    path VARCHAR(300) NOT NULL DEFAULT '',
    full_name VARCHAR(300) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT '',
    start_year SMALLINT DEFAULT NULL,
    end_year SMALLINT DEFAULT NULL,
    new_code VARCHAR(300) NOT NULL DEFAULT '',
    source VARCHAR(50) NOT NULL DEFAULT '',
    INDEX idx_administrative_area_code (code, status),
    INDEX idx_administrative_area_period (code, start_year, end_year)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _areas (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    name       VARCHAR(100) NOT NULL UNIQUE,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
               ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _area_leader_links (
    area_id   INT NOT NULL,
    member_id INT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (area_id, member_id),
    INDEX idx_area_leader_member (member_id, area_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO _areas (name) VALUES ('东片'), ('中片'), ('西片');

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

CREATE TABLE IF NOT EXISTS _online_source_rows (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    spreadsheet_id INT NOT NULL,
    parser_type VARCHAR(50) NOT NULL,
    sheet_id VARCHAR(100) NOT NULL,
    physical_row INT NOT NULL,
    row_key CHAR(32) NOT NULL,
    row_hash CHAR(64) NOT NULL,
    values_json JSON NOT NULL,
    cell_meta_json JSON NOT NULL,
    revision BIGINT UNSIGNED NOT NULL DEFAULT 1,
    refreshed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_online_source_position (spreadsheet_id, sheet_id, physical_row),
    INDEX idx_online_source_business (parser_type, row_key, spreadsheet_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _online_source_projection (
    parser_type VARCHAR(50) NOT NULL,
    row_key CHAR(32) NOT NULL,
    values_json JSON NOT NULL,
    community VARCHAR(200) NOT NULL DEFAULT '',
    inspector VARCHAR(100) NOT NULL DEFAULT '',
    task_state VARCHAR(20) NOT NULL DEFAULT '',
    source_count INT NOT NULL DEFAULT 1,
    conflict TINYINT(1) NOT NULL DEFAULT 0,
    search_text MEDIUMTEXT NOT NULL,
    pending_state VARCHAR(20) NOT NULL DEFAULT '',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
               ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (parser_type, row_key),
    INDEX idx_source_projection_community (parser_type, community),
    INDEX idx_source_projection_pending (parser_type, pending_state),
    INDEX idx_source_projection_tasks (parser_type, community, inspector, task_state)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _qmf_status_scan_runs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    trigger_source VARCHAR(20) NOT NULL,
    scan_mode VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'queued',
    concurrency INT NOT NULL DEFAULT 4,
    total_count INT NOT NULL DEFAULT 0,
    processed_count INT NOT NULL DEFAULT 0,
    match_count INT NOT NULL DEFAULT 0,
    mismatch_count INT NOT NULL DEFAULT 0,
    pending_count INT NOT NULL DEFAULT 0,
    not_found_count INT NOT NULL DEFAULT 0,
    non_jurisdiction_count INT NOT NULL DEFAULT 0,
    error_count INT NOT NULL DEFAULT 0,
    requested_by INT DEFAULT NULL,
    scheduled_date DATE DEFAULT NULL,
    error_code VARCHAR(64) NOT NULL DEFAULT '',
    started_at DATETIME DEFAULT NULL,
    finished_at DATETIME DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_qmf_status_scan_schedule (trigger_source, scheduled_date),
    INDEX idx_qmf_status_scan_status (status, id),
    INDEX idx_qmf_status_scan_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _qmf_status_scan_items (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id BIGINT NOT NULL,
    parser_type VARCHAR(50) NOT NULL,
    row_key CHAR(32) NOT NULL,
    source_id BIGINT NOT NULL,
    expected_revision BIGINT UNSIGNED NOT NULL,
    expected_row_hash CHAR(64) NOT NULL,
    identity_hmac CHAR(64) NOT NULL DEFAULT '',
    expected_result VARCHAR(30) NOT NULL DEFAULT '',
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    feedback_state VARCHAR(40) NOT NULL DEFAULT '',
    feedback_result VARCHAR(30) NOT NULL DEFAULT '',
    checked_at VARCHAR(64) NOT NULL DEFAULT '',
    origin VARCHAR(40) NOT NULL DEFAULT '',
    error_code VARCHAR(64) NOT NULL DEFAULT '',
    duration_ms INT NOT NULL DEFAULT 0,
    started_at DATETIME DEFAULT NULL,
    finished_at DATETIME DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_qmf_status_scan_item (run_id, parser_type, row_key),
    INDEX idx_qmf_status_scan_item_queue (run_id, status, id),
    INDEX idx_qmf_status_scan_item_source (source_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _qmf_status_snapshots (
    parser_type VARCHAR(50) NOT NULL,
    row_key CHAR(32) NOT NULL,
    source_id BIGINT NOT NULL,
    source_revision BIGINT UNSIGNED NOT NULL,
    source_row_hash CHAR(64) NOT NULL,
    identity_hmac CHAR(64) NOT NULL DEFAULT '',
    platform_result VARCHAR(30) NOT NULL DEFAULT '',
    feedback_state VARCHAR(40) NOT NULL DEFAULT '',
    feedback_result VARCHAR(30) NOT NULL DEFAULT '',
    checked_at VARCHAR(64) NOT NULL DEFAULT '',
    origin VARCHAR(40) NOT NULL DEFAULT '',
    error_code VARCHAR(64) NOT NULL DEFAULT '',
    scan_run_id BIGINT NOT NULL,
    last_scanned_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (parser_type, row_key),
    INDEX idx_qmf_status_snapshot_state (parser_type, feedback_state, last_scanned_at),
    INDEX idx_qmf_status_snapshot_source (source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _online_source_cache_state (
    spreadsheet_id INT NOT NULL PRIMARY KEY,
    parser_type VARCHAR(50) NOT NULL,
    row_count INT NOT NULL DEFAULT 0,
    refreshed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_source_cache_parser (parser_type, refreshed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _online_writeback_audit (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    user_id INT NOT NULL,
    username VARCHAR(50) NOT NULL DEFAULT '',
    action VARCHAR(20) NOT NULL,
    parser_type VARCHAR(50) NOT NULL,
    spreadsheet_id INT NOT NULL,
    sheet_id VARCHAR(100) NOT NULL,
    physical_row INT DEFAULT NULL,
    column_name VARCHAR(200) DEFAULT NULL,
    row_key_before CHAR(32) DEFAULT NULL,
    row_key_after CHAR(32) DEFAULT NULL,
    before_values JSON DEFAULT NULL,
    after_values JSON DEFAULT NULL,
    sync_status VARCHAR(20) NOT NULL DEFAULT 'pending',
    synced_at DATETIME DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_writeback_audit_time (created_at),
    INDEX idx_writeback_audit_pending (spreadsheet_id, sync_status, created_at),
    INDEX idx_writeback_audit_user (user_id, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _online_local_changes (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    audit_id BIGINT NOT NULL,
    source_id BIGINT NOT NULL,
    parser_type VARCHAR(50) NOT NULL,
    spreadsheet_id INT NOT NULL,
    sheet_id VARCHAR(100) NOT NULL,
    physical_row INT NOT NULL,
    row_key CHAR(32) NOT NULL,
    field_name VARCHAR(200) NOT NULL,
    base_value TEXT NOT NULL,
    local_value TEXT NOT NULL,
    remote_value TEXT DEFAULT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    attempt_count INT UNSIGNED NOT NULL DEFAULT 0,
    next_attempt_at DATETIME DEFAULT NULL,
    error_code VARCHAR(100) NOT NULL DEFAULT '',
    last_error VARCHAR(500) NOT NULL DEFAULT '',
    user_id INT NOT NULL,
    username VARCHAR(50) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_online_local_change_field (source_id, field_name),
    INDEX idx_online_local_change_audit (audit_id, status),
    INDEX idx_online_local_change_due (status, next_attempt_at, updated_at),
    INDEX idx_online_local_change_row (parser_type, row_key, status),
    INDEX idx_online_local_change_source (source_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _police_address_entries (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(300) NOT NULL,
    normalized_name VARCHAR(300) NOT NULL,
    detail_address VARCHAR(1000) NOT NULL DEFAULT '',
    address_type VARCHAR(30) NOT NULL DEFAULT 'community',
    pattern VARCHAR(200) NOT NULL DEFAULT '',
    community_id INT DEFAULT NULL,
    aliases_json JSON NOT NULL,
    source_flags JSON NOT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    created_by INT DEFAULT NULL,
    updated_by INT DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_police_address_name_community (normalized_name, community_id),
    INDEX idx_police_address_community (community_id, enabled),
    INDEX idx_police_address_type (address_type, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _police_address_imports (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    import_kind VARCHAR(30) NOT NULL,
    file_name VARCHAR(255) NOT NULL DEFAULT '',
    file_sha256 CHAR(64) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'preview',
    imported_count INT NOT NULL DEFAULT 0,
    created_count INT NOT NULL DEFAULT 0,
    merged_count INT NOT NULL DEFAULT 0,
    conflict_count INT NOT NULL DEFAULT 0,
    created_by INT DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_police_address_import_hash (import_kind, file_sha256),
    INDEX idx_police_address_import_time (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _police_address_sources (
    entry_id BIGINT NOT NULL,
    import_id BIGINT NOT NULL,
    source_kind VARCHAR(30) NOT NULL,
    source_row INT NOT NULL,
    source_name VARCHAR(300) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (entry_id, import_id, source_row),
    INDEX idx_police_address_source_import (import_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _police_address_import_conflicts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    import_id BIGINT DEFAULT NULL,
    source_row INT NOT NULL DEFAULT 0,
    reason VARCHAR(500) NOT NULL,
    values_json JSON NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_police_address_conflict_import (import_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _police_dispatch_batches (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    file_name VARCHAR(255) NOT NULL DEFAULT '',
    file_sha256 CHAR(64) NOT NULL UNIQUE,
    sheet_name VARCHAR(255) NOT NULL DEFAULT '',
    status VARCHAR(30) NOT NULL DEFAULT 'reviewing',
    total_count INT NOT NULL DEFAULT 0,
    counts_json JSON NOT NULL,
    imported_by INT NOT NULL,
    first_publish_date DATE DEFAULT NULL,
    publish_started_at DATETIME DEFAULT NULL,
    completed_at DATETIME DEFAULT NULL,
    last_error VARCHAR(500) NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_police_dispatch_batch_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _police_dispatch_tasks (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    batch_id BIGINT NOT NULL,
    source_row INT NOT NULL,
    source_name VARCHAR(300) NOT NULL DEFAULT '',
    person_name VARCHAR(200) NOT NULL DEFAULT '',
    identity_number VARCHAR(50) NOT NULL DEFAULT '',
    identity_hash CHAR(64) NOT NULL DEFAULT '',
    phone VARCHAR(200) NOT NULL DEFAULT '',
    original_address VARCHAR(1500) NOT NULL DEFAULT '',
    source_created_time VARCHAR(100) NOT NULL DEFAULT '',
    transfer_note TEXT,
    raw_values_json JSON NOT NULL,
    duplicate_group_key CHAR(64) NOT NULL DEFAULT '',
    duplicate_kind VARCHAR(30) NOT NULL DEFAULT '',
    suggested_action VARCHAR(30) NOT NULL DEFAULT 'dispatch',
    suggested_community_id INT DEFAULT NULL,
    suggestion_reason VARCHAR(1000) NOT NULL DEFAULT '',
    allocation_mode VARCHAR(30) NOT NULL DEFAULT '',
    final_action VARCHAR(30) NOT NULL DEFAULT '',
    final_community_id INT DEFAULT NULL,
    review_note VARCHAR(1000) NOT NULL DEFAULT '',
    reviewed_by INT DEFAULT NULL,
    reviewer_name VARCHAR(100) NOT NULL DEFAULT '',
    reviewed_at DATETIME DEFAULT NULL,
    version INT UNSIGNED NOT NULL DEFAULT 1,
    task_status VARCHAR(30) NOT NULL DEFAULT 'pending_review',
    publish_status VARCHAR(30) NOT NULL DEFAULT 'not_required',
    published_row INT DEFAULT NULL,
    publish_key CHAR(64) NOT NULL DEFAULT '',
    publish_error VARCHAR(500) NOT NULL DEFAULT '',
    linked_source_id BIGINT DEFAULT NULL,
    linked_row_hash CHAR(64) NOT NULL DEFAULT '',
    conflict_values_json JSON DEFAULT NULL,
    cache_pending TINYINT(1) NOT NULL DEFAULT 0,
    published_at DATETIME DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_police_dispatch_source_row (batch_id, source_row),
    INDEX idx_police_dispatch_task_filter (batch_id, task_status, suggested_action),
    INDEX idx_police_dispatch_task_duplicate (batch_id, duplicate_group_key),
    INDEX idx_police_dispatch_task_identity (batch_id, identity_hash),
    INDEX idx_police_dispatch_task_publish (batch_id, publish_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _police_dispatch_publish_results (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id BIGINT NOT NULL UNIQUE,
    spreadsheet_id INT NOT NULL,
    sheet_id VARCHAR(100) NOT NULL,
    physical_row INT DEFAULT NULL,
    business_key CHAR(64) NOT NULL DEFAULT '',
    request_values_json JSON NOT NULL,
    verified_values_json JSON DEFAULT NULL,
    source_row_id BIGINT DEFAULT NULL,
    expected_row_hash CHAR(64) NOT NULL DEFAULT '',
    resolution VARCHAR(30) NOT NULL DEFAULT '',
    cache_pending TINYINT(1) NOT NULL DEFAULT 0,
    status VARCHAR(30) NOT NULL DEFAULT 'pending',
    error_code VARCHAR(100) NOT NULL DEFAULT '',
    error_message VARCHAR(500) NOT NULL DEFAULT '',
    attempt_count INT NOT NULL DEFAULT 0,
    last_attempt_at DATETIME DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_police_publish_status (status, updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _departments (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    name            VARCHAR(200) NOT NULL UNIQUE,
    department_type VARCHAR(20) NOT NULL,
    community_id    INT DEFAULT NULL,
    is_active       TINYINT(1) NOT NULL DEFAULT 1,
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                    ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_department_community (community_id),
    INDEX idx_department_type_active (department_type, is_active)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _grid_member_department_links (
    member_id     INT NOT NULL,
    department_id INT NOT NULL,
    sort_order    INT NOT NULL DEFAULT 0,
    created_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                  ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (member_id, department_id),
    INDEX idx_member_department_department (department_id, member_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO _grid_member_department_links
    (member_id, department_id, sort_order)
SELECT id, department_id, 0
FROM _grid_members
WHERE department_id IS NOT NULL;

INSERT IGNORE INTO _departments (name, department_type)
VALUES ('内勤', 'internal');

INSERT IGNORE INTO _departments (name, department_type, community_id)
SELECT name, 'community', id FROM _communities;

CREATE TABLE IF NOT EXISTS _permission_groups (
    id          INT AUTO_INCREMENT PRIMARY KEY,
    code        VARCHAR(50) NOT NULL UNIQUE,
    name        VARCHAR(100) NOT NULL UNIQUE,
    description VARCHAR(500) NOT NULL DEFAULT '',
    permissions JSON NOT NULL,
    data_scope  VARCHAR(30) NOT NULL DEFAULT 'own_department',
    is_system   TINYINT(1) NOT NULL DEFAULT 0,
    is_locked   TINYINT(1) NOT NULL DEFAULT 0,
    sort_order  INT NOT NULL DEFAULT 100,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _position_permission_groups (
    position            VARCHAR(20) NOT NULL PRIMARY KEY,
    permission_group_id INT NOT NULL,
    updated_by          INT DEFAULT NULL,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_position_permission_group (permission_group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _position_permission_group_links (
    position            VARCHAR(20) NOT NULL,
    permission_group_id INT NOT NULL,
    updated_by          INT DEFAULT NULL,
    updated_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (position, permission_group_id),
    INDEX idx_position_group_link_group (permission_group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _user_permission_group_links (
    user_id             INT NOT NULL,
    permission_group_id INT NOT NULL,
    assigned_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, permission_group_id),
    INDEX idx_user_group_link_group (permission_group_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _permission_change_log (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    action VARCHAR(100) NOT NULL,
    target_type VARCHAR(50) NOT NULL,
    target_id VARCHAR(100) NOT NULL,
    detail JSON DEFAULT NULL,
    changed_by INT DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_permission_change_target (target_type, target_id),
    INDEX idx_permission_change_time (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _users (
    id                       INT AUTO_INCREMENT PRIMARY KEY,
    username                 VARCHAR(50) NOT NULL UNIQUE,
    display_name             VARCHAR(100) NOT NULL DEFAULT '',
    password_hash            VARCHAR(255) NOT NULL,
    role                     ENUM('super_admin','admin','leader','member')
                             NOT NULL DEFAULT 'member',
    member_id                INT DEFAULT NULL,
    permission_group_id      INT DEFAULT NULL,
    group_assignment_mode    VARCHAR(20) NOT NULL DEFAULT 'inherited',
    password_is_temporary    TINYINT(1) NOT NULL DEFAULT 0,
    active_session_id        VARCHAR(64) DEFAULT NULL,
    active_desktop_session_id VARCHAR(64) DEFAULT NULL,
    active_mobile_session_id VARCHAR(64) DEFAULT NULL,
    table_display_mode       VARCHAR(10) NOT NULL DEFAULT 'table',
    task_display_mode        VARCHAR(10) NOT NULL DEFAULT 'card',
    report_column_mode       VARCHAR(10) NOT NULL DEFAULT 'three',
    mobile_navigation_mode   VARCHAR(10) NOT NULL DEFAULT 'dock',
    mobile_dock_config       JSON DEFAULT NULL,
    theme_mode               VARCHAR(10) NOT NULL DEFAULT 'light',
    created_at               DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at               DATETIME DEFAULT CURRENT_TIMESTAMP
                             ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_users_member (member_id),
    INDEX idx_users_permission_group (permission_group_id),
    INDEX idx_users_active_session (active_session_id),
    INDEX idx_users_active_desktop_session (active_desktop_session_id),
    INDEX idx_users_active_mobile_session (active_mobile_session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _sessions (
    session_id       VARCHAR(64) PRIMARY KEY,
    management_id     CHAR(36) UNIQUE,
    user_id          INT NOT NULL,
    device_type      VARCHAR(10) DEFAULT NULL,
    device_id_hash   CHAR(64) DEFAULT NULL,
    client_platform  VARCHAR(20) DEFAULT NULL,
    user_agent_family VARCHAR(40) DEFAULT NULL,
    created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_activity_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at       DATETIME NOT NULL,
    INDEX idx_user (user_id),
    INDEX idx_expires (expires_at),
    INDEX idx_sessions_user_device (user_id, device_type, expires_at),
    INDEX idx_session_user_activity (user_id, last_activity_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _user_presence_clients (
    client_id        VARCHAR(64) PRIMARY KEY,
    user_id          INT NOT NULL,
    session_id       VARCHAR(64) NOT NULL,
    last_seen_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_presence_last_seen (last_seen_at),
    INDEX idx_presence_user (user_id),
    INDEX idx_presence_session (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _visit_import_batches (
    id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
    import_type        VARCHAR(20) NOT NULL DEFAULT 'detail',
    filename           VARCHAR(255) NOT NULL,
    file_sha256        CHAR(64) NOT NULL,
    file_size_bytes    BIGINT NOT NULL DEFAULT 0,
    status             VARCHAR(20) NOT NULL DEFAULT 'running',
    uploader_id        INT DEFAULT NULL,
    source_type        VARCHAR(20) NOT NULL DEFAULT 'manual',
    source_run_id      BIGINT DEFAULT NULL,
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
    INDEX idx_visit_batch_source (source_type, source_run_id),
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

CREATE TABLE IF NOT EXISTS _visit_source_runs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    source_kind VARCHAR(20) NOT NULL,
    trigger_source VARCHAR(20) NOT NULL DEFAULT 'manual',
    status VARCHAR(30) NOT NULL DEFAULT 'preview',
    requested_by INT DEFAULT NULL,
    requested_start_date DATE NOT NULL,
    requested_end_date DATE NOT NULL,
    response_business_date DATE DEFAULT NULL,
    source_page VARCHAR(120) NOT NULL,
    source_url VARCHAR(500) DEFAULT NULL,
    record_count INT NOT NULL DEFAULT 0,
    valid_count INT NOT NULL DEFAULT 0,
    issue_count INT NOT NULL DEFAULT 0,
    summary_json JSON DEFAULT NULL,
    payload_json JSON DEFAULT NULL,
    error_code VARCHAR(60) DEFAULT NULL,
    error_message VARCHAR(500) DEFAULT NULL,
    confirmed_by INT DEFAULT NULL,
    confirmed_at DATETIME DEFAULT NULL,
    superseded_by BIGINT DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_visit_source_kind_status (source_kind, status),
    INDEX idx_visit_source_dates (requested_start_date, requested_end_date),
    INDEX idx_visit_source_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _code_summary_runs (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    source_kind VARCHAR(20) NOT NULL,
    trigger_source VARCHAR(20) NOT NULL DEFAULT 'manual',
    status VARCHAR(20) NOT NULL,
    requested_by INT DEFAULT NULL,
    requested_start_date DATE NOT NULL,
    requested_end_date DATE NOT NULL,
    source_endpoint VARCHAR(190) NOT NULL,
    raw_count INT NOT NULL DEFAULT 0,
    valid_count INT NOT NULL DEFAULT 0,
    excluded_count INT NOT NULL DEFAULT 0,
    duplicate_count INT NOT NULL DEFAULT 0,
    unclassified_count INT NOT NULL DEFAULT 0,
    source_hash CHAR(64) NOT NULL DEFAULT '',
    classifier_version VARCHAR(20) NOT NULL DEFAULT 'v1',
    summary_json JSON DEFAULT NULL,
    error_code VARCHAR(60) DEFAULT NULL,
    error_message VARCHAR(500) DEFAULT NULL,
    finished_at DATETIME DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_code_run_source_created (source_kind, created_at),
    INDEX idx_code_run_dates (requested_start_date, requested_end_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _code_daily_snapshots (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    source_kind VARCHAR(20) NOT NULL,
    business_date DATE NOT NULL,
    version_no INT UNSIGNED NOT NULL,
    run_id BIGINT NOT NULL,
    raw_count INT NOT NULL DEFAULT 0,
    total_people INT NOT NULL DEFAULT 0,
    patrol_scan_count INT NOT NULL DEFAULT 0,
    dispatch_hall_scan_count INT NOT NULL DEFAULT 0,
    household_hall_scan_count INT NOT NULL DEFAULT 0,
    social_scan_count INT NOT NULL DEFAULT 0,
    unclassified_scan_count INT NOT NULL DEFAULT 0,
    active_accounts INT NOT NULL DEFAULT 0,
    instruction_count INT NOT NULL DEFAULT 0,
    new_registration_count INT NOT NULL DEFAULT 0,
    excluded_identity_count INT NOT NULL DEFAULT 0,
    duplicate_removed_count INT NOT NULL DEFAULT 0,
    classifier_version VARCHAR(20) NOT NULL DEFAULT 'v1',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_snapshot_version (source_kind, business_date, version_no),
    INDEX idx_code_snapshot_latest (source_kind, business_date, version_no),
    INDEX idx_code_snapshot_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _code_summary_location_labels (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    source_kind VARCHAR(20) NOT NULL,
    location_key VARCHAR(255) NOT NULL,
    display_name VARCHAR(255) NOT NULL DEFAULT '',
    classification VARCHAR(30) NOT NULL,
    enabled TINYINT(1) NOT NULL DEFAULT 1,
    created_by INT DEFAULT NULL,
    updated_by INT DEFAULT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_location_label (source_kind, location_key),
    INDEX idx_code_location_label_class (source_kind, classification, enabled)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _code_summary_location_counts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    run_id BIGINT NOT NULL,
    source_kind VARCHAR(20) NOT NULL,
    business_date DATE NOT NULL,
    location_key VARCHAR(255) NOT NULL,
    display_name VARCHAR(255) NOT NULL DEFAULT '',
    classification VARCHAR(30) NOT NULL DEFAULT 'unclassified',
    row_count INT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_code_location_count (run_id, business_date, location_key),
    INDEX idx_code_location_count_range (source_kind, business_date, location_key),
    INDEX idx_code_location_count_run (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 1. 全链条（15列业务数据，兼容旧版14列腾讯来源表）
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
    电话号码        VARCHAR(500),
    地址            VARCHAR(500),
    登记情况        VARCHAR(500),
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

CREATE TABLE IF NOT EXISTS _daily_task_ledger (
    report_date       DATE NOT NULL,
    parser_type       VARCHAR(50) NOT NULL,
    row_key           VARCHAR(200) NOT NULL,
    source            VARCHAR(20) NOT NULL,
    included          TINYINT(1) NOT NULL DEFAULT 1,
    online_present    TINYINT(1) NOT NULL DEFAULT 1,
    community         VARCHAR(200) DEFAULT '',
    inspector         VARCHAR(100) DEFAULT '',
    task_state        VARCHAR(20) NOT NULL,
    unable_to_verify  TINYINT(1) NOT NULL DEFAULT 0,
    reached_bottom    TINYINT(1) NOT NULL DEFAULT 0,
    effective_workload TINYINT UNSIGNED NOT NULL DEFAULT 0,
    created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (report_date, parser_type, row_key),
    INDEX idx_ledger_type_date (parser_type, report_date),
    INDEX idx_ledger_person (report_date, community, inspector)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS _daily_task_ledger_runs (
    report_date             DATE NOT NULL,
    parser_type             VARCHAR(50) NOT NULL,
    snapshot_table          VARCHAR(100) NOT NULL,
    previous_snapshot_table VARCHAR(100) DEFAULT NULL,
    ledger_rows             INT NOT NULL DEFAULT 0,
    included_rows           INT NOT NULL DEFAULT 0,
    generation_method       VARCHAR(20) NOT NULL DEFAULT 'sync',
    generated_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (report_date, parser_type),
    INDEX idx_ledger_run_date (report_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
