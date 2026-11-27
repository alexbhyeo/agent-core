#!/usr/bin/env bash
# Build and start the Playground frontend (static site) on this host.
#
#   PLAYGROUND_API_BASE_URL  required: backend URL as the browser reaches it, e.g. https://backend.example:8800
#   PLAYGROUND_DEFAULT_USER  optional (default alice)
#   FRONTEND_PUBLISH         host port binding (default 8080)
#   FRONTEND_NETWORK         optional docker network to join (e.g. fc-net when testing on one host)
set -euo pipefail
: "${PLAYGROUND_API_BASE_URL:?set PLAYGROUND_API_BASE_URL to the backend URL}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="${PLAYGROUND_FRONTEND_NAME:-fc-playground-frontend}"

net_args=()
if [ -n "${FRONTEND_NETWORK:-}" ]; then net_args=(--network "$FRONTEND_NETWORK" --hostname "$NAME"); fi

docker build -q -t fc-playground-frontend:dev "$HERE" >/dev/null
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" "${net_args[@]}" -p "${FRONTEND_PUBLISH:-8080}:8080" \
  -e PLAYGROUND_API_BASE_URL -e PLAYGROUND_DEFAULT_USER="${PLAYGROUND_DEFAULT_USER:-alice}" \
  fc-playground-frontend:dev >/dev/null
for _ in $(seq 1 30); do
  if docker exec "$NAME" wget -qO- http://127.0.0.1:8080/healthz >/dev/null 2>&1; then
    echo "Playground frontend is up on port ${FRONTEND_PUBLISH:-8080} (backend: $PLAYGROUND_API_BASE_URL)"
    exit 0
  fi
  sleep 1
done
docker logs "$NAME" >&2
exit 1
