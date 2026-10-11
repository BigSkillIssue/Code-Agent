# The runtime image for static web clients of hosted apps (W26b): Node 22 for the build in a
# throwaway container (`npm ci && npm run build`), and Caddy to serve the built files from /srv
# on $PORT, with index.html for every path the client routes itself. Build from the repo root:
#   docker build -f forge-web/docker/runtime-static.Dockerfile -t forge-runtime-static:1 .
ARG NODE_IMAGE=node:22.22.0-bookworm-slim
ARG CADDY_IMAGE=caddy:2.10.2
FROM ${CADDY_IMAGE} AS caddy
FROM ${NODE_IMAGE}
COPY --from=caddy /usr/bin/caddy /tmp/caddy
# A plain copy drops the binary's file capability (for ports below 1024): with every capability
# dropped, as the host runs services, such a binary cannot even start, and $PORT is high anyway.
RUN cp /tmp/caddy /usr/local/bin/caddy && rm /tmp/caddy \
 && printf '%s\n' \
      '{' '	admin off' '	auto_https off' '	persist_config off' '}' \
      ':{$PORT} {' '	root * /srv' '	encode gzip' '	try_files {path} /index.html' \
      '	file_server' '	header -Server' '}' > /etc/Caddyfile \
 && PORT=8080 caddy validate --config /etc/Caddyfile --adapter caddyfile
# The service's root is read-only: Caddy keeps its state in the container's /tmp.
ENV HOME=/tmp \
    XDG_CONFIG_HOME=/tmp \
    XDG_DATA_HOME=/tmp \
    npm_config_cache=/tmp/npm-cache \
    npm_config_update_notifier=false
WORKDIR /app
CMD ["caddy", "run", "--config", "/etc/Caddyfile", "--adapter", "caddyfile"]
