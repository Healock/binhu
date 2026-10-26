CREATE TABLE IF NOT EXISTS dev_venue_external_projection (
 run_id VARCHAR(80) PRIMARY KEY, venue_visits BIGINT NOT NULL,
 external_updates BIGINT NOT NULL, latest_revision BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS dev_venue_external_projection_python LIKE dev_venue_external_projection;
