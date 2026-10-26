CREATE TABLE IF NOT EXISTS dev_qmf_projection (
 run_id VARCHAR(80) PRIMARY KEY, acquisition_events BIGINT NOT NULL,
 registration_events BIGINT NOT NULL, latest_revision BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS dev_qmf_projection_python LIKE dev_qmf_projection;
