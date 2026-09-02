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
  "${schema_source}" | MYSQL_PWD="${MYSQL_ROOT_PASSWORD:?MYSQL_ROOT_PASSWORD is required}" \
    mysql --protocol=socket -uroot --database="${shadow_database}"
