CREATE TABLE IF NOT EXISTS _shadow_loadtest_marker (
  run_id VARCHAR(32) NOT NULL PRIMARY KEY,
  environment VARCHAR(16) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT IGNORE INTO _shadow_loadtest_marker (run_id, environment)
VALUES ('__UNSEEDED__', 'shadow');
-- The placeholder proves that this database was initialized by the isolated
-- shadow Compose project. The seeder replaces it with the exact run id.
