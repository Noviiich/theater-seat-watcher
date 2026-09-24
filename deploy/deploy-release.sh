#!/usr/bin/env bash
set -euo pipefail

release_id="${1:-}"
if [[ ! "$release_id" =~ ^[a-f0-9]{40}-[0-9]+-[0-9]+$ ]]; then
  echo "Invalid release identifier" >&2
  exit 2
fi

root=/opt/theater-seat-watcher
release="$root/releases/$release_id"
current="$root/current"
shared_env="$root/shared/.env"
compose=(docker compose --project-name theater-tickets --file "$release/deploy/compose.yaml")

if [[ ! -f "$shared_env" ]]; then
  echo "Server configuration is missing: $shared_env" >&2
  exit 2
fi
if [[ ! -f "$release/deploy/compose.yaml" || ! -d "$release/config/halls" ]]; then
  echo "Release files are incomplete" >&2
  exit 2
fi
if ! command -v docker >/dev/null || ! docker compose version >/dev/null; then
  echo "Docker and the Compose plugin are required" >&2
  exit 2
fi
if [[ -e "$current" && ! -L "$current" ]]; then
  echo "$current exists and is not a symbolic link" >&2
  exit 2
fi
if [[ ! -L "$current" ]]; then
  mapfile -t existing_bots < <(
    docker ps -q \
      --filter label=com.docker.compose.project=theater-tickets \
      --filter label=com.docker.compose.service=bot
  )
  if (( ${#existing_bots[@]} > 1 )); then
    echo "Several running bot containers have no managed release; stop for manual review" >&2
    exit 2
  fi
  if (( ${#existing_bots[@]} == 1 )); then
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
fi

if [[ -L "$release/.env" ]]; then
  if [[ "$(readlink -f "$release/.env")" != "$(readlink -f "$shared_env")" ]]; then
    echo "Release .env link points to an unexpected file" >&2
    exit 2
  fi
elif [[ -e "$release/.env" ]]; then
  echo "Release .env exists and is not the expected symlink" >&2
  exit 2
else
  ln -s "$shared_env" "$release/.env"
fi
export THEATER_TICKETS_IMAGE="theater-tickets:${release_id%%-*}"

if ! docker image inspect "$THEATER_TICKETS_IMAGE" >/dev/null 2>&1; then
  echo "Ready image is missing: $THEATER_TICKETS_IMAGE" >&2
  exit 2
fi
if [[ "$(docker image inspect "$THEATER_TICKETS_IMAGE" --format '{{.Architecture}}')" != amd64 ]]; then
  echo "Ready image has an unsupported architecture" >&2
  exit 2
fi
if [[ "$(docker image inspect "$THEATER_TICKETS_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')" != "${release_id%%-*}" ]]; then
  echo "Ready image revision does not match the release" >&2
  exit 2
fi

if [[ -L "$current" ]]; then
  previous="$(readlink -f "$current")"
  if [[ -n "$(docker compose --project-name theater-tickets --file "$previous/deploy/compose.yaml" ps -q bot)" ]]; then
    backup_name="theater-tickets-$(date -u +%Y%m%dT%H%M%SZ)-${release_id}.sqlite3"
    echo "Creating database backup $backup_name"
    docker compose --project-name theater-tickets --file "$previous/deploy/compose.yaml" \
      exec -T bot theater-tickets backup "/app/backups/$backup_name"
  else
    echo "Previous bot is not running; database backup needs manual review" >&2
    exit 2
  fi
elif [[ -f "$root/shared/theater_tickets_initial.sqlite3" ]]; then
  echo "Importing initial SQLite backup"
  "${compose[@]}" run --rm --no-deps --user root:root --cap-add DAC_READ_SEARCH \
    --volume "$root/shared/theater_tickets_initial.sqlite3:/app/import/initial.sqlite3:ro" \
    --entrypoint sh bot -ec '
      test ! -e /app/data/theater_tickets.sqlite3
      theater-tickets restore /app/import/initial.sqlite3 --yes
      chown 10001:10001 /app/data/theater_tickets.sqlite3
    '
fi

echo "Starting release ${release_id%%-*}"
"${compose[@]}" up -d --no-build --force-recreate --wait --wait-timeout 180 bot
"${compose[@]}" exec -T bot theater-tickets health

link="$root/.current-$release_id"
ln -s "$release" "$link"
mv -Tf "$link" "$current"
echo "Deployment healthy: ${release_id%%-*}"

# The smallest supported servers have little room for repeated Chromium builds.
while IFS= read -r image; do
  if [[ "$image" != "$THEATER_TICKETS_IMAGE" ]]; then
    docker image rm "$image" >/dev/null || echo "Could not remove old image: $image" >&2
  fi
done < <(docker image ls --format '{{.Repository}}:{{.Tag}}' --filter 'reference=theater-tickets:*')
