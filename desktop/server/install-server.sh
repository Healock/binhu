#!/bin/sh
set -eu

MODE=${1:---check}
PUBLIC_KEY=${2:-}
ROOT=/srv/binhu-updates
IP_ADDRESS=47.100.44.36

find_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}

find_certbot() {
  for candidate in /usr/local/bin/certbot /snap/bin/certbot /usr/bin/certbot; do
    [ -x "$candidate" ] || continue
    printf '%s\n' "$candidate"
    return 0
  done
  command -v certbot 2>/dev/null || return 1
}

find_android_tool() {
  name=$1
  command -v "$name" 2>/dev/null && return 0
  for candidate in /opt/android-sdk/build-tools/*/"$name"; do
    [ -x "$candidate" ] || continue
    printf '%s\n' "$candidate"
  done | sort -Vr | head -n 1
}

reject_crlf() {
  carriage_return=$(printf '\r')
  for path in "$@"; do
    [ -f "$path" ] || { echo "Required server asset is missing: $path" >&2; exit 1; }
    if LC_ALL=C grep -q "$carriage_return" "$path"; then
      echo "Refusing to install a Linux asset with CRLF line endings: $path" >&2
      exit 1
    fi
  done
}

reject_crlf \
  binhu-renew-ip-certificate \
  binhu-obtain-ip-certificate \
  binhu-record-ip-certificate-failure \
  systemd/binhu-ip-cert-renew.service \
  systemd/binhu-ip-cert-renew-failed.service \
  systemd/binhu-ip-cert-renew.timer \
  nginx/binhu-updates-acme.inc

[ -r /etc/os-release ] || { echo '/etc/os-release is required' >&2; exit 1; }
# shellcheck source=/dev/null
. /etc/os-release
printf 'OS: %s %s\n' "$ID" "$VERSION_ID"
df -h /srv 2>/dev/null || df -h /
nginx -T >/tmp/binhu-nginx-current.txt 2>&1 || true
printf 'Nginx configuration captured in /tmp/binhu-nginx-current.txt\n'
ss -ltn | grep -E ':(80|443|51234)[[:space:]]' || true

NGINX_LAYOUT=unknown
WEB_GROUP=
if [ -d /www/server/panel/vhost/nginx ] && [ -f "/www/server/panel/vhost/nginx/$IP_ADDRESS.conf" ]; then
  NGINX_LAYOUT=bt-panel
  WEB_GROUP=www
elif [ -d /etc/nginx/sites-available ] && [ -d /etc/nginx/sites-enabled ]; then
  NGINX_LAYOUT=debian
  WEB_GROUP=www-data
fi
printf 'Nginx layout: %s\n' "$NGINX_LAYOUT"
PYTHON=$(find_python || true)
printf 'Python 3.9+: %s\n' "${PYTHON:-not found}"
CERTBOT=$(find_certbot || true)
printf 'Certbot: %s\n' "${CERTBOT:-not found}"
AAPT2=$(find_android_tool aapt2 || true)
APKSIGNER=$(find_android_tool apksigner || true)
printf 'Android APK verifier: aapt2=%s apksigner=%s\n' "${AAPT2:-not found}" "${APKSIGNER:-not found}"

if [ "$MODE" != "--install" ]; then
  printf 'Inspection only. Re-run as root with --install and the SSH public key.\n'
  exit 0
fi
[ "$(id -u)" -eq 0 ] || { echo 'root is required' >&2; exit 1; }
[ -n "$PUBLIC_KEY" ] || { echo 'SSH public key is required' >&2; exit 1; }
[ -n "$PYTHON" ] || { echo 'Python 3.9 or newer is required.' >&2; exit 1; }
[ -n "$CERTBOT" ] || { echo 'Certbot is required.' >&2; exit 1; }
[ -n "$AAPT2" ] || { echo 'aapt2 is required for Android release verification.' >&2; exit 1; }
[ -n "$APKSIGNER" ] || { echo 'apksigner is required for Android release verification.' >&2; exit 1; }
"$CERTBOT" --version 2>&1 | grep -E 'certbot (5\.[4-9]|[6-9]\.|[1-9][0-9]+\.)' >/dev/null || {
  echo 'Certbot 5.4 or newer is required for IP webroot certificates.' >&2; exit 1;
}
case "$NGINX_LAYOUT" in
  debian|bt-panel) ;;
  *) echo 'Unsupported Nginx layout. Debian sites-enabled or BT Panel is required.' >&2; exit 1 ;;
esac

id binhu-update-publish >/dev/null 2>&1 || useradd --system --home-dir /var/lib/binhu-update-publish --create-home --shell /bin/sh binhu-update-publish
usermod --shell /bin/sh binhu-update-publish
install -d -o binhu-update-publish -g binhu-update-publish -m 0750 "$ROOT/incoming" "$ROOT/state" "$ROOT/archive"
install -d -o binhu-update-publish -g "$WEB_GROUP" -m 0750 "$ROOT/public/win7-x64" "$ROOT/public/win10-x64" "$ROOT/public/android-arm64"
for platform in win7-x64 win10-x64 android-arm64; do
  policy="$ROOT/public/$platform/policy.stable.json"
  if [ ! -e "$policy" ]; then
    printf '{"minimumVersion":"0.0.0"}\n' > "$policy"
    chown "binhu-update-publish:$WEB_GROUP" "$policy"
    chmod 0640 "$policy"
  fi
done
install -d -o root -g root -m 0755 /usr/local/libexec /usr/local/sbin /var/www/binhu-acme
install -o root -g root -m 0644 binhu_update_gateway.py /usr/local/libexec/binhu-update-gateway.py
cat > /usr/local/libexec/binhu-update-gateway <<EOF
#!/bin/sh
exec "$PYTHON" /usr/local/libexec/binhu-update-gateway.py "\$@"
EOF
chown root:root /usr/local/libexec/binhu-update-gateway
chmod 0755 /usr/local/libexec/binhu-update-gateway
install -o root -g root -m 0755 binhu-renew-ip-certificate /usr/local/sbin/binhu-renew-ip-certificate
install -o root -g root -m 0755 binhu-obtain-ip-certificate /usr/local/sbin/binhu-obtain-ip-certificate
install -o root -g root -m 0755 binhu-record-ip-certificate-failure /usr/local/sbin/binhu-record-ip-certificate-failure
install -o root -g root -m 0644 systemd/binhu-ip-cert-renew.service /etc/systemd/system/binhu-ip-cert-renew.service
install -o root -g root -m 0644 systemd/binhu-ip-cert-renew-failed.service /etc/systemd/system/binhu-ip-cert-renew-failed.service
install -o root -g root -m 0644 systemd/binhu-ip-cert-renew.timer /etc/systemd/system/binhu-ip-cert-renew.timer

ssh_dir=/var/lib/binhu-update-publish/.ssh
install -d -o binhu-update-publish -g binhu-update-publish -m 0700 "$ssh_dir"
printf 'restrict,command="/usr/local/libexec/binhu-update-gateway" %s\n' "$PUBLIC_KEY" > "$ssh_dir/authorized_keys"
chown binhu-update-publish:binhu-update-publish "$ssh_dir/authorized_keys"
chmod 0600 "$ssh_dir/authorized_keys"

if [ "$NGINX_LAYOUT" = debian ]; then
  for config in binhu-updates binhu-updates-http; do
    source="nginx/$config.conf"
    target="/etc/nginx/sites-available/$config"
    if [ -e "$target" ] && ! cmp -s "$source" "$target"; then
      echo "Existing $target differs; refusing to overwrite." >&2
      exit 1
    fi
    install -o root -g root -m 0644 "$source" "$target"
  done
  ln -sfn /etc/nginx/sites-available/binhu-updates-http /etc/nginx/sites-enabled/binhu-updates
else
  BT_DIR=/www/server/panel/vhost/nginx
  BT_VHOST="$BT_DIR/$IP_ADDRESS.conf"
  BT_INCLUDE="$BT_DIR/binhu-updates-acme.inc"
  BT_HTTPS="$BT_DIR/binhu-updates.conf"
  install -o root -g root -m 0644 nginx/binhu-updates-acme.inc "$BT_INCLUDE"
  install -o root -g root -m 0644 nginx/binhu-updates-bt.conf "$BT_HTTPS.disabled"
  install -d -o root -g root -m 0700 "$ROOT/state/nginx-backups"
  if ! grep -Fq 'binhu-updates-acme.inc' "$BT_VHOST"; then
    backup="$ROOT/state/nginx-backups/$IP_ADDRESS.conf.before-binhu"
    [ -e "$backup" ] || cp -a "$BT_VHOST" "$backup"
    awk -v include_line="    include $BT_INCLUDE;" -v ip="$IP_ADDRESS" '
      !inserted && $0 ~ "^[[:space:]]*server_name[[:space:]]+" ip "[[:space:]]*;" {
        print
        print ""
        print "    # Binhu desktop update certificate validation"
        print include_line
        inserted=1
        next
      }
      { print }
      END { if (!inserted) exit 42 }
    ' "$BT_VHOST" > "$BT_VHOST.binhu-new" || {
      rm -f "$BT_VHOST.binhu-new"
      echo "Unable to add the ACME include to $BT_VHOST" >&2
      exit 1
    }
    chown --reference="$BT_VHOST" "$BT_VHOST.binhu-new"
    chmod --reference="$BT_VHOST" "$BT_VHOST.binhu-new"
    mv "$BT_VHOST.binhu-new" "$BT_VHOST"
  fi
fi
nginx -t
systemctl daemon-reload
systemctl reload nginx
echo 'HTTP validation endpoint installed. Run binhu-obtain-ip-certificate <acme-email> to enable HTTPS.'
