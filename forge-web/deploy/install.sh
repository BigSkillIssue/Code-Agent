#!/usr/bin/env bash
# Sets up Forge Web with Docker Compose on this Linux server: Docker, the domains, .env, the
# images, the start, `forge-web doctor` and the link for the first admin account.
#
#   sudo forge-web/deploy/install.sh            asks for the two domains and the mail address
#   sudo forge-web/deploy/install.sh --domain forge.example.com --preview-domain vorschau.example.net \
#        --email admin@example.com --yes
#
# Run it from a checkout (git clone, see docs/EINRICHTUNG.md "In drei Befehlen"). An existing
# forge-web/.env is never overwritten, so running it again only builds and restarts. Images are
# built from this checkout as update.sh does, or pulled with --release. --dry-run prints every
# command instead of running it.
set -euo pipefail

root="$(cd "$(dirname "$0")/../.." && pwd)"
web="$root/forge-web"
DOMAIN_RE='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
EMAIL_RE='^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$'
SERVER_IMAGE=forge-web:local
SANDBOX_IMAGE=forge-web-sandbox:local

domain=""
preview=""
email=""
assume_yes=0
release=0
dry_run=0

usage() {
  cat <<'EOF'
Usage: install.sh [--domain NAME] [--preview-domain NAME] [--email ADDRESS] [--yes]
                  [--release] [--dry-run]

  --domain NAME          Forge Web's address, e.g. forge.example.com
  --preview-domain NAME  a separate domain for live previews (wildcard DNS *.NAME)
  --email ADDRESS        your mail address for the HTTPS certificates (Let's Encrypt)
  --yes                  ask nothing: everything comes from the options (or .env)
  --release              pull the released images instead of building them here
  --dry-run              print each command instead of running it
EOF
}

die() { echo "forge-web install: $*" >&2; exit 1; }
say() { echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }

run() {  # a command, or with --dry-run the command as it would run
  if [ "$dry_run" = 1 ]; then
    printf '+'; printf ' %q' "$@"; printf '\n'
  else
    "$@"
  fi
}

value() {  # the value of option $1, which must have one
  [ "$#" -ge 2 ] && [ -n "$2" ] || die "$1 needs a value (see --help)"
  printf '%s' "$2"
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --domain) domain="$(value "$@")"; shift 2 ;;
      --preview-domain) preview="$(value "$@")"; shift 2 ;;
      --email) email="$(value "$@")"; shift 2 ;;
      --yes) assume_yes=1; shift ;;
      --release) release=1; shift ;;
      --dry-run) dry_run=1; shift ;;
      -h | --help) usage; exit 0 ;;
      *) die "unknown option: $1 (see --help)" ;;
    esac
  done
  check "$domain" "$DOMAIN_RE" "not a domain: $domain"
  check "$preview" "$DOMAIN_RE" "not a domain: $preview"
  check "$email" "$EMAIL_RE" "not a mail address: $email"
}

check() {  # check VALUE PATTERN MESSAGE: an empty value or one that matches
  [ -z "$1" ] || [[ "$1" =~ $2 ]] || die "$3"
}

ask() {  # ask NAME QUESTION PATTERN: the answer, asked again until it fits
  local answer=""
  [ "$assume_yes" = 0 ] && [ -t 0 ] || die "$1 is missing: give it as an option (see --help)"
  while ! [[ "$answer" =~ $3 ]]; do
    read -r -p "$2: " answer
  done
  printf '%s' "$answer"
}

env_value() {  # the value of $1 in forge-web/.env, if it is set there
  sed -n "s/^$1=//p" "$web/.env" | tail -n 1
}

check_system() {
  [ "$(id -u)" = 0 ] || [ "$dry_run" = 1 ] || die "run this as root (sudo $0)"
  [ "$(uname -s)" = Linux ] || die "this installer is for Linux; see EINRICHTUNG.md section 6"
  [ -f "$web/compose.yaml" ] || die "run this from a checkout of the repository"
}

install_docker() {
  if command -v docker >/dev/null; then
    say "Docker is installed"
  else
    say "Installing Docker"
    if [ "$dry_run" = 1 ]; then echo "+ curl -fsSL https://get.docker.com | sh"; else
      curl -fsSL https://get.docker.com | sh
    fi
  fi
  run systemctl enable --now docker
}

docker_gid() {  # the group id compose gives Forge Web for the Docker socket
  local gid
  gid="$(getent group docker | cut -d: -f3 || true)"
  if [ -z "$gid" ]; then
    [ "$dry_run" = 1 ] || die "there is no group docker; is Docker installed?"
    gid=999  # Docker is not there yet in a dry run
  fi
  printf '%s' "$gid"
}

write_env() {  # forge-web/.env from deploy/env.example, readable by root only
  [ -n "$domain" ] || domain="$(ask --domain "Forge Web's address (e.g. forge.example.com)" "$DOMAIN_RE")"
  [ -n "$preview" ] || preview="$(ask --preview-domain "The preview domain (e.g. vorschau.example.net)" "$DOMAIN_RE")"
  [ -n "$email" ] || email="$(ask --email "Your mail address for the certificates" "$EMAIL_RE")"
  [ "$domain" != "$preview" ] || die "the preview domain must differ from Forge Web's"
  # The values matched the patterns above, so they hold no sed or shell syntax.
  sed -e "1s|.*|# Written by deploy/install.sh from deploy/env.example. docker compose reads it.|" \
    -e "s|^FORGE_DOMAIN=.*|FORGE_DOMAIN=$domain|" \
    -e "s|^PREVIEW_DOMAIN=.*|PREVIEW_DOMAIN=$preview|" \
    -e "s|^ACME_EMAIL=.*|ACME_EMAIL=$email|" \
    -e "s|^DOCKER_GID=.*|DOCKER_GID=$(docker_gid)|" \
    "$web/deploy/env.example" >"$work/env"
  if [ "$release" = 0 ]; then
    printf '\n# Built from this checkout by install.sh; update.sh builds them again.\n' >>"$work/env"
    printf 'FORGE_WEB_IMAGE=%s\nSANDBOX_IMAGE=%s\n' "$SERVER_IMAGE" "$SANDBOX_IMAGE" >>"$work/env"
  fi
  if [ "$dry_run" = 1 ]; then
    echo "+ write $web/.env (mode 0600):"
    sed 's/^/    /' "$work/env"
  else
    install -m 0600 "$work/env" "$web/.env"
  fi
}

images() {  # built from this checkout (as update.sh does) or the released ones
  if [ "$release" = 1 ]; then
    run docker compose pull
    return
  fi
  local server="$SERVER_IMAGE" sandbox="$SANDBOX_IMAGE"
  if [ -f "$web/.env" ]; then
    server="$(env_value FORGE_WEB_IMAGE)"
    sandbox="$(env_value SANDBOX_IMAGE)"
  fi
  if [ -z "$server" ] || [ -z "$sandbox" ]; then
    die "name the images to build in forge-web/.env (FORGE_WEB_IMAGE, SANDBOX_IMAGE) or use --release"
  fi
  say "Building the images (a few minutes)"
  # shellcheck disable=SC2086  # DOCKER_BUILD_ARGS holds several arguments on purpose
  run docker build ${DOCKER_BUILD_ARGS:-} -f "$web/docker/sandbox.Dockerfile" -t "$sandbox" "$root"
  # shellcheck disable=SC2086
  run docker build ${DOCKER_BUILD_ARGS:-} -f "$web/docker/server.Dockerfile" -t "$server" "$root"
}

first_admin_link() {  # the setup link the server logs while there is no account yet
  if [ "$dry_run" = 1 ]; then
    echo "+ docker compose logs forge-web   # the link for the first admin account"
    return
  fi
  local link="" _
  for _ in $(seq 1 30); do
    link="$(docker compose logs forge-web 2>/dev/null \
      | grep -o 'https://[^[:space:]]*/setup#token=[^[:space:]]*' | tail -n 1 || true)"
    [ -z "$link" ] || break
    sleep 2
  done
  if [ -n "$link" ]; then
    say "Create the first admin account (the link works until there is one):"
    echo "    $link"
  else
    say "No setup link: there is an account already, or see: docker compose logs forge-web"
  fi
}

main() {
  parse_args "$@"
  check_system
  work="$(mktemp -d)"
  trap 'rm -rf "$work"' EXIT
  cd "$web"
  [ "$dry_run" = 0 ] || say "Dry run: nothing is changed, every command is printed"
  install_docker
  if [ -f .env ]; then
    say "forge-web/.env exists and is kept"
    domain="$(env_value FORGE_DOMAIN)"
  else
    write_env
  fi
  [ -f forge-web.toml ] || run cp deploy/forge-web.example.toml forge-web.toml
  images
  run docker compose up -d
  say "Checking the server"
  run docker compose exec -T forge-web forge-web doctor || warn "forge-web doctor found problems (above)"
  first_admin_link
  say "Forge Web runs at https://$domain - DNS and the next steps: docs/EINRICHTUNG.md section 3."
}

main "$@"
