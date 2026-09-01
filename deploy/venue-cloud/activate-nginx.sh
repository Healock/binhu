#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "root is required" >&2; exit 1; }

bt_dir=/www/server/panel/vhost/nginx
active="$bt_dir/binhu-updates.conf"
duplicate="$bt_dir/bt_proxy.conf"
disabled="$bt_dir/bt_proxy.conf.disabled-binhu-venue"
http_source=/etc/binhu-venue/nginx-http-context.conf
server_source=/etc/binhu-venue/nginx-server-locations.conf
http_target="$bt_dir/00-binhu-venue-http.conf"
state_root=/srv/binhu-venue/state/nginx-backups
python=/usr/bin/python3.11

for command in nginx curl "$python"; do
  command -v "$command" >/dev/null 2>&1 || { echo "missing command: $command" >&2; exit 1; }
done
for path in "$active" "$http_source" "$server_source" /etc/binhu-venue/mtls-ca.pem; do
  [ -f "$path" ] || { echo "required file is missing: $path" >&2; exit 1; }
  [ ! -L "$path" ] || { echo "symbolic links are forbidden: $path" >&2; exit 1; }
done

curl --fail --silent --show-error http://127.0.0.1:48727/health/ready >/dev/null

stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup="$state_root/$stamp"
install -d -o root -g root -m 0700 "$backup"
cp -a "$active" "$backup/binhu-updates.conf"
[ ! -e "$http_target" ] || cp -a "$http_target" "$backup/00-binhu-venue-http.conf"
[ ! -e "$duplicate" ] || cp -a "$duplicate" "$backup/bt_proxy.conf"
[ ! -e "$disabled" ] || cp -a "$disabled" "$backup/bt_proxy.conf.disabled-binhu-venue"

restore() {
  cp -a "$backup/binhu-updates.conf" "$active"
  if [ -e "$backup/00-binhu-venue-http.conf" ]; then
    cp -a "$backup/00-binhu-venue-http.conf" "$http_target"
  else
    rm -f "$http_target"
  fi
  if [ -e "$backup/bt_proxy.conf" ]; then
    cp -a "$backup/bt_proxy.conf" "$duplicate"
  else
    rm -f "$duplicate"
  fi
  if [ -e "$backup/bt_proxy.conf.disabled-binhu-venue" ]; then
    cp -a "$backup/bt_proxy.conf.disabled-binhu-venue" "$disabled"
  else
    rm -f "$disabled"
  fi
  nginx -t >/dev/null 2>&1 && systemctl reload nginx || true
}

success=false
trap 'if [ "$success" != true ]; then restore; fi' EXIT HUP INT TERM

install -o root -g root -m 0644 "$http_source" "$http_target.new"
mv "$http_target.new" "$http_target"

if [ -e "$duplicate" ]; then
  [ ! -e "$disabled" ] || { echo "duplicate disabled Nginx file already exists: $disabled" >&2; exit 1; }
  mv "$duplicate" "$disabled"
fi

"$python" - "$active" "$server_source" <<'PY'
from pathlib import Path
import sys

active = Path(sys.argv[1])
include_source = Path(sys.argv[2])
include_line = f"    include {include_source};"
text = active.read_text(encoding="utf-8")
if include_line not in text:
    marker = "    location / { return 404; }"
    if text.count(marker) != 1:
        raise SystemExit("the final 404 location was not found exactly once")
    text = text.replace(marker, f"{include_line}\n\n{marker}", 1)
temporary = active.with_suffix(".conf.binhu-new")
temporary.write_text(text, encoding="utf-8")
PY
chown --reference="$active" "$active.binhu-new"
chmod --reference="$active" "$active.binhu-new"
mv "$active.binhu-new" "$active"

test_output=$(nginx -t 2>&1)
printf '%s\n' "$test_output"
if printf '%s\n' "$test_output" | grep -F 'conflicting server name "47.100.44.36"' >/dev/null; then
  echo "duplicate IP virtual host warning still exists" >&2
  exit 1
fi
systemctl reload nginx

curl --fail --silent --show-error https://47.100.44.36/updates/win10-x64/releases.stable.json >/dev/null
internal_code=""
i=0
while [ "$i" -lt 10 ]; do
  internal_code=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' https://47.100.44.36/api/internal/status || true)
  [ "$internal_code" = 403 ] && break
  i=$((i + 1))
  sleep 1
done
[ "$internal_code" = 403 ] || { echo "internal API without mTLS returned $internal_code" >&2; exit 1; }
public_code=$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' https://47.100.44.36/venue/not-a-real-token)
case "$public_code" in 502|503|504) echo "venue proxy is unavailable: $public_code" >&2; exit 1 ;; esac

success=true
trap - EXIT HUP INT TERM
echo "Nginx venue routes activated; backup: $backup"
