#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi
if [[ $# -lt 3 ]]; then
  echo "Usage: $0 server PRIVATE_KEY_FILE PEER_PUBLIC_KEY" >&2
  echo "   or: $0 client PRIVATE_KEY_FILE PEER_PUBLIC_KEY OLD_CLOUD_HOST" >&2
  exit 64
fi

readonly role="$1"
readonly private_key_file="$2"
readonly peer_public_key="$3"
readonly endpoint="${4:-}"
readonly interface="wg0"
readonly listen_port="51820"

[[ -f "$private_key_file" ]] || {
  echo "WireGuard private key file is missing." >&2
  exit 64
}
[[ "$peer_public_key" =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$ ]] || {
  echo "Invalid WireGuard peer public key." >&2
  exit 64
}
case "$role" in
  server)
    [[ $# -eq 3 ]] || exit 64
    ;;
  client)
    [[ $# -eq 4 && "$endpoint" =~ ^[A-Za-z0-9.-]+$ ]] || {
      echo "Invalid old-cloud endpoint." >&2
      exit 64
    }
    ;;
  *)
    echo "Role must be server or client." >&2
    exit 64
    ;;
esac

if ! command -v wg >/dev/null 2>&1 || ! command -v wg-quick >/dev/null 2>&1; then
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y wireguard-tools
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y wireguard-tools
  else
    echo "Install wireguard-tools before running this installer." >&2
    exit 1
  fi
fi

private_key="$(tr -d '\r\n' < "$private_key_file")"
derived_public="$(printf '%s' "$private_key" | wg pubkey 2>/dev/null || true)"
[[ "$derived_public" =~ ^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$ ]] || {
  echo "Invalid WireGuard private key." >&2
  exit 64
}

install -d -o root -g root -m 0700 /etc/wireguard
if [[ -f "/etc/wireguard/${interface}.conf" ]]; then
  backup="/etc/wireguard/${interface}.conf.$(date -u +%Y%m%dT%H%M%SZ).bak"
  install -o root -g root -m 0600 "/etc/wireguard/${interface}.conf" "$backup"
fi

temporary_dir="$(mktemp -d)"
temporary="$temporary_dir/wg0.conf"
trap 'rm -f -- "$temporary"; rmdir -- "$temporary_dir" 2>/dev/null || true' EXIT
if [[ "$role" == "server" ]]; then
  cat > "$temporary" <<EOF
[Interface]
Address = 10.77.0.1/30
ListenPort = $listen_port
PrivateKey = $private_key

[Peer]
PublicKey = $peer_public_key
AllowedIPs = 10.77.0.2/32
EOF
else
  cat > "$temporary" <<EOF
[Interface]
Address = 10.77.0.2/30
PrivateKey = $private_key

[Peer]
PublicKey = $peer_public_key
AllowedIPs = 10.77.0.1/32
Endpoint = ${endpoint}:${listen_port}
PersistentKeepalive = 25
EOF
fi
chmod 0600 "$temporary"
wg-quick strip "$temporary" >/dev/null
install -o root -g root -m 0600 "$temporary" "/etc/wireguard/${interface}.conf"
rm -f -- "$temporary"
rmdir -- "$temporary_dir"
trap - EXIT
systemctl enable "wg-quick@${interface}.service"
systemctl restart "wg-quick@${interface}.service"
wg show "$interface"
