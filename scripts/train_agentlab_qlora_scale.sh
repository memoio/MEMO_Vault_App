#!/usr/bin/env bash
set -euo pipefail
if (( $# < 3 )); then
  echo 'Usage: train_agentlab_qlora_scale.sh PREPARED_DATA_DIR MODEL_DIR RUN_DIR [extra options...]' >&2
  exit 2
fi
data_dir=$1; model_dir=$2; run_dir=$3; shift 3
project_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
python=$(printenv AL_PYTHON || printf python)
steps=$(printenv AL_SCALE_STEPS || printf 50)
eval_steps=$(printenv AL_EVAL_STEPS || printf 10)
export PYTHONPATH="$project_dir/src"
exec "$python" -m everos.agent_training.agentlab_qlora --mode scale --data-dir "$data_dir" --model "$model_dir" --run-dir "$run_dir" --cutoff-len 262144 --max-steps "$steps" --eval-steps "$eval_steps" "$@"
