#!/bin/sh
set -eu

# Read-only production preflight. It reports blockers but never installs or edits services.
failures=0
warn() { printf 'WARN: %s\n' "$*" >&2; }
fail() { printf 'FAIL: %s\n' "$*" >&2; failures=$((failures + 1)); }
ok() { printf 'OK: %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 2; }

printf '%s\n' '=== Binhu venue cloud preflight (read-only) ==='
printf 'host=%s\n' "$(hostname)"
if [ -r /etc/os-release ]; then . /etc/os-release; printf 'os=%s %s\n' "${NAME:-unknown}" "${VERSION_ID:-unknown}"; fi

if command -v docker >/dev/null 2>&1; then
    ok "docker $(docker --version 2>/dev/null || true)"
    if docker compose version >/dev/null 2>&1; then ok "docker compose available"; else fail "docker compose plugin unavailable"; fi
else
    fail "docker is not installed"
fi

if command -v nginx >/dev/null 2>&1; then
    if nginx -t >/dev/null 2>&1; then ok "nginx configuration syntax"; else fail "nginx -t failed"; fi
else
    warn "nginx binary not found in PATH; check control-panel nginx separately"
fi

root_avail_kb=$(df -Pk / | awk 'NR==2 {print $4}')
if [ "${root_avail_kb:-0}" -lt 8388608 ]; then
    fail "root filesystem has less than 8 GiB free ($(df -hP / | awk 'NR==2 {print $4}'))"
else
    ok "root filesystem free space $(df -hP / | awk 'NR==2 {print $4}')"
fi

for port in 3306 48727; do
    if command -v ss >/dev/null 2>&1 && ss -ltn "sport = :$port" 2>/dev/null | tail -n +2 | grep -q .; then
        warn "TCP port $port is listening; confirm exposure is intentional"
    else
        ok "TCP port $port is not publicly detected as listening"
    fi
done

if [ -d /srv/binhu-updates ]; then ok "/srv/binhu-updates exists"; else warn "/srv/binhu-updates is absent"; fi
if [ -e /srv/binhu-venue ] || [ -e /etc/binhu-venue ]; then
    warn "venue deployment paths already exist; preserve and inspect before installation"
else
    ok "venue deployment paths are unused"
fi

if [ "$failures" -gt 0 ]; then
    printf 'preflight=blocked failures=%s\n' "$failures" >&2
    exit 1
fi
printf '%s\n' 'preflight=passed'
