#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "root is required" >&2; exit 1; }
[ "$#" -eq 1 ] || { echo "usage: install-server.sh '<ssh-ed25519 public key>'" >&2; exit 1; }
public_key=$1
case "$public_key" in ssh-ed25519\ *) ;; *) echo "an Ed25519 public key is required" >&2; exit 1 ;; esac

reject_crlf() {
  carriage_return=$(printf '\r')
  for path in "$@"; do
    [ -f "$path" ] || { echo "required deployment asset is missing: $path" >&2; exit 1; }
    if LC_ALL=C grep -q "$carriage_return" "$path"; then
      echo "refusing to install a Linux asset with CRLF line endings: $path" >&2
      exit 1
    fi
  done
}

reject_crlf \
  binhu-venue-publish-gateway \
  binhu-venue-publish-gateway.py \
  validate-server.sh \
  migrate.sh \
  install-docker-engine.sh \
  activate-nginx.sh \
  nginx-http-context.conf \
  nginx-server-locations.conf

useradd --system --create-home --shell /bin/sh binhu-venue-publish 2>/dev/null || true
usermod --shell /bin/sh binhu-venue-publish
install -d -o root -g root -m 0755 /usr/local/libexec /usr/local/sbin
install -d -o root -g root -m 0755 /srv/binhu-venue
install -d -o root -g root -m 0700 /srv/binhu-venue/mysql
install -d -o 10001 -g 10001 -m 0700 /srv/binhu-venue/photos
install -d -o root -g root -m 0755 /srv/binhu-venue/incoming /srv/binhu-venue/archive /srv/binhu-venue/state
install -d -o root -g root -m 0700 /etc/binhu-venue
install -d -o root -g 10001 -m 0750 /etc/binhu-venue/venue-encryption-public
install -o root -g root -m 0755 ./binhu-venue-publish-gateway /usr/local/sbin/binhu-venue-publish-gateway
install -o root -g root -m 0755 ./binhu-venue-publish-gateway.py /usr/local/libexec/binhu-venue-publish-gateway.py
install -o root -g root -m 0755 ./validate-server.sh /usr/local/sbin/binhu-venue-validate
install -o root -g root -m 0755 ./migrate.sh /usr/local/sbin/binhu-venue-migrate
install -o root -g root -m 0755 ./install-docker-engine.sh /usr/local/sbin/binhu-venue-install-docker
install -o root -g root -m 0755 ./activate-nginx.sh /usr/local/sbin/binhu-venue-activate-nginx
install -o root -g root -m 0644 ./docker-compose.yml /etc/binhu-venue/docker-compose.yml
install -o root -g root -m 0644 ./nginx-http-context.conf /etc/binhu-venue/nginx-http-context.conf
install -o root -g root -m 0644 ./nginx-server-locations.conf /etc/binhu-venue/nginx-server-locations.conf
[ -e /etc/binhu-venue/receiver.env ] || install -o root -g root -m 0600 ./receiver.env.example /etc/binhu-venue/receiver.env
[ -e /etc/binhu-venue/mysql.env ] || install -o root -g root -m 0600 ./mysql.env.example /etc/binhu-venue/mysql.env

home=$(getent passwd binhu-venue-publish | cut -d: -f6)
install -d -o binhu-venue-publish -g binhu-venue-publish -m 0700 "$home/.ssh"
printf 'restrict,command="/usr/local/sbin/binhu-venue-publish-gateway" %s\n' "$public_key" > "$home/.ssh/authorized_keys"
chown binhu-venue-publish:binhu-venue-publish "$home/.ssh/authorized_keys"
chmod 0600 "$home/.ssh/authorized_keys"
printf 'binhu-venue-publish ALL=(root) NOPASSWD: /usr/local/libexec/binhu-venue-publish-gateway.py *\n' > /etc/sudoers.d/binhu-venue-publish
chmod 0440 /etc/sudoers.d/binhu-venue-publish

echo "Installed without starting containers or changing Nginx."
echo "Fill both env files and install key files as root:10001 mode 0640, then validate Compose and Nginx before first deployment."
