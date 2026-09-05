SET 'execution.checkpointing.interval' = '10 s';
CREATE TABLE cdc_events (id BIGINT, payload STRING, revision INT, updated_at TIMESTAMP(3), PRIMARY KEY (id) NOT ENFORCED) WITH ('connector'='mysql-cdc','hostname'='mysql','port'='3306','username'='flink_cdc','password'='__CDC_PASSWORD__','database-name'='FlinkPOC_20260906','table-name'='synthetic_events','server-id'='11001','scan.startup.mode'='initial');
CREATE TABLE print_sink (id BIGINT, payload STRING, revision INT, updated_at TIMESTAMP(3)) WITH ('connector'='print');
INSERT INTO print_sink SELECT id,payload,revision,updated_at FROM cdc_events;
