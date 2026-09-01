#!/bin/sh
set -eu

[ "$(id -u)" -eq 0 ] || { echo "root is required" >&2; exit 1; }
[ -r /etc/os-release ] || { echo "/etc/os-release is required" >&2; exit 1; }
. /etc/os-release

case "${ID:-}" in
  alinux|rhel|centos|rocky|almalinux) ;;
  *) echo "unsupported Docker host OS: ${ID:-unknown}" >&2; exit 1 ;;
esac

available_kb=$(df -Pk /var/lib 2>/dev/null | awk 'NR == 2 {print $4}')
[ -n "$available_kb" ] || available_kb=$(df -Pk / | awk 'NR == 2 {print $4}')
[ "$available_kb" -ge 10485760 ] || {
  echo "at least 10 GiB of free space is required before Docker installation" >&2
  exit 1
}

if command -v docker >/dev/null 2>&1 && [ -z "${BINHU_DOCKER_RPM_DIR:-}" ]; then
  docker version
  docker compose version
  systemctl enable --now docker
  exit 0
fi

conflicts="docker docker-client docker-client-latest docker-common docker-latest docker-latest-logrotate docker-logrotate docker-engine podman-docker"
for package in $conflicts; do
  if rpm -q "$package" >/dev/null 2>&1; then
    echo "conflicting package is installed; review it manually: $package" >&2
    exit 1
  fi
done

# A previous interrupted install may have left an enabled Docker repository.
# Do not let that repository break the bootstrap step before reachability is checked.
dnf install -y --disablerepo='docker-ce*' dnf-plugins-core

official_repo="https://download.docker.com/linux/rhel/docker-ce.repo"
repo_tmp=$(mktemp)
package_names=$(mktemp)
trap 'rm -f "$repo_tmp" "$package_names"' EXIT HUP INT TERM

if curl -fsSL --retry 2 --connect-timeout 10 --max-time 30 \
  "$official_repo" -o "$repo_tmp"; then
  echo "Using Docker's official RHEL repository."
  grep -q '^gpgcheck=1$' "$repo_tmp" || {
    echo "Docker repository must enforce RPM signature verification" >&2
    exit 1
  }
  grep -q '^gpgkey=https://' "$repo_tmp" || {
    echo "Docker repository must use an HTTPS signing-key URL" >&2
    exit 1
  }
  install -o root -g root -m 0644 "$repo_tmp" /etc/yum.repos.d/docker-ce.repo
  dnf makecache --refresh --disablerepo='*' --enablerepo=docker-ce-stable
  dnf install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
else
  rpm_dir=${BINHU_DOCKER_RPM_DIR:-}
  [ -n "$rpm_dir" ] && [ -d "$rpm_dir" ] || {
    echo "Docker's official repository is unreachable." >&2
    echo "Set BINHU_DOCKER_RPM_DIR to a directory containing Docker's signed RPM bundle and docker-ce.gpg." >&2
    exit 1
  }
  signing_key="$rpm_dir/docker-ce.gpg"
  [ -f "$signing_key" ] || { echo "offline Docker signing key is missing" >&2; exit 1; }
  expected_key_sha256="e6c650e0700b1bf4868b693b30761b926844befc8a0acb7ac0dd9b1faf1b7423"
  actual_key_sha256=$(sha256sum "$signing_key" | awk '{print $1}')
  [ "$actual_key_sha256" = "$expected_key_sha256" ] || {
    echo "offline Docker signing key hash mismatch" >&2
    exit 1
  }
  rpm --import "$signing_key"
  set -- "$rpm_dir"/*.rpm
  [ -f "$1" ] || { echo "offline Docker RPM bundle is empty" >&2; exit 1; }
  for rpm_file in "$@"; do
    rpmkeys --checksig "$rpm_file" | grep -q 'digests signatures OK$' || {
      echo "Docker RPM signature verification failed: $rpm_file" >&2
      exit 1
    }
    rpm -qp --queryformat '%{NAME}\n' "$rpm_file" >> "$package_names"
  done
  for package in containerd.io docker-buildx-plugin docker-ce docker-ce-cli docker-compose-plugin; do
    grep -qx "$package" "$package_names" || {
      echo "required Docker RPM is missing: $package" >&2
      exit 1
    }
  done
  echo "Using the locally supplied Docker RPM bundle after signature verification."
  dnf install -y --disablerepo='docker-ce*' "$rpm_dir"/*.rpm
fi
systemctl enable --now docker
docker version
docker compose version

echo "Docker Engine and Compose are ready. No containers were started."
