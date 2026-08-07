#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi
if [[ $# -lt 2 ]]; then
  echo "Usage: $0 new ACTIVE_NGINX_FILE" >&2
  echo "   or: $0 old ACTIVE_NGINX_FILE SERVER_NAME" >&2
  exit 64
fi

readonly role="$1"
readonly active_file="$(readlink -m "$2")"
readonly server_name="${3:-}"
readonly script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly repository_root="$(cd "$script_dir/.." && pwd)"
readonly template_dir="$repository_root/nginx/migration"
readonly profile_dir="/etc/nginx/binhu-profiles"

[[ "$active_file" == /etc/nginx/* || "$active_file" == /www/server/panel/vhost/nginx/* ]] || {
  echo "Active Nginx file is outside an approved directory." >&2
  exit 64
}
[[ -f "$active_file" ]] || {
  echo "Active Nginx file does not exist." >&2
  exit 64
}
[[ -f "$script_dir/binhu-nginx-profile" ]] || {
  echo "Profile switcher is missing." >&2
  exit 1
}
for command_name in install nginx systemctl; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing required command: ${command_name}" >&2
    exit 1
  }
done

install -d -o root -g root -m 0755 "$profile_dir"
install -o root -g root -m 0755 \
  "$script_dir/binhu-nginx-profile" /usr/local/sbin/binhu-nginx-profile

case "$role" in
  new)
    [[ $# -eq 2 ]] || exit 64
    install -d -o root -g root -m 0755 /etc/nginx/snippets
    install -o root -g root -m 0644 \
      "$template_dir/new-app-locations.conf" \
      /etc/nginx/snippets/binhu-app-locations.conf
    install -o root -g root -m 0644 \
      "$template_dir/new-maintenance.conf" "$profile_dir/new-maintenance.conf"
    install -o root -g root -m 0644 \
      "$template_dir/new-production.conf" "$profile_dir/new-production.conf"
    install -o root -g root -m 0644 "$active_file" "$profile_dir/new-legacy.conf"
    install -d -o root -g root -m 0755 /etc/systemd/system/nginx.service.d
    cat > /etc/systemd/system/nginx.service.d/binhu-wireguard.conf <<'EOF'
[Unit]
Wants=wg-quick@wg0.service
After=wg-quick@wg0.service
EOF
    ;;
  old)
    [[ $# -eq 3 && "$server_name" =~ ^[A-Za-z0-9.-]+$ ]] || {
      echo "A simple old-server HTTPS name is required." >&2
      exit 64
    }
    install -o root -g root -m 0644 "$active_file" "$profile_dir/old-legacy.conf"
    sed "s/__BINHU_SERVER_NAME__/${server_name}/g" \
      "$template_dir/old-maintenance.conf.template" \
      > "$profile_dir/.old-maintenance.conf"
    sed "s/__BINHU_SERVER_NAME__/${server_name}/g" \
      "$template_dir/old-proxy.conf.template" \
      > "$profile_dir/.old-proxy.conf"
    install -o root -g root -m 0644 \
      "$profile_dir/.old-maintenance.conf" "$profile_dir/old-maintenance.conf"
    install -o root -g root -m 0644 \
      "$profile_dir/.old-proxy.conf" "$profile_dir/old-proxy.conf"
    rm -f -- "$profile_dir/.old-maintenance.conf" "$profile_dir/.old-proxy.conf"
    ;;
  *)
    echo "Role must be new or old." >&2
    exit 64
    ;;
esac

cat > /etc/binhu-nginx-profile.conf <<EOF
BINHU_NGINX_ACTIVE_FILE=$active_file
BINHU_NGINX_PROFILE_DIR=$profile_dir
EOF
chown root:root /etc/binhu-nginx-profile.conf
chmod 0644 /etc/binhu-nginx-profile.conf
systemctl daemon-reload
nginx -t
echo "Installed ${role} Nginx profiles without changing the active profile."
