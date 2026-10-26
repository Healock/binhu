CREATE TABLE IF NOT EXISTS dev_daily_ledger_projection (
 run_id VARCHAR(80) NOT NULL, business_date DATE NOT NULL,
 event_count BIGINT NOT NULL, created_count BIGINT NOT NULL, saved_count BIGINT NOT NULL,
 completed_count BIGINT NOT NULL, deleted_count BIGINT NOT NULL, latest_revision BIGINT NOT NULL,
 PRIMARY KEY (run_id,business_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS dev_daily_ledger_projection_python LIKE dev_daily_ledger_projection;
