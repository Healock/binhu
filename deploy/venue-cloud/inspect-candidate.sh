#!/bin/sh
set -eu

# Read-only inspection for an already installed venue candidate. This script
# never starts containers, writes a ledger, changes Nginx, or reads secrets.
compose_file=/etc/binhu-venue/docker-compose.yml
state_file=/srv/binhu-venue/state/current.env
photos_dir=/srv/binhu-venue/photos
config_dir=/etc/binhu-venue

ok() { printf 'OK %s=%s\n' "$1" "$2"; }
warn() { printf 'WARN %s=%s\n' "$1" "$2"; }
fail() { printf 'FAIL %s=%s\n' "$1" "$2" >&2; failures=$((failures + 1)); }

failures=0
printf '%s\n' '=== Binhu venue cloud candidate inspection (read-only) ==='

if [ -f "$compose_file" ]; then ok compose_file present; else fail compose_file missing; fi
if [ -f "$state_file" ]; then ok state_file present; else warn state_file missing; fi
if [ -d "$photos_dir" ]; then
    mode=$(stat -c '%a' "$photos_dir" 2>/dev/null || printf unknown)
    owner=$(stat -c '%u:%g' "$photos_dir" 2>/dev/null || printf unknown)
    ok photos_dir "$owner:$mode"
else
    fail photos_dir missing
fi
if [ -d "$config_dir" ]; then ok config_dir present; else fail config_dir missing; fi

if command -v docker >/dev/null 2>&1; then
    docker compose -f "$compose_file" ps --format '{{.Name}} {{.State}} {{.Image}}' 2>/dev/null || warn compose_status unavailable
    docker ps --filter 'name=binhu-venue-' --format 'container={{.Names}} status={{.Status}} image={{.Image}}' 2>/dev/null || warn container_inventory unavailable
else
    fail docker missing
fi

if command -v ss >/dev/null 2>&1; then
    if ss -ltn 'sport = :48727' 2>/dev/null | tail -n +2 | grep -q .; then
        ok ingress_loopback "$(ss -ltn 'sport = :48727' 2>/dev/null | tail -n +2 | tr '\n' ' ')"
    else
        warn ingress_loopback not_listening
    fi
fi

if command -v curl >/dev/null 2>&1; then
    ready=$(curl --silent --show-error --max-time 5 --output /dev/null --write-out '%{http_code}' http://127.0.0.1:48727/health/ready 2>/dev/null || printf 000)
    case "$ready" in
        200) ok receiver_ready "$ready" ;;
        *) warn receiver_ready "$ready" ;;
    esac
fi

if command -v nginx >/dev/null 2>&1; then
    if nginx -t >/dev/null 2>&1; then ok nginx_syntax passed; else fail nginx_syntax failed; fi
else
    warn nginx_binary unavailable
fi

if command -v systemctl >/dev/null 2>&1; then
    systemctl list-timers --all --no-legend 2>/dev/null | grep -E 'cert|acme|renew' || warn certificate_timer not_found
else
    warn certificate_timer systemctl_unavailable
fi

if [ "$failures" -gt 0 ]; then
    printf 'inspection=blocked failures=%s\n' "$failures" >&2
    exit 1
fi
printf '%s\n' 'inspection=passed'
