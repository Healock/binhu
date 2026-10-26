CREATE TABLE IF NOT EXISTS dev_process_workflow_projection (
 run_id VARCHAR(80) PRIMARY KEY, claimed BIGINT NOT NULL, assigned BIGINT NOT NULL,
 reviewed BIGINT NOT NULL, saved BIGINT NOT NULL, latest_revision BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS dev_process_workflow_projection_python LIKE dev_process_workflow_projection;
