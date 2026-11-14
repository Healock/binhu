#!/usr/bin/env bash
set -euo pipefail
umask 077

[[ "${EUID:-$(id -u)}" -eq 0 ]] || { echo 'Run as root.' >&2; exit 1; }
[[ $# -eq 2 && -f "$1" && -f "$2" ]] || { echo "Usage: $0 /path/to/dev-deploy-key.pub /path/to/event-pipeline-source.tar.gz" >&2; exit 64; }
key="$(tr -d '\r\n' < "$1")"
[[ "$key" =~ ^ssh-ed25519[[:space:]]+[A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || { echo 'Ed25519 key required.' >&2; exit 64; }
command -v useradd >/dev/null && command -v visudo >/dev/null && command -v openssl >/dev/null && command -v systemd-run >/dev/null && command -v tar >/dev/null || { echo 'required system tools missing' >&2; exit 1; }
source_stage="$(mktemp -d /usr/local/libexec/.binhu-dev-event-pipeline-source.XXXXXX)"
trap 'rm -rf "$source_stage"' EXIT
python3 - "$2" <<'PY'
import sys, tarfile
from pathlib import PurePosixPath
with tarfile.open(sys.argv[1], "r:gz") as archive:
    members = archive.getmembers()
    if not members or any(
        (not member.isfile() and not member.isdir())
        or member.issym() or member.islnk()
        or PurePosixPath(member.name).is_absolute()
        or ".." in PurePosixPath(member.name).parts
        or not (member.name == "deploy/environments/event_pipeline"
                or member.name.startswith("deploy/environments/event_pipeline/"))
        for member in members
    ):
        raise SystemExit(64)
PY
[[ $? -eq 0 ]] || { echo 'invalid event-pipeline source archive' >&2; exit 64; }
tar -xzf "$2" -C "$source_stage"
[[ -f "$source_stage/deploy/environments/event_pipeline/control.py" && -f "$source_stage/deploy/environments/event_pipeline/flink_submission.py" ]] || { echo 'incomplete event-pipeline source archive' >&2; exit 64; }
source_target="/usr/local/libexec/binhu-dev-event-pipeline-source"
source_previous="${source_target}.previous.$(date -u +%Y%m%dT%H%M%SZ)"
if [[ -e "$source_target" ]]; then
  mv "$source_target" "$source_previous"
fi
mv "$source_stage/deploy" "$source_target"
chown -R root:root "$source_target"
chmod -R go-w "$source_target"
restricted_shell="/usr/local/bin/binhu-dev-event-pipeline-gateway"
install -o root -g root -m 0755 binhu-dev-event-pipeline-gateway.py /usr/local/libexec/binhu-dev-event-pipeline-gateway.py
install -o root -g root -m 0755 observation_control.py /usr/local/libexec/binhu-dev-event-pipeline-observation.py
install -o root -g root -m 0755 dev_image_source_diagnostic.py /usr/local/libexec/binhu-dev-image-source-diagnostic.py
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
binhu-dev-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-event-pipeline-gateway.py stop *
binhu-dev-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-event-pipeline-gateway.py pause *
binhu-dev-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-event-pipeline-gateway.py resume *
binhu-dev-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-event-pipeline-gateway.py observe-start *
binhu-dev-deploy ALL=(root) NOPASSWD: /usr/local/libexec/binhu-dev-event-pipeline-gateway.py observe-status *
EOF
chmod 0440 /etc/sudoers.d/binhu-dev-event-pipeline
visudo -cf /etc/sudoers.d/binhu-dev-event-pipeline >/dev/null
echo 'Dev event-pipeline gateway installed.'
