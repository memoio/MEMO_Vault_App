#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
python=$(printenv AL_CONTROL_PYTHON || printenv AL_PYTHON || printf python)
export PYTHONPATH="$project_dir/src"
exec "$python" -m everos.agent_training.agentlab_api_compat "$@"
