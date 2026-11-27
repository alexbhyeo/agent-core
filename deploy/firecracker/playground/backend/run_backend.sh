#!/usr/bin/env bash
# Build and start the Playground backend next to FcSessionManager (docker network fc-net).
#
#   PLAYGROUND_TOKEN            required: access token the frontend must send
#   PLAYGROUND_ALLOWED_ORIGINS  required for a separately hosted frontend, e.g. https://frontend.example
#   FCM_API_TOKEN               required: the manager's service token
#   API_KEY / API_BASE / MODEL_NAME / MODEL_PROVIDER   the LLM the agents use
#   PLAYGROUND_ENV_FILE         optional env file holding the variables above
#   PLAYGROUND_PUBLISH          host port binding (default 8800, all interfaces)
#   PLAYGROUND_DOCKER=1         mount the Docker socket to enable "restart manager"
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../../.." && pwd)"
NAME="${PLAYGROUND_BACKEND_NAME:-fc-playground-backend}"

env_args=()
if [ -n "${PLAYGROUND_ENV_FILE:-}" ]; then env_args+=(--env-file "$PLAYGROUND_ENV_FILE"); fi
for key in PLAYGROUND_TOKEN PLAYGROUND_ALLOWED_ORIGINS PLAYGROUND_MAX_RUNS PLAYGROUND_MAX_ITERATIONS \
           PLAYGROUND_DEFAULT_MEM_MIB PLAYGROUND_DEFAULT_VCPUS PLAYGROUND_USERS FCM_API_TOKEN FC_MANAGER_URL \
           API_KEY API_BASE MODEL_NAME MODEL_PROVIDER MODEL_TIMEOUT; do
  if [ -n "${!key:-}" ]; then env_args+=(-e "$key"); fi
done
docker_args=()
if [ "${PLAYGROUND_DOCKER:-0}" = "1" ]; then
  docker_args=(-v /var/run/docker.sock:/var/run/docker.sock -e PLAYGROUND_DOCKER=1
               -e FC_MANAGER_CONTAINER="${FC_MANAGER_NAME:-fc-manager}")
fi

docker build -q -f "$REPO/deploy/firecracker/playground/backend/Dockerfile" -t fc-playground-backend:dev "$REPO" >/dev/null
docker network inspect fc-net >/dev/null 2>&1 || docker network create fc-net >/dev/null
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --hostname "$NAME" --network fc-net \
  -p "${PLAYGROUND_PUBLISH:-8800}:8800" \
  -v fc-playground-data:/var/lib/fc-playground \
  "${env_args[@]}" "${docker_args[@]}" \
  fc-playground-backend:dev >/dev/null

for _ in $(seq 1 60); do
  if docker exec "$NAME" curl -fsS http://127.0.0.1:8800/healthz >/dev/null 2>&1; then
    echo "Playground backend is up: http://$NAME:8800 (fc-net), port ${PLAYGROUND_PUBLISH:-8800} on this host"
    exit 0
  fi
  sleep 1
done
echo "backend did not become healthy; logs:" >&2
docker logs "$NAME" >&2
exit 1
