# Forge Web

A multi-user server with a web UI for the [Forge](../README.md) coding agent. Run it on a Linux, macOS or
Windows server; people sign in with Google, GitHub or email and password, keep several projects, and chat with
Forge in each one — with a file tree and editor, a changes/git panel, a terminal and a live preview, much like
the Claude Code desktop app. Every project runs in its own hardened container, so users can be strangers.

> Status: feature-complete, in final review — see [`docs/STEPS.md`](docs/STEPS.md) and
> [`PROGRESS.md`](PROGRESS.md). Setup guide (German): [`docs/EINRICHTUNG.md`](docs/EINRICHTUNG.md).

## Try it from a checkout

```bash
cd forge-web
uv sync
(cd frontend && npm ci && npm run build)   # the web UI, into forge_web/static
uv run forge-web serve --dev --fake        # prints a login link; the fake model needs no API key
uv run forge-web dev-chat --fake           # or chat in the terminal
```

`--dev` runs every project's sandbox as a local process (no Docker) with one local admin; it is
for trying Forge Web on your own machine, not for other people.

Forge Web lives next to Forge and never changes it: `forge-web/` is a separate uv workspace that uses Forge
as a library.

## Sign-in with Google, GitHub or your own identity provider

Register an OAuth app with the provider, using `<public_url>/api/auth/oauth/<name>/callback` as the
redirect (callback) URL, then name it in `forge-web.toml` and put its client secret in an environment
variable (`FORGE_WEB_<NAME>_SECRET` unless `client_secret_env` names another):

```toml
[server]
public_url = "https://forge.example.com"

[auth.providers.google]      # console.cloud.google.com → APIs & Services → Credentials → OAuth client
client_id = "1234-abc.apps.googleusercontent.com"   # secret in FORGE_WEB_GOOGLE_SECRET

[auth.providers.github]      # github.com → Settings → Developer settings → OAuth Apps
client_id = "Ov23li..."                             # secret in FORGE_WEB_GITHUB_SECRET

[auth.providers.company]     # any OpenID Connect issuer: Microsoft Entra, GitLab, Keycloak, …
kind = "oidc"
label = "Company login"
issuer = "https://login.microsoftonline.com/<tenant>/v2.0"
client_id = "..."
trust_email = true           # only if the issuer checks emails but sends no email_verified
```

A provider account joins an existing Forge account only when the provider has verified its email and
the Forge account's email is confirmed too; otherwise people link providers in their settings while
signed in. Sign-up rules (`auth.signup`: invite, approval or open, and `auth.allowed_domains`) apply to
provider sign-ins as well. GitHub can also be connected for repositories (clone, pull, push); that grant
is separate from signing in and is stored encrypted.

## Live previews

A dev server started in a project (`npm run dev`, `python3 -m http.server`, …) opens in the Preview tab.
Every preview gets a host of its own, `p<port>-<project>.<preview domain>`, so the app in it is another
site than Forge: it never sees Forge's cookies and cannot use Forge's API as you. Forge opens a preview
with a one-time ticket that becomes a cookie for that host only; other sites may link to a preview but
not fetch from it.

On your own machine nothing needs setting up: a server that listens only on `127.0.0.1` serves previews
on `http://p<port>-<project>.localhost:<port>` (Chrome, Edge and Firefox resolve `*.localhost` by
themselves). A server other people reach needs a domain of its own with wildcard DNS (and a wildcard
certificate on the reverse proxy) pointing at Forge Web:

```toml
[preview]
domain = "preview.example.net"   # *.preview.example.net → this server; not a subdomain of Forge's
# https = true                   # default: like server.public_url
# port = 8443                    # only if previews are not on the scheme's default port
```

Without a domain, previews are off on such a server.

## Two-factor sign-in and server settings

Everyone can turn on two-factor sign-in in their settings (any authenticator app; ten recovery codes
for a lost phone). With `auth.admin_two_factor = true`, admins must use it before they can administer.
Admins change sign-up rules, quotas, sandbox limits and server-key rules on the Admin page; those
values are kept in the database and win over `forge-web.toml`.

## Running it for other people

`compose.yaml` runs Forge Web, the sandbox image and Caddy (automatic HTTPS, also for preview hosts)
on a Linux server with Docker:

```bash
cp deploy/env.example .env && cp deploy/forge-web.example.toml forge-web.toml   # fill them in
docker compose up -d
docker compose logs forge-web                    # the link for the first admin account
docker compose exec forge-web forge-web doctor   # what is missing, and how to fix it
```

`deploy/` also has service templates for running the server without Docker (systemd, launchd,
WinSW); the projects still run in Docker containers. Images and wheels come from the release
workflow (`.github/workflows/forge-web-release.yml`, tags `forge-web-v*`) or are built from this
checkout (`docker/server.Dockerfile`, `docker/sandbox.Dockerfile`). The German guide walks through
DNS, HTTPS, Google and GitHub sign-in apps, backups and updates.
