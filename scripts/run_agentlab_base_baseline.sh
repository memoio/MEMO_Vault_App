#!/usr/bin/env bash
set -euo pipefail
if (( $# < 3 )); then
  echo 'Usage: run_agentlab_base_baseline.sh PREPARED_DATA_DIR OUTPUT_DIR MODEL_FINGERPRINT [eval options...]' >&2
  exit 2
fi
data_dir=$1
output_dir=$2
fingerprint=$3
shift 3
project_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
python=$(printenv AL_CONTROL_PYTHON || printenv AL_PYTHON || printf python)
export PYTHONPATH="$project_dir/src"
exec "$python" -m everos.agent_training.agentlab_mind2web_eval --data-dir "$data_dir" --model-tag base --model-fingerprint "$fingerprint" --output-dir "$output_dir" "$@"
