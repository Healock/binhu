#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi
if [[ $# -ne 2 || ! -f "$1" ]]; then
  echo "Usage: $0 /path/to/github-actions-deploy-key.pub https://public-platform-address" >&2
  exit 64
fi

readonly public_key_file="$1"
readonly public_health_url="$2"
readonly script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly deploy_user="binhu-deploy"
readonly deploy_home="/home/${deploy_user}"
readonly project_dir="/root/binhu"
readonly state_dir="/var/lib/binhu-deploy"

for command_name in useradd passwd install visudo; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing required command: ${command_name}" >&2
    exit 1
  }
done
[[ -f "$script_dir/binhu-deploy" && -f "$script_dir/binhu-deploy-gateway" ]] || {
  echo "Run the installer from a complete deploy script directory." >&2
  exit 1
}
[[ -d "$project_dir" && -f "$project_dir/docker-compose.yml" ]] || {
  echo "The Binhu production project directory is missing." >&2
  exit 1
}

public_key="$(tr -d '\r\n' < "$public_key_file")"
if [[ ! "$public_key" =~ ^ssh-ed25519[[:space:]]+[A-Za-z0-9+/=]+([[:space:]].*)?$ ]]; then
  echo "The deployment key must be an Ed25519 public key." >&2
  exit 64
fi
if [[ ! "$public_health_url" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~/-]*)?$ ]]; then
  echo "The public platform address must be a simple HTTPS URL." >&2
  exit 64
fi

if ! id "$deploy_user" >/dev/null 2>&1; then
  useradd --create-home --home-dir "$deploy_home" --shell /bin/bash "$deploy_user"
fi
passwd -l "$deploy_user" >/dev/null 2>&1 || true

install -o root -g root -m 0755 "$script_dir/binhu-deploy" /usr/local/sbin/binhu-deploy
install -o root -g root -m 0755 "$script_dir/binhu-deploy-gateway" /usr/local/bin/binhu-deploy-gateway
install -d -o root -g root -m 0750 "$state_dir" "$state_dir/incoming" \
  "$state_dir/work" "$state_dir/releases" "$state_dir/history"
install -d -o "$deploy_user" -g "$deploy_user" -m 0700 "$deploy_home/.ssh"

authorized_key="restrict,command=\"/usr/local/bin/binhu-deploy-gateway\" ${public_key}"
printf '%s\n' "$authorized_key" > "$deploy_home/.ssh/authorized_keys"
chown "$deploy_user:$deploy_user" "$deploy_home/.ssh/authorized_keys"
chmod 0600 "$deploy_home/.ssh/authorized_keys"

cat > /etc/binhu-deploy.conf <<EOF
BINHU_PROJECT_DIR=$project_dir
BINHU_DEPLOY_STATE_DIR=$state_dir
BINHU_DEPLOY_MIN_FREE_KB=1048576
BINHU_DEPLOY_MAX_BUNDLE_BYTES=134217728
BINHU_DEPLOY_PUBLIC_URL=$public_health_url
EOF
chown root:root /etc/binhu-deploy.conf
chmod 0644 /etc/binhu-deploy.conf

cat > /etc/sudoers.d/binhu-deploy <<'EOF'
binhu-deploy ALL=(root) NOPASSWD: /usr/local/sbin/binhu-deploy *
EOF
chown root:root /etc/sudoers.d/binhu-deploy
chmod 0440 /etc/sudoers.d/binhu-deploy
visudo -cf /etc/sudoers.d/binhu-deploy >/dev/null

restorecon -Rv "$deploy_home/.ssh" /usr/local/sbin/binhu-deploy \
  /usr/local/bin/binhu-deploy-gateway 2>/dev/null || true

echo "Binhu deployment gateway installed for ${deploy_user}."
