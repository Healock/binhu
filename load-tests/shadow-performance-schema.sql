-- Executed only by the guarded shadow bootstrap on its run-scoped database.
-- Keep metadata checks so a schema source that already includes these
-- additive definitions can also initialize without duplicate-column errors.
SET @shadow_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.columns
     WHERE table_schema=DATABASE() AND table_name='_online_projection_jobs'
       AND column_name='available_at') = 0,
    'ALTER TABLE _online_projection_jobs ADD COLUMN available_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP',
    'SELECT 1');
PREPARE shadow_statement FROM @shadow_ddl;
EXECUTE shadow_statement;
DEALLOCATE PREPARE shadow_statement;

SET @shadow_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics
     WHERE table_schema=DATABASE() AND table_name='_online_projection_jobs'
       AND index_name='idx_projection_job_available') = 0,
    'ALTER TABLE _online_projection_jobs ADD INDEX idx_projection_job_available (status,available_at,created_at,id)',
    'SELECT 1');
PREPARE shadow_statement FROM @shadow_ddl;
EXECUTE shadow_statement;
DEALLOCATE PREPARE shadow_statement;

SET @shadow_ddl = IF(
    (SELECT COUNT(*) FROM information_schema.statistics
     WHERE table_schema=DATABASE() AND table_name='_online_source_rows'
       AND index_name='idx_online_source_ref') = 0,
    'ALTER TABLE _online_source_rows ADD INDEX idx_online_source_ref (source_kind,source_ref)',
    'SELECT 1');
PREPARE shadow_statement FROM @shadow_ddl;
EXECUTE shadow_statement;
DEALLOCATE PREPARE shadow_statement;
