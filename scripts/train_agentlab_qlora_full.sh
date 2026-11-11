#!/usr/bin/env bash
set -euo pipefail
if (( $# < 3 )); then
  echo 'Usage: train_agentlab_qlora_full.sh PREPARED_DATA_DIR MODEL_DIR RUN_DIR [extra options...]' >&2
  exit 2
fi
data_dir=$1; model_dir=$2; run_dir=$3; shift 3
project_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
python=$(printenv AL_PYTHON || printf python)
epochs=$(printenv AL_FULL_EPOCHS || printf 1)
eval_steps=$(printenv AL_EVAL_STEPS || printf 100)
export PYTHONPATH="$project_dir/src"
exec "$python" -m everos.agent_training.agentlab_qlora --mode full --data-dir "$data_dir" --model "$model_dir" --run-dir "$run_dir" --cutoff-len 262144 --epochs "$epochs" --eval-steps "$eval_steps" "$@"
