#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
  echo "Run as root on the server" >&2
  exit 2
fi

deploy_user="${1:-}"
public_key_file="${2:-}"
if [[ ! "$deploy_user" =~ ^[a-z_][a-z0-9_-]*$ || ! -s "$public_key_file" ]]; then
  echo "Usage: bootstrap-ubuntu-debian.sh DEPLOY_USER PUBLIC_KEY_FILE" >&2
  exit 2
fi

# shellcheck source=/dev/null
source /etc/os-release
if [[ "$ID" != ubuntu && "$ID" != debian ]]; then
  echo "Only Ubuntu and Debian are supported by this bootstrap script" >&2
  exit 2
fi

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  apt-get update
  apt-get install -y ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  codename="${UBUNTU_CODENAME:-$VERSION_CODENAME}"
  architecture="$(dpkg --print-architecture)"
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/$ID
Suites: $codename
Components: stable
Architectures: $architecture
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi

systemctl enable --now docker
if ! id "$deploy_user" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash "$deploy_user"
fi
groupadd --force docker
usermod -aG docker "$deploy_user"

deploy_home="$(getent passwd "$deploy_user" | cut -d: -f6)"
install -d -m 0700 -o "$deploy_user" -g "$deploy_user" "$deploy_home/.ssh"
authorized_keys="$deploy_home/.ssh/authorized_keys"
touch "$authorized_keys"
chmod 0600 "$authorized_keys"
chown "$deploy_user:$deploy_user" "$authorized_keys"
while IFS= read -r public_key; do
  if [[ -n "$public_key" ]] && ! grep -Fxq "$public_key" "$authorized_keys"; then
    printf '%s\n' "$public_key" >> "$authorized_keys"
  fi
done < "$public_key_file"

install -d -m 0750 -o "$deploy_user" -g "$deploy_user" /opt/theater-seat-watcher
install -d -m 0750 -o "$deploy_user" -g "$deploy_user" \
  /opt/theater-seat-watcher/releases /opt/theater-seat-watcher/shared

echo "Server bootstrap completed for $deploy_user. Use a new SSH session for Docker group access."
