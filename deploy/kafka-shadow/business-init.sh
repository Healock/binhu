#!/bin/bash
# Fresh-run initializer for the clean Kafka business shadow.
# It is executed by the official MySQL image as root, before Backend starts.
set -Eeuo pipefail

fail() {
  echo "business shadow initialization failed: $1" >&2
  exit 1
}

run_id="${KAFKA_RUN_ID:-}"
online_db="${MYSQL_DATABASE:-}"
archive_db="${BUSINESS_ARCHIVE_DATABASE:-}"
daily_db="${BUSINESS_DAILY_DATABASE:-}"
backend_user="${SHADOW_BACKEND_USER:-}"
backend_password="${SHADOW_BACKEND_PASSWORD:-}"
relay_password="${MYSQL_PASSWORD:-}"
root_password="${MYSQL_ROOT_PASSWORD:-}"

[[ "$run_id" =~ ^KSHADOW-[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || fail "invalid KAFKA_RUN_ID"
for database in "$online_db" "$archive_db" "$daily_db"; do
  [[ "$database" =~ ^KShadow_[A-Za-z0-9_]{1,50}$ ]] || fail "invalid shadow database name"
done
[[ "$online_db" != "$archive_db" && "$online_db" != "$daily_db" && "$archive_db" != "$daily_db" ]] || fail "shadow databases must be distinct"
[[ "$backend_user" =~ ^shadow_backend_[A-Za-z0-9_]{1,32}$ ]] || fail "invalid backend user"
[[ "$backend_password" =~ ^[A-Za-z0-9._-]{24,128}$ ]] || fail "invalid backend credential"
[[ "$relay_password" =~ ^[A-Za-z0-9._-]{24,128}$ ]] || fail "invalid relay credential"
[[ -n "$root_password" ]] || fail "missing root credential"
[[ -f /opt/binhu-backend-init.sql ]] || fail "backend init schema is missing"

mysql_root() {
  MYSQL_PWD="$root_password" mysql --protocol=socket -uroot "$@"
}

mysql_root <<SQL
CREATE DATABASE IF NOT EXISTS $archive_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS $daily_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '$backend_user'@'%' IDENTIFIED BY '$backend_password';
ALTER USER '$backend_user'@'%' IDENTIFIED BY '$backend_password';
GRANT ALL PRIVILEGES ON $online_db.* TO '$backend_user'@'%';
GRANT ALL PRIVILEGES ON $archive_db.* TO '$backend_user'@'%';
GRANT ALL PRIVILEGES ON $daily_db.* TO '$backend_user'@'%';
REVOKE ALL PRIVILEGES, GRANT OPTION FROM 'shadow_derived'@'%';
CREATE TABLE IF NOT EXISTS $online_db._shadow_identity (
  environment VARCHAR(16) NOT NULL PRIMARY KEY,
  run_id VARCHAR(80) NOT NULL,
  database_name VARCHAR(80) NOT NULL,
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE IF NOT EXISTS $archive_db._shadow_identity LIKE $online_db._shadow_identity;
CREATE TABLE IF NOT EXISTS $daily_db._shadow_identity LIKE $online_db._shadow_identity;
CREATE TABLE IF NOT EXISTS $online_db._kafka_event_delivery (
  event_id CHAR(36) PRIMARY KEY,
  run_id VARCHAR(80) NOT NULL,
  event_json JSON NOT NULL,
  payload_sha256 CHAR(64) NOT NULL,
  status VARCHAR(24) NOT NULL DEFAULT 'pending',
  event_attempts INT UNSIGNED NOT NULL DEFAULT 0,
  dlq_attempts INT UNSIGNED NOT NULL DEFAULT 0,
  lease_token CHAR(36) NULL,
  locked_until DATETIME(6) NULL,
  available_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  created_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  published_at DATETIME(6) NULL,
  last_error_code VARCHAR(48) NOT NULL DEFAULT '',
  INDEX pending_delivery (run_id, status, available_at, created_at),
  INDEX expired_delivery (run_id, status, locked_until)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
DELETE FROM $online_db._shadow_identity;
INSERT INTO $online_db._shadow_identity (environment, run_id, database_name)
VALUES ('shadow', '$run_id', '$online_db');
DELETE FROM $archive_db._shadow_identity;
INSERT INTO $archive_db._shadow_identity (environment, run_id, database_name)
VALUES ('shadow', '$run_id', '$archive_db');
DELETE FROM $daily_db._shadow_identity;
INSERT INTO $daily_db._shadow_identity (environment, run_id, database_name)
VALUES ('shadow', '$run_id', '$daily_db');
GRANT SELECT ON $online_db._shadow_identity TO 'shadow_derived'@'%';
GRANT SELECT, INSERT, UPDATE ON $online_db._kafka_event_delivery TO 'shadow_derived'@'%';
FLUSH PRIVILEGES;
SQL

# Reuse the canonical application schema.  All canonical domain names are
# mapped to this run's three databases before execution, so no production
# non-shadow schema name is created by the shadow initializer.
tmp_sql="$(mktemp)"
trap 'rm -f "$tmp_sql"' EXIT
sed \
  -e "s/OnlineDataArchive/${archive_db}/g" \
  -e "s/daily_report/${daily_db}/g" \
  -e "s/PlatformData/${online_db}/g" \
  -e "s/VisitData/${online_db}/g" \
  -e "s/DispatchData/${online_db}/g" \
  -e "s/RegistryData/${online_db}/g" \
  -e "s/WorkflowData/${online_db}/g" \
  -e "s/OnlineData/${online_db}/g" \
  -e "s/'binhu'/'${backend_user}'/g" \
  /opt/binhu-backend-init.sql > "$tmp_sql"
mysql_root < "$tmp_sql"
