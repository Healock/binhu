#!/usr/bin/env bash
set -euo pipefail

shadow_database="${MYSQL_DATABASE:?MYSQL_DATABASE is required}"
if [[ ! "${shadow_database}" =~ ^LoadTest_[A-Za-z0-9_]+$ ]]; then
  echo "Refusing to initialize a non-shadow database: ${shadow_database}" >&2
  exit 1
fi

schema_source="/shadow-schema/backend-init.sql"
if [[ ! -r "${schema_source}" ]]; then
  echo "Missing backend schema source: ${schema_source}" >&2
  exit 1
fi

MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD is required}" \
  mysql --protocol=socket -uroot \
  -e "ALTER DATABASE \`${shadow_database}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"

# The production initializer uses separate logical databases. The isolated
# shadow environment deliberately routes every domain to one run-scoped
# database, so rewrite only the known database identifiers and remove grants
# that target the production application user. The script runs only inside the
# official MySQL initialization container and connects through its local socket.
sed \
  -e "s/OnlineDataArchive/${shadow_database}/g" \
  -e "s/daily_report/${shadow_database}/g" \
  -e "s/PlatformData/${shadow_database}/g" \
  -e "s/VisitData/${shadow_database}/g" \
  -e "s/DispatchData/${shadow_database}/g" \
  -e "s/RegistryData/${shadow_database}/g" \
  -e "s/WorkflowData/${shadow_database}/g" \
  -e "s/OnlineData/${shadow_database}/g" \
  -e '/^GRANT ALL PRIVILEGES/d' \
  -e '/^FLUSH PRIVILEGES/d' \
  "${schema_source}" | MYSQL_PWD="${MYSQL_ROOT_PASSWORD}" \
    mysql --protocol=socket -uroot --database="${shadow_database}"

# Keep the observability metadata contract explicit.  Older init.sql snapshots
# and pre-existing named volumes may not contain this table even though the
# application starts successfully; create it in the run-scoped database before
# any shadow traffic is admitted.
MYSQL_PWD="${MYSQL_ROOT_PASSWORD}" mysql --protocol=socket -uroot \
  --database="${shadow_database}" <<'SQL'
CREATE TABLE IF NOT EXISTS _daily_report_meta (
    id INT AUTO_INCREMENT PRIMARY KEY,
    table_name VARCHAR(100) NOT NULL,
    report_date DATE NOT NULL,
    parser_type VARCHAR(50) NOT NULL,
    generation_method VARCHAR(20) DEFAULT 'auto',
    generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_table_name (table_name),
    INDEX idx_date (report_date),
    INDEX idx_type (parser_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
SQL
