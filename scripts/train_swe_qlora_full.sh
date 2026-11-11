#!/usr/bin/env bash
set -euo pipefail
if (( $# < 3 )); then echo 'Usage: train_swe_qlora_full.sh PREPARED_DATA MODEL_DIR RUN_DIR [--epochs 1.0] [--prepare-only]' >&2; exit 2; fi
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"
data=$1; model=$2; run=$3; shift 3
exec "${SWE_PYTHON:-python}" -m everos.agent_training.swe_train_run --mode full --data "$data" --model "$model" --run "$run" "$@"
