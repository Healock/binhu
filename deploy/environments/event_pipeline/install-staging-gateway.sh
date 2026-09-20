#!/usr/bin/env bash
set -euo pipefail
umask 077

[[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo 'Run as root.' >&2; exit 1; }
[[ $# -eq 1 && -f "$1" ]] || { echo 'Usage: install-staging-gateway.sh /path/to/staging-deploy-key.pub' >&2; exit 64; }
key="$(tr -d '\r\n' < "$1")"
[[ "$key" =~ ^ssh-ed25519[[:space:]]+[A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || { echo 'Ed25519 key required.' >&2; exit 64; }
command -v useradd >/dev/null && command -v visudo >/dev/null && command -v openssl >/dev/null || { echo 'required tools missing' >&2; exit 1; }

wrapper=/usr/local/bin/binhu-staging-event-pipeline-gateway
install -o root -g root -m 0755 binhu-staging-event-pipeline-gateway.py /usr/local/libexec/binhu-staging-event-pipeline-gateway.py
install -o root -g root -m 0755 binhu-staging-event-pipeline-gateway "$wrapper"
id binhu-staging-deploy >/dev/null 2>&1 || useradd --create-home --home-dir /home/binhu-staging-deploy --shell "$wrapper" binhu-staging-deploy
password_hash="$(openssl rand -hex 48 | openssl passwd -6 -stdin)"
usermod --password "$password_hash" --shell "$wrapper" binhu-staging-deploy
unset password_hash
install -d -o root -g root -m 0700 /var/lib/binhu-staging-event-pipeline/candidates
install -d -o root -g root -m 0700 /var/lib/binhu-staging-event-pipeline/evidence
install -d -o binhu-staging-deploy -g binhu-staging-deploy -m 0700 /home/binhu-staging-deploy/.ssh
printf 'restrict,command="%s" %s\n' "$wrapper" "$key" > /home/binhu-staging-deploy/.ssh/authorized_keys
chown binhu-staging-deploy:binhu-staging-deploy /home/binhu-staging-deploy/.ssh/authorized_keys
chmod 0600 /home/binhu-staging-deploy/.ssh/authorized_keys
cat > /etc/sudoers.d/binhu-staging-event-pipeline <<'EOF'
binhu-staging-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-event-pipeline-gateway.py status
binhu-staging-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-event-pipeline-gateway.py prepare *
binhu-staging-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-event-pipeline-gateway.py measure *
binhu-staging-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-event-pipeline-gateway.py apply *
binhu-staging-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-event-pipeline-gateway.py run *
binhu-staging-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-event-pipeline-gateway.py sample *
binhu-staging-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-event-pipeline-gateway.py rollback *
binhu-staging-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-staging-event-pipeline-gateway.py verify *
EOF
chmod 0440 /etc/sudoers.d/binhu-staging-event-pipeline
visudo -cf /etc/sudoers.d/binhu-staging-event-pipeline >/dev/null
echo 'Staging event-pipeline gateway installed.'
