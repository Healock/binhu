CREATE TABLE IF NOT EXISTS dev_police_dispatch_projection (
 run_id VARCHAR(80) NOT NULL, business_date DATE NOT NULL,
 dispatch_events BIGINT NOT NULL, assignment_events BIGINT NOT NULL,
 review_events BIGINT NOT NULL, latest_revision BIGINT NOT NULL,
 PRIMARY KEY (run_id,business_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS dev_police_dispatch_projection_python LIKE dev_police_dispatch_projection;
