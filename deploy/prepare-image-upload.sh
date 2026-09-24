#!/usr/bin/env bash
set -euo pipefail

root=/opt/theater-seat-watcher
current="$root/current"
prepared="$root/shared/.image-upload-prepared"

if [[ -e "$current" && ! -L "$current" ]]; then
  echo "$current exists and is not a symbolic link" >&2
  exit 2
fi

mapfile -t existing_bots < <(
  docker ps -q \
    --filter label=com.docker.compose.project=theater-tickets \
    --filter label=com.docker.compose.service=bot
)
if (( ${#existing_bots[@]} > 1 )); then
  echo "Several running bot containers need manual review" >&2
  exit 2
fi

if [[ ! -L "$current" ]] && (( ${#existing_bots[@]} == 1 )); then
  recovered_config="$({
    docker inspect "${existing_bots[0]}" \
      --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}'
  } 2>/dev/null)"
  recovered_release="$(dirname "$(dirname "$recovered_config")")"
  if [[ "$recovered_release" != "$root"/releases/* ]] || \
    [[ "$recovered_config" != "$recovered_release/deploy/compose.yaml" ]] || \
    [[ ! -f "$recovered_config" ]]; then
    echo "Running bot does not belong to a release under $root/releases" >&2
    exit 2
  fi
  recovery_link="$root/.current-recovered-$$"
  ln -s "$recovered_release" "$recovery_link"
  mv -Tf "$recovery_link" "$current"
  echo "Recovered managed release $(basename "$recovered_release")"
fi

if [[ -L "$current" ]] && (( ${#existing_bots[@]} == 1 )); then
  previous="$(readlink -f "$current")"
  previous_compose="$previous/deploy/compose.yaml"
  if [[ "$previous" != "$root"/releases/* || ! -f "$previous_compose" ]]; then
    echo "Managed release path is invalid" >&2
    exit 2
  fi
  backup_name="theater-tickets-$(date -u +%Y%m%dT%H%M%S%NZ)-before-image-upload.sqlite3"
  echo "Creating database backup $backup_name"
  docker compose --project-name theater-tickets --file "$previous_compose" \
    exec -T bot theater-tickets backup "/app/backups/$backup_name"
  printf '%s\n' "$backup_name" > "$prepared.tmp"
  chmod 0600 "$prepared.tmp"
  mv -f "$prepared.tmp" "$prepared"
  docker compose --project-name theater-tickets --file "$previous_compose" stop bot
  docker compose --project-name theater-tickets --file "$previous_compose" rm -f bot
elif [[ -L "$current" && ! -f "$prepared" ]]; then
  echo "Managed bot is stopped without a prepared database backup" >&2
  exit 2
fi

if [[ -f "$prepared" ]]; then
  mapfile -t remaining_bots < <(
    docker ps -aq \
      --filter label=com.docker.compose.project=theater-tickets \
      --filter label=com.docker.compose.service=bot
  )
  if (( ${#remaining_bots[@]} > 1 )); then
    echo "Several bot containers remain after backup; stop for manual review" >&2
    exit 2
  fi
  if (( ${#remaining_bots[@]} == 1 )); then
    docker rm -f "${remaining_bots[0]}"
  fi
fi

while IFS= read -r image; do
  docker image rm "$image"
done < <(docker image ls --format '{{.Repository}}:{{.Tag}}' --filter 'reference=theater-tickets:*')
docker image prune -f >/dev/null
docker builder prune -af >/dev/null

available_kib="$(df --output=avail "$root" | tail -n 1 | tr -d ' ')"
echo "Server prepared for image upload; available_kib=$available_kib"
