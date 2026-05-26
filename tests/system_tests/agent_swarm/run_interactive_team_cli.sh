#!/usr/bin/env bash
# Launch the interactive Team CLI from the repository root.
#
# Activates the local .venv, sets PYTHONPATH, exports the model /
# endpoint defaults that tests/system_tests/agent_swarm/main.py used to set in
# Python, then runs tests/system_tests/agent_swarm/interactive_team_cli.py. Safe
# to invoke from any working directory — the script resolves the repo
# root from its own location. Forwards extra args to the launcher, so
# you can pass an alternate yaml:
#
#     ./tests/system_tests/agent_swarm/run_interactive_team_cli.sh \
#         tests/system_tests/agent_swarm/config_hitt_blast_furnace.yaml
#
# Override any default by exporting the variable before invocation:
#
#     export MODEL_NAME=glm-5.1
#     ./tests/system_tests/agent_swarm/run_interactive_team_cli.sh
#
# Windows: the project keeps shell scripts unix-only. Run the launcher
# directly with `python tests\system_tests\agent_swarm\interactive_team_cli.py`
# from an activated venv instead.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

cd "${REPO_ROOT}"

if [[ ! -f ".venv/bin/activate" ]]; then
    echo "error: .venv/bin/activate not found in ${REPO_ROOT}" >&2
    echo "       run 'uv sync' first to create the virtual environment." >&2
    exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate
export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"

# Default model / endpoint env (mirrors the os.environ.setdefault block
# in tests/system_tests/agent_swarm/main.py). `${VAR:-default}` keeps any value
# the user already exported.
export API_KEY="${API_KEY:-sk-1e61b6de1f9b4ccab4a117d3ce2e33b4}"
export LEADER_API_KEY="${LEADER_API_KEY:-sk-1e61b6de1f9b4ccab4a117d3ce2e33b4}"
export TEAMMATE_API_KEY="${TEAMMATE_API_KEY:-sk-1e61b6de1f9b4ccab4a117d3ce2e33b4}"
export API_BASE="${API_BASE:-https://dashscope.aliyuncs.com/compatible-mode/v1}"
export MODEL_NAME="${MODEL_NAME:-glm-5}"

exec python tests/system_tests/agent_swarm/interactive_team_cli.py "$@"
