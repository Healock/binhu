CREATE TABLE IF NOT EXISTS dev_registry_projection (
 run_id VARCHAR(80) NOT NULL, task_id VARCHAR(96) NOT NULL, source_id BIGINT NOT NULL,
 revision BIGINT NOT NULL, registry_revision BIGINT NOT NULL, household_revision BIGINT NOT NULL,
 tag_revision BIGINT NOT NULL, PRIMARY KEY (run_id,task_id,source_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS dev_registry_projection_python LIKE dev_registry_projection;
