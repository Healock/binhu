#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

readonly action="${1:-}"
readonly script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly program_source="$script_dir/binhu_offsite_backup.py"
readonly systemd_source="$script_dir/systemd"
readonly state_dir="/var/lib/binhu-offsite"
readonly config_dir="/etc/binhu-offsite"
readonly backup_user="binhu-backup"
readonly backup_home="/home/${backup_user}"
readonly restricted_shell="/usr/local/sbin/binhu-backup-shell"

for command_name in install systemctl python3; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Missing required command: ${command_name}" >&2
    exit 1
  }
done
[[ -f "$program_source" ]] || {
  echo "Run the installer from a complete deploy directory." >&2
  exit 1
}

install_program() {
  install -o root -g root -m 0755 "$program_source" /usr/local/sbin/binhu-offsite-backup
  install -d -o root -g root -m 0700 "$state_dir" "$config_dir"
}

case "$action" in
  sender-init)
    [[ $# -eq 1 ]] || {
      echo "Usage: $0 sender-init" >&2
      exit 64
    }
    for command_name in ssh ssh-keygen; do
      command -v "$command_name" >/dev/null 2>&1 || {
        echo "Missing required command: ${command_name}" >&2
        exit 1
      }
    done
    install_program
    if [[ ! -f "$config_dir/id_ed25519" ]]; then
      ssh-keygen -q -t ed25519 -N '' -C binhu-offsite-backup \
        -f "$config_dir/id_ed25519"
    fi
    chmod 0600 "$config_dir/id_ed25519"
    chmod 0644 "$config_dir/id_ed25519.pub"
    install -o root -g root -m 0644 \
      "$systemd_source/binhu-offsite-push.service" \
      /etc/systemd/system/binhu-offsite-push.service
    install -o root -g root -m 0644 \
      "$systemd_source/binhu-offsite-push.timer" \
      /etc/systemd/system/binhu-offsite-push.timer
    systemctl daemon-reload
    echo "Sender prepared. Install this public key on the receiver:"
    cat "$config_dir/id_ed25519.pub"
    ;;

  receiver)
    [[ $# -eq 2 && -f "$2" ]] || {
      echo "Usage: $0 receiver /path/to/sender-key.pub" >&2
      exit 64
    }
    for command_name in useradd usermod openssl; do
      command -v "$command_name" >/dev/null 2>&1 || {
        echo "Missing required command: ${command_name}" >&2
        exit 1
      }
    done
    public_key="$(tr -d '\r\n' < "$2")"
    [[ "$public_key" =~ ^ssh-ed25519[[:space:]]+[A-Za-z0-9+/=]+([[:space:]].*)?$ ]] || {
      echo "The sender key must be an Ed25519 public key." >&2
      exit 64
    }
    install_program
    cat > "$restricted_shell" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

readonly receive_command="/usr/local/sbin/binhu-offsite-backup receive --inbox /var/lib/binhu-offsite/inbox"
if [[ $# -eq 2 && "$1" == "-c" && "$2" == "$receive_command" ]]; then
  exec /usr/local/sbin/binhu-offsite-backup receive \
    --inbox /var/lib/binhu-offsite/inbox
fi
echo "This account only accepts Binhu backup uploads." >&2
exit 126
EOF
    chown root:root "$restricted_shell"
    chmod 0755 "$restricted_shell"
    if ! id "$backup_user" >/dev/null 2>&1; then
      useradd --create-home --home-dir "$backup_home" \
        --shell "$restricted_shell" "$backup_user"
    fi
    random_password_hash="$(openssl rand -hex 48 | openssl passwd -6 -stdin)"
    usermod --password "$random_password_hash" --shell "$restricted_shell" \
      "$backup_user"
    unset random_password_hash
    install -d -o "$backup_user" -g "$backup_user" -m 0700 \
      "$backup_home/.ssh" "$state_dir/inbox"
    install -d -o root -g root -m 0700 "$state_dir/archive"
    chmod 0711 "$state_dir"
    authorized_key="restrict,from=\"10.77.0.2\",command=\"/usr/local/sbin/binhu-offsite-backup receive --inbox $state_dir/inbox\" ${public_key}"
    printf '%s\n' "$authorized_key" > "$backup_home/.ssh/authorized_keys"
    chown "$backup_user:$backup_user" "$backup_home/.ssh/authorized_keys"
    chmod 0600 "$backup_home/.ssh/authorized_keys"
    install -o root -g root -m 0644 \
      "$systemd_source/binhu-offsite-ingest.service" \
      /etc/systemd/system/binhu-offsite-ingest.service
    install -o root -g root -m 0644 \
      "$systemd_source/binhu-offsite-ingest.timer" \
      /etc/systemd/system/binhu-offsite-ingest.timer
    systemctl daemon-reload
    systemctl enable --now binhu-offsite-ingest.timer
    restorecon -Rv "$backup_home/.ssh" /usr/local/sbin/binhu-offsite-backup \
      2>/dev/null || true
    echo "Restricted off-site backup receiver installed."
    ;;

  sender-activate)
    [[ $# -eq 2 && -f "$2" ]] || {
      echo "Usage: $0 sender-activate /path/to/verified-known-hosts" >&2
      exit 64
    }
    install_program
    [[ -f "$config_dir/id_ed25519" ]] || {
      echo "Run sender-init before sender-activate." >&2
      exit 1
    }
    known_hosts="$(tr -d '\r' < "$2")"
    [[ "$known_hosts" =~ \[10\.77\.0\.1\]:51234[[:space:]]+ssh-ed25519[[:space:]]+[A-Za-z0-9+/=]+ ]] || {
      echo "known_hosts must contain the verified WireGuard SSH host key." >&2
      exit 64
    }
    printf '%s\n' "$known_hosts" > "$config_dir/known_hosts"
    chmod 0600 "$config_dir/known_hosts"
    systemctl enable --now binhu-offsite-push.timer
    echo "Off-site backup sender activated."
    ;;

  *)
    echo "Usage: $0 sender-init | receiver KEY.pub | sender-activate KNOWN_HOSTS" >&2
    exit 64
    ;;
esac
