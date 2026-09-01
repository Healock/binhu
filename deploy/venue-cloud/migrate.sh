#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }
compose_file=${COMPOSE_FILE:-/etc/binhu-venue/docker-compose.yml}
compose_env=${BINHU_VENUE_COMPOSE_ENV:-/srv/binhu-venue/state/current.env}
[ -f "$compose_file" ] || { echo "missing compose file: $compose_file" >&2; exit 1; }
[ -f "$compose_env" ] || { echo "missing image tag environment: $compose_env" >&2; exit 1; }
[ -f /etc/binhu-venue/receiver.env ] || { echo "missing receiver.env" >&2; exit 1; }
[ -f /etc/binhu-venue/mysql.env ] || { echo "missing mysql.env" >&2; exit 1; }

echo "Starting only MySQL for schema migration..."
docker compose --env-file "$compose_env" -f "$compose_file" up -d mysql
echo "Waiting for MySQL health..."
i=0
while [ "$i" -lt 60 ]; do
    status=$(docker compose --env-file "$compose_env" -f "$compose_file" ps --format '{{.Service}} {{.Health}}' 2>/dev/null || true)
    case "$status" in
        *'mysql healthy'*) break ;;
    esac
    i=$((i + 1))
    sleep 2
done
[ "$i" -lt 60 ] || { echo "MySQL did not become healthy" >&2; exit 1; }

echo "Applying idempotent receiver schema..."
docker compose --env-file "$compose_env" -f "$compose_file" run --rm --no-deps receiver python -m app.migrate
echo "Migration complete; no receiver or Nginx changes were made."
