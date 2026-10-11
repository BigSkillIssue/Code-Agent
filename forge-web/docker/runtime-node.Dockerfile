# The runtime image for Node services of hosted apps (W26b): the host builds a release in a
# throwaway container of this image (`npm ci`, `npm run build`) and runs the service from that
# build, read-only, under gVisor. Build from the repository root:
#   docker build -f forge-web/docker/runtime-node.Dockerfile -t forge-runtime-node:22 .
ARG NODE_IMAGE=node:22.22.0-bookworm-slim
FROM ${NODE_IMAGE}
ENV HOME=/tmp \
    npm_config_cache=/tmp/npm-cache \
    npm_config_update_notifier=false
WORKDIR /app
