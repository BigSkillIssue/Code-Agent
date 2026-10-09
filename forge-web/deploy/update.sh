#!/usr/bin/env bash
# Update a Forge Web server that runs with Docker Compose: newest code, new images, restart.
#
#   forge-web/deploy/update.sh            build both images from this checkout (no release yet)
#   forge-web/deploy/update.sh --release  pull the released images instead
#
# The image names come from forge-web/.env (FORGE_WEB_IMAGE, SANDBOX_IMAGE; see EINRICHTUNG.md
# 2.4). Extra `docker build` arguments, such as a registry mirror for the base images, go into
# DOCKER_BUILD_ARGS, e.g.
#   DOCKER_BUILD_ARGS="--build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim-bookworm"
# Projects keep their files. Each one gets the new Forge the next time its sandbox starts (a
# running one after its idle stop), and the database is brought up to date at the start.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
web="$root/forge-web"

image_in_env() {  # the value of $1 in forge-web/.env, if it is set there
  if [ -f "$web/.env" ]; then
    sed -n "s/^$1=//p" "$web/.env" | tail -n 1
  fi
}

git -C "$root" pull --ff-only
cd "$web"
if [ "${1:-}" = "--release" ]; then
  docker compose pull
else
  server="$(image_in_env FORGE_WEB_IMAGE)"
  sandbox="$(image_in_env SANDBOX_IMAGE)"
  if [ -z "$server" ] || [ -z "$sandbox" ]; then
    echo "Name the images to build in forge-web/.env (FORGE_WEB_IMAGE, SANDBOX_IMAGE)," >&2
    echo "see EINRICHTUNG.md 2.4 - or update to the released images with --release." >&2
    exit 1
  fi
  # shellcheck disable=SC2086  # DOCKER_BUILD_ARGS holds several arguments on purpose
  docker build ${DOCKER_BUILD_ARGS:-} -f "$web/docker/sandbox.Dockerfile" -t "$sandbox" "$root"
  # shellcheck disable=SC2086
  docker build ${DOCKER_BUILD_ARGS:-} -f "$web/docker/server.Dockerfile" -t "$server" "$root"
fi
docker compose up -d
docker compose exec -T forge-web forge-web doctor || echo "forge-web doctor found problems (above)."
echo "Forge Web is up to date. Projects get the new Forge the next time their sandbox starts."
