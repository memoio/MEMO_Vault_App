#!/usr/bin/env bash
set -euo pipefail
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python=${OH_CONTROL_PYTHON:-${OH_PYTHON:-python}}
export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"
exec "$python" -m everos.agent_training.openhands_api_compat "$@"
