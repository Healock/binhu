CREATE TABLE IF NOT EXISTS dev_visit_attendance_projection (
 run_id VARCHAR(80) PRIMARY KEY, attendance_events BIGINT NOT NULL,
 review_events BIGINT NOT NULL, assignment_events BIGINT NOT NULL,
 latest_revision BIGINT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS dev_visit_attendance_projection_python LIKE dev_visit_attendance_projection;
