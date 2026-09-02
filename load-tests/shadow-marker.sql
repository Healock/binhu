CREATE TABLE IF NOT EXISTS _shadow_loadtest_marker (
  run_id VARCHAR(32) NOT NULL PRIMARY KEY,
  environment VARCHAR(16) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- The application upgrades this legacy table during startup before the rest
-- of the runtime-managed schema is available. Production already has the
-- table, but a brand-new isolated shadow database does not, so seed the
-- smallest compatible definition here and let the normal startup migration
-- add or adjust any future columns.
CREATE TABLE IF NOT EXISTS _sync_log (
  id INT AUTO_INCREMENT PRIMARY KEY,
  status VARCHAR(20) DEFAULT 'pending',
  trigger_source VARCHAR(20) NOT NULL DEFAULT 'manual',
  requested_by INT DEFAULT NULL,
  phase VARCHAR(30) NOT NULL DEFAULT 'queued',
  current_item VARCHAR(200) DEFAULT NULL,
  total_steps INT NOT NULL DEFAULT 0,
  completed_steps INT NOT NULL DEFAULT 0,
  total_rows INT DEFAULT 0,
  processed_rows INT DEFAULT 0,
  error_message TEXT,
  started_at DATETIME,
  finished_at DATETIME,
  INDEX idx_sync_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO _shadow_loadtest_marker (run_id, environment)
VALUES ('__UNSEEDED__', 'shadow');
-- The placeholder proves that this database was initialized by the isolated
-- shadow Compose project. The seeder replaces it with the exact run id.
