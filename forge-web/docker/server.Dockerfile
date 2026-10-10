# The Forge Web server image: the web server with the built UI and a Docker CLI that starts one
# sandbox container per project on the host's Docker (its socket is mounted in, see compose.yaml).
# Build from the repository root:
#   docker build -f forge-web/docker/server.Dockerfile -t forge-web .
# An optional build secret "ca" adds a proxy's CA certificate for npm and pip. Behind a registry
# mirror, the three base images can be given as build arguments (e.g. mirror.gcr.io/library/...).
ARG NODE_IMAGE=node:22.12.0-bookworm-slim
ARG DOCKER_CLI_IMAGE=docker:27.5.1-cli
ARG PYTHON_IMAGE=python:3.12-slim-bookworm

FROM ${NODE_IMAGE} AS ui
WORKDIR /build/forge-web/frontend
COPY forge-web/frontend/package.json forge-web/frontend/package-lock.json ./
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca; fi \
 && npm ci --no-audit --no-fund
COPY forge-web/frontend ./
# Vite writes the build next to the server package: /build/forge-web/packages/server/...
RUN npm run build

FROM ${DOCKER_CLI_IMAGE} AS docker-cli

FROM ${PYTHON_IMAGE}
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# The server runs as its own user; it reaches Docker through the mounted socket only.
RUN useradd --system --uid 10001 --create-home --home-dir /home/forge-web forge-web \
 && mkdir -p /data \
 && chown forge-web:forge-web /data

COPY pyproject.toml /src/forge/pyproject.toml
COPY src/forge /src/forge/src/forge
COPY forge-web/packages/sandbox /src/forge-sandbox
COPY forge-web/packages/macworker /src/forge-macworker
COPY forge-web/packages/server /src/forge-web
COPY --from=ui /build/forge-web/packages/server/src/forge_web/static /src/forge-web/src/forge_web/static
# The server uses only the Mac worker's wire format; the worker itself runs on Macs.
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi \
 && python -m venv /opt/forge-web \
 && /opt/forge-web/bin/pip install --no-cache-dir /src/forge /src/forge-sandbox \
    /src/forge-macworker /src/forge-web \
 && rm -rf /src

ENV PATH=/opt/forge-web/bin:/usr/local/bin:/usr/bin:/bin \
    FORGE_WEB_DATA_DIR=/data \
    FORGE_WEB_SERVER__HOST=0.0.0.0 \
    FORGE_WEB_SERVER__PORT=8420 \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1
USER forge-web
WORKDIR /home/forge-web
VOLUME ["/data"]
EXPOSE 8420
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8420/api/health', timeout=4)"]
CMD ["forge-web", "serve"]
