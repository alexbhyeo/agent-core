#!/bin/sh
# nginx image entrypoint hook: write config.js (backend URL, default user) from the environment.
set -eu
: "${PLAYGROUND_API_BASE_URL:?set PLAYGROUND_API_BASE_URL to the backend URL, e.g. https://backend.example:8800}"
export PLAYGROUND_DEFAULT_USER="${PLAYGROUND_DEFAULT_USER:-alice}"
envsubst '${PLAYGROUND_API_BASE_URL} ${PLAYGROUND_DEFAULT_USER}' \
  < /usr/share/nginx/html/config.template.js > /usr/share/nginx/html/config.js
echo "playground frontend: backend at ${PLAYGROUND_API_BASE_URL}"
