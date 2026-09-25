#!/usr/bin/env bash
set -euo pipefail
umask 077
[[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo 'Run as root.' >&2; exit 1; }
[[ $# -eq 2 && -f "$1" && -f "$2" ]] || { echo 'Usage: install-dev-application-gateway.sh DEPLOY_KEY.pub CONTROL_COMMIT' >&2; exit 64; }
key="$(tr -d '\r\n' < "$1")"
control_commit="$(tr -d '\r\n' < "$2")"
[[ "$key" =~ ^ssh-ed25519[[:space:]]+[A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || { echo 'Application Ed25519 key required.' >&2; exit 64; }
[[ "$control_commit" =~ ^[0-9a-f]{40}$ ]] || { echo 'Exact main commit required.' >&2; exit 64; }
[[ -f deploy/__init__.py && -f deploy/environments/development_application_gateway.py ]] || { echo 'Controlled source package required.' >&2; exit 64; }
command -v useradd >/dev/null && command -v visudo >/dev/null && command -v openssl >/dev/null || { echo 'required tools missing' >&2; exit 1; }
install -d -o root -g root -m 0755 /usr/local/libexec/binhu-dev-application/deploy/environments
install -o root -g root -m 0644 deploy/__init__.py /usr/local/libexec/binhu-dev-application/deploy/__init__.py
for module in artifact.py database_identity.py development_application_gateway.py image.py runtime.py update.py; do
  install -o root -g root -m 0644 \
    "deploy/environments/$module" \
    "/usr/local/libexec/binhu-dev-application/deploy/environments/$module"
done
install -o root -g root -m 0755 deploy/environments/dev-application-root-launcher /usr/local/libexec/binhu-dev-application-gateway
install -o root -g root -m 0755 deploy/environments/binhu-dev-application-gateway /usr/local/bin/binhu-dev-application-gateway
printf '%s\n' "$control_commit" > /usr/local/libexec/binhu-dev-application/control-commit
chmod 0444 /usr/local/libexec/binhu-dev-application/control-commit
id binhu-dev-app-deploy >/dev/null 2>&1 || useradd --create-home --home-dir /home/binhu-dev-app-deploy --shell /usr/local/bin/binhu-dev-application-gateway binhu-dev-app-deploy
password_hash="$(openssl rand -hex 48 | openssl passwd -6 -stdin)"
usermod --password "$password_hash" --shell /usr/local/bin/binhu-dev-application-gateway binhu-dev-app-deploy
unset password_hash
install -d -o binhu-dev-app-deploy -g binhu-dev-app-deploy -m 0700 /home/binhu-dev-app-deploy/.ssh
printf 'restrict,command="/usr/local/bin/binhu-dev-application-gateway" %s\n' "$key" > /home/binhu-dev-app-deploy/.ssh/authorized_keys
chown binhu-dev-app-deploy:binhu-dev-app-deploy /home/binhu-dev-app-deploy/.ssh/authorized_keys
chmod 0600 /home/binhu-dev-app-deploy/.ssh/authorized_keys
install -d -o root -g root -m 0700 /var/lib/binhu-dev-application /var/lib/binhu-dev-application/candidates /var/log/binhu-dev-application-gateway
chmod 0700 /var/lib/binhu-dev-application /var/lib/binhu-dev-application/candidates /var/log/binhu-dev-application-gateway
cat > /etc/sudoers.d/binhu-dev-application <<'EOF'
binhu-dev-app-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-application-gateway status
binhu-dev-app-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-application-gateway prepare *
binhu-dev-app-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-application-gateway measure *
binhu-dev-app-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-application-gateway reconcile *
binhu-dev-app-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-application-gateway apply *
binhu-dev-app-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-application-gateway accept *
EOF
chmod 0440 /etc/sudoers.d/binhu-dev-application
visudo -cf /etc/sudoers.d/binhu-dev-application >/dev/null
install_stamp="$(date -u +%Y%m%dT%H%M%SZ)"
INSTALL_STAMP="$install_stamp" /usr/bin/python3 - <<'PY'
import hashlib,json,os
from pathlib import Path
paths=(Path('/usr/local/bin/binhu-dev-application-gateway'),Path('/usr/local/libexec/binhu-dev-application-gateway'),Path('/usr/local/libexec/binhu-dev-application/deploy/environments/development_application_gateway.py'),Path('/etc/sudoers.d/binhu-dev-application'))
report={'schema':1,'event':'dev_application_gateway_installed','installed_at':os.environ['INSTALL_STAMP'],'account':'binhu-dev-app-deploy','control_commit':Path('/usr/local/libexec/binhu-dev-application/control-commit').read_text().strip(),'sha256':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
target=Path('/var/log/binhu-dev-application-gateway')/('installation-'+os.environ['INSTALL_STAMP']+'.json')
target.write_text(json.dumps(report,sort_keys=True),encoding='utf-8'); target.chmod(0o600)
PY
echo 'Dev application gateway installed.'
