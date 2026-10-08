# syntax=docker/dockerfile:1
# The sandbox image: one container per project runs the Forge Web sandbox daemon as PID 1
# (under docker's --init). Build from the repository root:
#   docker build -f forge-web/docker/sandbox.Dockerfile -t forge-web-sandbox .
FROM python:3.12-slim-bookworm

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git ripgrep curl ca-certificates nodejs npm build-essential procps less unzip \
 && rm -rf /var/lib/apt/lists/*

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

ENV HOME=/home/forge \
    USER=forge \
    FORGE_HOME=/home/forge/.forge \
    LANG=C.UTF-8 \
    PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin
WORKDIR /workspace
LABEL org.forge-web.protocol="1"

# The daemon runs as root inside the container only to keep its socket away from the agent and
# to start programs as the `forge` user; the server drops every other capability.
ENTRYPOINT ["/opt/forge/bin/python", "-I", "-m", "forge_sandbox", "daemon", \
            "--workspace", "/workspace", "--socket", "/run/forge-sandbox/daemon.sock", \
            "--owner", "1000:1000"]
