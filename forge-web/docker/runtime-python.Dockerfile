# The runtime image for Python services of hosted apps (W26b). The host builds a release in a
# throwaway container of this image (`uv sync --locked --no-dev` makes /app/.venv) and then runs
# the service from that build, read-only, under gVisor. Build from the repository root:
#   docker build -f forge-web/docker/runtime-python.Dockerfile -t forge-runtime-python:3.12 .
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
FROM ${PYTHON_IMAGE}
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /usr/local/bin/
# The venv uses this image's Python; caches live in the container's /tmp, never in the release.
ENV UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_CACHE_DIR=/tmp/uv-cache \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/tmp
WORKDIR /app
