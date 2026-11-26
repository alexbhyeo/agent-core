#!/usr/bin/env bash
# Start FcSessionManager in a privileged container on the docker network fc-net.
#   FCM_API_TOKEN  required: service credential agent-core sends as a bearer token
#   FC_ARTIFACTS   directory holding rootfs.ext4 (default: ~/.cache/fc-artifacts)
#   FCM_*          any other manager setting is passed through (see manager/fc_session_manager/config.py)
set -euo pipefail
: "${FCM_API_TOKEN:?set FCM_API_TOKEN}"
ART="${FC_ARTIFACTS:-$HOME/.cache/fc-artifacts}"
NAME="${FC_MANAGER_NAME:-fc-manager}"

docker network inspect fc-net >/dev/null 2>&1 || docker network create fc-net >/dev/null
docker rm -f "$NAME" >/dev/null 2>&1 || true
# Pass every other FCM_* setting of the caller through (tiers, warm pool, snapshot sealing, ...).
extra_env=()
while IFS='=' read -r key _; do
  case "$key" in FCM_API_TOKEN|FCM_ADVERTISE_HOST|FCM_DEFAULT_MEM_MIB) ;; FCM_*) extra_env+=(-e "$key") ;; esac
done < <(env)
# --stop-timeout: a graceful stop persists running VMs (snapshots), which takes a few seconds each.
docker run -d --name "$NAME" --hostname "$NAME" --network fc-net \
  --privileged --device /dev/kvm --stop-timeout 300 \
  -v "$ART:/var/lib/fc-images:ro" \
  -v fc-sessions:/var/lib/fc-sessions \
  -e FCM_API_TOKEN="$FCM_API_TOKEN" \
  -e FCM_ADVERTISE_HOST="$NAME" \
  -e FCM_DEFAULT_MEM_MIB="${FCM_DEFAULT_MEM_MIB:-2048}"   "${extra_env[@]}" \
  -p 127.0.0.1:8700:8700 \
  fc-manager:dev

# First start caches the rootfs on the data volume, which can take a minute or two.
for _ in $(seq 1 300); do
  if curl -fsS http://127.0.0.1:8700/healthz >/dev/null 2>&1; then
    echo "FcSessionManager is up: http://$NAME:8700 (inside fc-net), http://127.0.0.1:8700 (host)"
    exit 0
  fi
  sleep 1
done
echo "manager did not become healthy; logs:" >&2
docker logs "$NAME" >&2
exit 1
