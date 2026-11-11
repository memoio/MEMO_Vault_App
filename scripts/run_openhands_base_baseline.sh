#!/usr/bin/env bash
set -euo pipefail

if (( $# < 2 )); then
  echo 'Usage: run_openhands_base_baseline.sh TASKS_JSONL OUTPUT_DIR [additional eval options]' >&2
  exit 2
fi

tasks=$1
output=$2
shift 2
project_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
control_python=${OH_CONTROL_PYTHON:-python}
openhands_repo=${OH_REPO:?set OH_REPO to the pinned OpenHands 0.30.0 checkout}
network=${OH_NETWORK:?set OH_NETWORK to the precreated internal Docker network}
base_url=${OPENAI_BASE_URL:-http://127.0.0.1:18082/v1}
model_name=${OH_MODEL_NAME:-openai/qwen35}
api_model_name=${API_MODEL_NAME:-qwen35}
api_key=${OH_EVAL_API_KEY:-local}

models=$(curl --fail --silent --show-error -H "Authorization: Bearer $api_key" "$base_url/models")
printf '%s' "$models" | "$control_python" -c \
  'import json,sys; data=json.load(sys.stdin); expected=sys.argv[1]; ids=[x["id"] for x in data.get("data",[])]; assert expected in ids, (expected, ids)' \
  "$api_model_name"

completion=$(curl --fail --silent --show-error "$base_url/chat/completions" \
  -H 'Content-Type: application/json' -H "Authorization: Bearer $api_key" \
  -d "{\"model\":\"$api_model_name\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with OK only.\"}],\"max_completion_tokens\":16,\"temperature\":0}")
printf '%s' "$completion" | "$control_python" -c \
  'import json,sys; data=json.load(sys.stdin); content=data["choices"][0]["message"]["content"]; assert isinstance(content,str) and content.strip(), data; print("Qwen API completion check passed:", content[:120])'

PYTHONPATH="$openhands_repo:$project_dir/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$control_python" -m everos.agent_training.openhands_eval run \
  --tasks "$tasks" --output "$output" --openhands-repo "$openhands_repo" \
  --mode base --network "$network" --model-name "$model_name" --base-url "$base_url" \
  --max-iterations "${OH_MAX_ITERATIONS:-10}" \
  --max-output-tokens "${OH_MAX_OUTPUT_TOKENS:-512}" \
  --timeout "${OH_TASK_TIMEOUT:-300}" "$@"
