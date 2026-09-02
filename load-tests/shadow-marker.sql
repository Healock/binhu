CREATE TABLE IF NOT EXISTS _shadow_loadtest_marker (
  run_id VARCHAR(32) NOT NULL PRIMARY KEY,
  environment VARCHAR(16) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
-- The run-specific row is inserted by the shadow-only seeder after the
-- Compose environment is started.  Docker does not interpolate environment
-- variables inside mounted SQL files, so a literal run id here would be unsafe.
