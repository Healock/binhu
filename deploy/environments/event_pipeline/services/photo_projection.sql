CREATE TABLE IF NOT EXISTS dev_photo_projection (
 run_id VARCHAR(80) PRIMARY KEY, requested BIGINT NOT NULL,
 attached BIGINT NOT NULL, latest_revision BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS dev_photo_projection_python LIKE dev_photo_projection;
