# The sandbox image: one container per project runs the Forge Web sandbox daemon as PID 1
# (under docker's --init). Build from the repository root:
#   docker build -f forge-web/docker/sandbox.Dockerfile -t forge-web-sandbox .
# Behind a registry mirror: --build-arg PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-slim-bookworm
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ripgrep curl ca-certificates xz-utils gnupg build-essential procps less unzip \
 && rm -rf /var/lib/apt/lists/*

# Full-stack apps (Forge's `forge app new`, W24): Node 22 for the web client (Debian's Node is too
# old for Vite), PostgreSQL 16 for `forge app dev` and `forge app check` (a throwaway cluster per
# run, as the `forge` user), and uv for the server's lockfile.
# Downloads honour the optional build secret "ca" (a proxy's CA), like pip below.
ARG NODE_VERSION=22.22.0
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export CURL_CA_BUNDLE=/run/secrets/ca; fi \
 && arch="$(dpkg --print-architecture | sed 's/amd64/x64/')" \
 && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz" \
    | tar -xJ -C /usr/local --strip-components=1 --exclude=CHANGELOG.md --exclude=README.md \
 && node --version && npm --version
# apt checks the repository's signature (signed-by), as for Debian's own http mirrors.
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export CURL_CA_BUNDLE=/run/secrets/ca; fi \
 && install -d /usr/share/postgresql-common/pgdg \
 && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
      https://www.postgresql.org/media/keys/ACCC4CF8.asc \
 && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-16 \
 && rm -rf /var/lib/apt/lists/* \
 && /usr/lib/postgresql/16/bin/initdb --version
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /usr/local/bin/

# The user that owns the project and runs everything the agent starts.
RUN useradd --create-home --uid 1000 --shell /bin/bash forge \
 && mkdir -p /workspace /home/forge/.forge \
 && chown forge:forge /workspace /home/forge/.forge

# Forge and the sandbox runtime live in their own venv, outside the user's PATH, so a project's
# own `pip install` never touches them. An optional build secret "ca" adds a proxy's CA.
COPY pyproject.toml /src/forge/pyproject.toml
COPY src/forge /src/forge/src/forge
COPY forge-web/packages/sandbox /src/forge-sandbox
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi \
 && python -m venv /opt/forge \
 && /opt/forge/bin/pip install --no-cache-dir /src/forge \
 && /opt/forge/bin/pip install --no-cache-dir --no-deps /src/forge-sandbox \
 && rm -rf /src

# The Chromium that Forge photographs a running app with before its release review (S67b),
# matching Forge's Playwright; readable by the `forge` user.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca; fi \
 && /opt/forge/bin/playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* \
 && chmod -R a+rX /opt/pw-browsers

ENV HOME=/home/forge \
    USER=forge \
    FORGE_HOME=/home/forge/.forge \
    LANG=C.UTF-8 \
    UV_CACHE_DIR=/home/forge/.cache/uv \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin
WORKDIR /workspace
LABEL org.forge-web.protocol="1"

# The daemon runs as root inside the container only to keep its socket away from the agent and
# to start programs as the `forge` user; the server drops every other capability.
ENTRYPOINT ["/opt/forge/bin/python", "-I", "-m", "forge_sandbox", "daemon", \
            "--workspace", "/workspace", "--socket", "/run/forge-sandbox/daemon.sock", \
            "--owner", "1000:1000"]
