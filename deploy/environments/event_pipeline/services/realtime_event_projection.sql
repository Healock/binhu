-- Isolated derived table for the realtime event metadata domain.
-- Applied only by the Dev/Staging event-pipeline migration, never OnlineData.
CREATE TABLE IF NOT EXISTS dev_realtime_event_projection (
  run_id VARCHAR(80) NOT NULL,
  event_type VARCHAR(40) NOT NULL,
  event_count BIGINT NOT NULL,
  latest_revision BIGINT NOT NULL,
  changed_field_count BIGINT NOT NULL,
  PRIMARY KEY (run_id, event_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS dev_realtime_event_projection_python (
  run_id VARCHAR(80) NOT NULL,
  event_type VARCHAR(40) NOT NULL,
  event_count BIGINT NOT NULL,
  latest_revision BIGINT NOT NULL,
  changed_field_count BIGINT NOT NULL,
  PRIMARY KEY (run_id, event_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
