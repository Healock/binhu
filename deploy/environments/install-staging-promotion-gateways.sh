#!/usr/bin/env bash
set -euo pipefail
umask 077

[[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo 'Run as root.' >&2; exit 1; }
[[ $# -eq 3 && -f "$1" && -f "$2" && -f "$3" ]] || { echo 'Usage: install-staging-promotion-gateways.sh APP_KEY.pub DATA_KEY.pub CONTROL_COMMIT' >&2; exit 64; }
app_key="$(tr -d '\r\n' < "$1")"
data_key="$(tr -d '\r\n' < "$2")"
control_commit="$(tr -d '\r\n' < "$3")"
[[ "$app_key" =~ ^ssh-ed25519[[:space:]]+[A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || { echo 'Application Ed25519 key required.' >&2; exit 64; }
[[ "$data_key" =~ ^ssh-ed25519[[:space:]]+[A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || { echo 'Data Ed25519 key required.' >&2; exit 64; }
[[ "$control_commit" =~ ^[0-9a-f]{40}$ ]] || { echo 'Exact main commit required.' >&2; exit 64; }
[[ -f deploy/__init__.py && -f deploy/environments/staging_application_gateway.py && -f deploy/environments/staging_data_gateway.py ]] || { echo 'Controlled source package required.' >&2; exit 64; }
command -v useradd >/dev/null && command -v visudo >/dev/null && command -v openssl >/dev/null || { echo 'required tools missing' >&2; exit 1; }

libexec_root=/usr/local/libexec/binhu-staging-promotion
deploy_root="$libexec_root/deploy"
install -d -o root -g root -m 0755 "$deploy_root"
[[ -z "$(find deploy/environments -type l -print -quit)" ]] || { echo 'Controlled source package contains symlinks.' >&2; exit 64; }
staged_environments="$(mktemp -d "$deploy_root/.environments.new.XXXXXX")"
previous_environments=''
control_commit_path="$libexec_root/control-commit"
control_commit_backup=''
control_commit_replaced=0
deploy_init_path="$deploy_root/__init__.py"
deploy_init_backup=''
deploy_init_replaced=0
installation_complete=0
cleanup_installation() {
  if [[ "$installation_complete" -ne 1 && -n "$previous_environments" && -d "$previous_environments" ]]; then
    rm -rf "$deploy_root/environments"
    mv "$previous_environments" "$deploy_root/environments"
  elif [[ "$installation_complete" -ne 1 && -z "$previous_environments" ]]; then
    rm -rf "$deploy_root/environments"
  fi
  if [[ "$control_commit_replaced" -eq 1 ]]; then
    if [[ -n "$control_commit_backup" && -f "$control_commit_backup" ]]; then
      mv "$control_commit_backup" "$control_commit_path"
    else
      rm -f "$control_commit_path"
    fi
  fi
  if [[ "$deploy_init_replaced" -eq 1 ]]; then
    if [[ -n "$deploy_init_backup" && -f "$deploy_init_backup" ]]; then
      mv "$deploy_init_backup" "$deploy_init_path"
    else
      rm -f "$deploy_init_path"
    fi
  fi
  [[ -z "$staged_environments" ]] || rm -rf "$staged_environments"
  [[ -z "$previous_environments" || ! -d "$previous_environments" ]] || rm -rf "$previous_environments"
  [[ -z "$control_commit_backup" || ! -f "$control_commit_backup" ]] || rm -f "$control_commit_backup"
  [[ -z "$deploy_init_backup" || ! -f "$deploy_init_backup" ]] || rm -f "$deploy_init_backup"
}
trap cleanup_installation EXIT
cp -a deploy/environments/. "$staged_environments/"
find "$staged_environments" -type d -exec chmod 0755 {} +
find "$staged_environments" -type f -exec chmod 0644 {} +
[[ -z "$(find "$staged_environments" -type l -print -quit)" ]] || { echo 'Staged gateway package contains symlinks.' >&2; exit 64; }
/usr/bin/python3 -m compileall -q "$staged_environments"
if [[ -f "$deploy_init_path" ]]; then
  deploy_init_backup="$deploy_root/.deploy-init.previous.$$"
  cp -p "$deploy_init_path" "$deploy_init_backup"
fi
install -o root -g root -m 0644 deploy/__init__.py "$deploy_init_path"
deploy_init_replaced=1
if [[ -d "$deploy_root/environments" ]]; then
  previous_environments="$deploy_root/.environments.previous.$(date -u +%Y%m%dT%H%M%SZ).$$"
  mv "$deploy_root/environments" "$previous_environments"
fi
mv "$staged_environments" "$deploy_root/environments"
staged_environments=''
if [[ -f "$control_commit_path" ]]; then
  control_commit_backup="$libexec_root/.control-commit.previous.$$"
  cp -p "$control_commit_path" "$control_commit_backup"
fi
control_commit_candidate="$libexec_root/.control-commit.candidate.$$"
printf '%s\n' "$control_commit" > "$control_commit_candidate"
chmod 0444 "$control_commit_candidate"
mv "$control_commit_candidate" "$control_commit_path"
control_commit_replaced=1
install -o root -g root -m 0755 deploy/environments/staging-application-root-launcher /usr/local/libexec/binhu-staging-application-gateway
install -o root -g root -m 0755 deploy/environments/staging-data-root-launcher /usr/local/libexec/binhu-staging-data-gateway
install -o root -g root -m 0755 deploy/environments/binhu-staging-application-gateway /usr/local/bin/binhu-staging-application-gateway
install -o root -g root -m 0755 deploy/environments/binhu-staging-data-gateway /usr/local/bin/binhu-staging-data-gateway

configure_account() {
  local account="$1" wrapper="$2" key="$3"
  id "$account" >/dev/null 2>&1 || useradd --create-home --home-dir "/home/$account" --shell "$wrapper" "$account"
  local password_hash
  password_hash="$(openssl rand -hex 48 | openssl passwd -6 -stdin)"
  usermod --password "$password_hash" --shell "$wrapper" "$account"
  unset password_hash
  install -d -o "$account" -g "$account" -m 0700 "/home/$account/.ssh"
  printf 'restrict,command="%s" %s\n' "$wrapper" "$key" > "/home/$account/.ssh/authorized_keys"
  chown "$account:$account" "/home/$account/.ssh/authorized_keys"
  chmod 0600 "/home/$account/.ssh/authorized_keys"
}

configure_account binhu-staging-app-deploy /usr/local/bin/binhu-staging-application-gateway "$app_key"
configure_account binhu-staging-data-deploy /usr/local/bin/binhu-staging-data-gateway "$data_key"
install -d -o root -g root -m 0700 /var/lib/binhu-staging-application/candidates /var/lib/binhu-staging-data-gateway /var/log/binhu-staging-gateways

cat > /etc/sudoers.d/binhu-staging-application <<'EOF'
binhu-staging-app-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-application-gateway status
binhu-staging-app-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-application-gateway prepare *
binhu-staging-app-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-application-gateway measure *
binhu-staging-app-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-application-gateway apply *
binhu-staging-app-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-application-gateway accept *
EOF
cat > /etc/sudoers.d/binhu-staging-data <<'EOF'
binhu-staging-data-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-data-gateway status
binhu-staging-data-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-data-gateway measure
binhu-staging-data-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-data-gateway export approved-sanitized-scope-v1
binhu-staging-data-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-data-gateway create *
binhu-staging-data-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-data-gateway import *
binhu-staging-data-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-data-gateway verify *
binhu-staging-data-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-data-gateway switch *
EOF
chmod 0440 /etc/sudoers.d/binhu-staging-application /etc/sudoers.d/binhu-staging-data
visudo -cf /etc/sudoers.d/binhu-staging-application >/dev/null
visudo -cf /etc/sudoers.d/binhu-staging-data >/dev/null
installed_at="$(date -u +%Y%m%dT%H%M%SZ)"
INSTALL_STAMP="$installed_at" /usr/bin/python3 - <<'PY'
import hashlib,json,os
from pathlib import Path
paths=(Path('/usr/local/bin/binhu-staging-application-gateway'),
       Path('/usr/local/bin/binhu-staging-data-gateway'),
       Path('/etc/sudoers.d/binhu-staging-application'),
       Path('/etc/sudoers.d/binhu-staging-data'))
report={'schema':1,'event':'staging_gateway_authorization_boundary_installed',
        'installed_at':os.environ['INSTALL_STAMP'],
        'accounts':['binhu-staging-app-deploy','binhu-staging-data-deploy'],
        'control_commit':Path('/usr/local/libexec/binhu-staging-promotion/control-commit').read_text().strip(),
        'sha256':{str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}}
target=Path('/var/log/binhu-staging-gateways')/('installation-'+os.environ['INSTALL_STAMP']+'.json')
with target.open('x',encoding='utf-8') as stream:json.dump(report,stream,sort_keys=True)
target.chmod(0o600)
PY
installation_complete=1
echo 'Staging application and sanitized-data gateways installed.'
