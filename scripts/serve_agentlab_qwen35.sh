#!/usr/bin/env bash
set -euo pipefail
if (( $# < 3 )); then
  echo 'Usage: serve_agentlab_qwen35.sh MODEL_DIR base|adapter PORT [ADAPTER_DIR]' >&2
  exit 2
fi
model_dir=$1
mode=$2
port=$3
adapter_dir=""
if (( $# >= 4 )); then adapter_dir=$4; fi
project_dir=$(cd -- "$(dirname -- "$0")/.." && pwd)
python=$(printenv AL_PYTHON || printf python)
export PYTHONPATH="$project_dir/src"
case "$mode" in
  base) if [[ -n "$adapter_dir" || $# -ne 3 ]]; then echo 'base mode takes exactly MODEL_DIR base PORT' >&2; exit 2; fi ;;
  adapter) if [[ -z "$adapter_dir" || $# -ne 4 ]]; then echo 'adapter mode requires ADAPTER_DIR' >&2; exit 2; fi ;;
  *) echo 'mode must be base or adapter' >&2; exit 2 ;;
esac
if ! [[ "$port" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then echo "invalid TCP port: $port" >&2; exit 2; fi
if [[ "$mode" == adapter ]]; then
  [[ -f "$adapter_dir/adapter_config.json" ]] || { echo "adapter_config.json not found: $adapter_dir" >&2; exit 1; }
  [[ -f "$adapter_dir/adapter_model.safetensors" || -f "$adapter_dir/adapter_model.bin" ]] || { echo "adapter weights not found: $adapter_dir" >&2; exit 1; }
fi
verify_model() {
  "$python" - "$@" <<'PY'
import sys
from importlib.metadata import version
from everos.agent_training.agentlab_qwen_model import model_fingerprint
if version("llamafactory") != "0.9.5":
    raise SystemExit(f"LLaMA-Factory 0.9.5 required, found {version('llamafactory')}")
adapter = sys.argv[2] if len(sys.argv) > 2 else None
identity = model_fingerprint(sys.argv[1], adapter)
print("Verified Qwen3.5 model identity:", identity["fingerprint"], flush=True)
PY
}
if [[ "$mode" == adapter ]]; then verify_model "$model_dir" "$adapter_dir"; else verify_model "$model_dir"; fi
api_host=$(printenv AL_API_HOST || printf 127.0.0.1)
if [[ "$api_host" != 127.0.0.1 ]]; then echo 'model API must bind to 127.0.0.1' >&2; exit 2; fi
export API_HOST=127.0.0.1
export API_PORT="$port"
export API_MODEL_NAME=$(printenv AL_API_MODEL_NAME || printf qwen35)
export API_KEY=$(printenv AL_API_KEY || printf local)
set -- api --model_name_or_path "$model_dir" --template qwen3_5_nothink --infer_backend huggingface --quantization_bit 4 --quantization_method bnb --quantization_type nf4 --double_quantization true
if [[ "$mode" == adapter ]]; then set -- "$@" --finetuning_type lora --adapter_name_or_path "$adapter_dir"; fi
exec "$python" -m llamafactory.cli "$@"
