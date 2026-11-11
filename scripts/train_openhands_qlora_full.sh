#!/usr/bin/env bash
set -euo pipefail
if (( $# < 4 )); then
  echo 'Usage: train_openhands_qlora_full.sh DATA_DIR MODEL_DIR RUN_DIR CUTOFF_LEN [additional options]' >&2
  exit 2
fi
data_dir=$1
model_dir=$2
run_dir=$3
cutoff_len=$4
shift 4
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"
exec "${OH_PYTHON:-python}" -m everos.agent_training.openhands_qlora \
  --mode full --data-dir "$data_dir" --model "$model_dir" \
  --run-dir "$run_dir" --cutoff-len "$cutoff_len" "$@"
