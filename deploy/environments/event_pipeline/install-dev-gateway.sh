#!/usr/bin/env bash
set -euo pipefail
umask 077

[[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo 'Run as root.' >&2; exit 1; }
[[ $# -eq 1 && -f "$1" ]] || { echo "Usage: $0 /path/to/dev-deploy-key.pub" >&2; exit 64; }
key="$(tr -d '\r\n' < "$1")"
[[ "$key" =~ ^ssh-ed25519[[:space:]]+[A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || { echo 'Ed25519 key required.' >&2; exit 64; }
command -v useradd >/dev/null && command -v visudo >/dev/null && command -v openssl >/dev/null || { echo 'required system tools missing' >&2; exit 1; }
restricted_shell="/usr/local/bin/binhu-dev-event-pipeline-gateway"
install -o root -g root -m 0755 binhu-dev-event-pipeline-gateway.py /usr/local/libexec/binhu-dev-event-pipeline-gateway.py
install -o root -g root -m 0755 binhu-dev-event-pipeline-gateway "$restricted_shell"
id binhu-dev-deploy >/dev/null 2>&1 || useradd --create-home --home-dir /home/binhu-dev-deploy --shell "$restricted_shell" binhu-dev-deploy
# sshd with PAM rejects a locked account before evaluating authorized_keys.
# Give the account an unknown, discarded password and use the gateway itself as
# the login shell, so every SSH path still enters the same fixed command parser.
random_password_hash="$(openssl rand -hex 48 | openssl passwd -6 -stdin)"
usermod --password "$random_password_hash" --shell "$restricted_shell" binhu-dev-deploy
unset random_password_hash
install -d -o root -g root -m 0700 /var/lib/binhu-dev-event-pipeline/candidates
install -d -o binhu-dev-deploy -g binhu-dev-deploy -m 0700 /home/binhu-dev-deploy/.ssh
printf 'restrict,command="/usr/local/bin/binhu-dev-event-pipeline-gateway" %s\n' "$key" > /home/binhu-dev-deploy/.ssh/authorized_keys
chown binhu-dev-deploy:binhu-dev-deploy /home/binhu-dev-deploy/.ssh/authorized_keys
chmod 0600 /home/binhu-dev-deploy/.ssh/authorized_keys
cat > /etc/sudoers.d/binhu-dev-event-pipeline <<'EOF'
binhu-dev-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-event-pipeline-gateway.py status
binhu-dev-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-event-pipeline-gateway.py prepare *
binhu-dev-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-event-pipeline-gateway.py measure *
binhu-dev-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-event-pipeline-gateway.py apply *
binhu-dev-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-event-pipeline-gateway.py accept *
EOF
chmod 0440 /etc/sudoers.d/binhu-dev-event-pipeline
visudo -cf /etc/sudoers.d/binhu-dev-event-pipeline >/dev/null
echo 'Dev event-pipeline gateway installed.'
