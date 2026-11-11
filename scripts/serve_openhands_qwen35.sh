#!/usr/bin/env bash
set -euo pipefail

if (( $# < 3 )); then
  echo 'Usage: serve_openhands_qwen35.sh MODEL_DIR base|adapter PORT [ADAPTER_DIR]' >&2
  exit 2
fi

model_dir=$1
mode=$2
port=$3
adapter_dir=${4:-}
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
python=${OH_PYTHON:-python}
export PYTHONPATH="$project_dir/src${PYTHONPATH:+:$PYTHONPATH}"

case "$mode" in
  base)
    if [[ -n "$adapter_dir" || $# -ne 3 ]]; then
      echo 'base mode takes exactly MODEL_DIR base PORT' >&2
      exit 2
    fi
    ;;
  adapter)
    if [[ -z "$adapter_dir" || $# -ne 4 ]]; then
      echo 'adapter mode requires ADAPTER_DIR' >&2
      exit 2
    fi
    ;;
  *)
    echo 'mode must be base or adapter' >&2
    exit 2
    ;;
esac

[[ -d "$model_dir" ]] || { echo "model directory not found: $model_dir" >&2; exit 1; }
if [[ "$mode" == adapter ]]; then
  [[ -f "$adapter_dir/adapter_config.json" ]] || { echo "adapter_config.json not found: $adapter_dir" >&2; exit 1; }
  [[ -f "$adapter_dir/adapter_model.safetensors" || -f "$adapter_dir/adapter_model.bin" ]] || {
    echo "adapter weights not found: $adapter_dir" >&2
    exit 1
  }
fi

"$python" - "$model_dir" <<'PY'
import sys
from importlib.metadata import version
from pathlib import Path

from everos.agent_training.openhands_inventory import sha256_file
from everos.agent_training.openhands_qlora import MODEL_HASHES, TOKENIZER_HASHES

model = Path(sys.argv[1])
actual_version = version('llamafactory')
if actual_version != '0.9.5':
    raise SystemExit(f'LLaMA-Factory 0.9.5 required, found {actual_version}')
for name, expected in {**TOKENIZER_HASHES, **MODEL_HASHES}.items():
    path = model / name
    if not path.is_file():
        raise SystemExit(f'model file missing: {path}')
    actual = sha256_file(path)
    if actual != expected:
        raise SystemExit(f'model file hash mismatch: {name}: {actual}')
print('LLaMA-Factory 0.9.5 and fixed Qwen3.5 model hashes verified.')
PY

if ! [[ "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
  echo "invalid TCP port: $port" >&2
  exit 2
fi

args=(api
  --model_name_or_path "$model_dir"
  --template qwen3_5_nothink
  --infer_backend huggingface
  --finetuning_type lora
  --quantization_bit 4
  --quantization_method bnb
  --quantization_type nf4
  --double_quantization true)
if [[ "$mode" == adapter ]]; then
  args+=(--adapter_name_or_path "$adapter_dir")
fi

export API_HOST=127.0.0.1
export API_PORT="$port"
export API_MODEL_NAME=qwen35
export API_KEY=${OH_API_KEY:-local}
exec "$python" -m llamafactory.cli "${args[@]}"
