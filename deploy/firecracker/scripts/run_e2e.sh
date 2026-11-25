#!/usr/bin/env bash
# Run tests inside the e2e runner container, on the same docker network as the manager.
#   FC_E2E_ENV_FILE  env file with FCM_API_TOKEN (+ optional API_KEY/API_BASE/MODEL_NAME/MODEL_PROVIDER)
#   remaining args   passed to pytest (default: the Firecracker unit + e2e tests)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
: "${FC_E2E_ENV_FILE:?set FC_E2E_ENV_FILE to an env file with FCM_API_TOKEN}"
if [ "$#" -eq 0 ]; then
  set -- tests/unit_tests/extensions/sys_operation/sandbox/test_firecracker_launcher.py \
         tests/system_tests/extensions/sys_operation/sandbox/test_firecracker_e2e.py
fi
# The checkout is mounted read-only; logs and workspaces go to a scratch dir.
args=()
for a in "$@"; do
  case "$a" in tests/*|examples/*) args+=("/src/$a") ;; *) args+=("$a") ;; esac
done
docker run --rm --network fc-net \
  --env-file "$FC_E2E_ENV_FILE" \
  -e FC_MANAGER_URL="${FC_MANAGER_URL:-http://fc-manager:8700}" \
  -v "$REPO:/src:ro" \
  --tmpfs /work:exec,size=2g -w /work \
  -e PYTHONDONTWRITEBYTECODE=1 \
  fc-e2e-runner:dev \
  python -m pytest --rootdir /src -c /src/pyproject.toml -p no:cacheprovider -o addopts="" -v -s "${args[@]}"
