#!/usr/bin/env bash
set -euo pipefail
# Explicit destination; existing files are never overwritten.
output_dir=${1:?usage: download_openhands_adp_sft.sh OUTPUT_DIR}
revision=31a76bfb0124d77ae7322eabbb0171bf11ee2c67
base=https://huggingface.co/datasets/neulab/agent-data-collection/resolve/$revision
mkdir -p "$output_dir"
download() {
  local name=$1 remote=$2 expected=$3 temp
  if [[ -f "$output_dir/$name" ]]; then
    printf '%s  %s\n' "$expected" "$output_dir/$name" | sha256sum -c -
    return
  fi
  temp=$(mktemp "$output_dir/.download.XXXXXXXX")
  if ! curl -4 -L --fail --retry 3 --connect-timeout 20 -o "$temp" "$base/$remote"; then
    rm -f "$temp"
    return 1
  fi
  if ! printf '%s  %s\n' "$expected" "$temp" | sha256sum -c -; then
    rm -f "$temp"
    return 1
  fi
  if ! ln "$temp" "$output_dir/$name"; then
    rm -f "$temp"
    return 1
  fi
  rm -f "$temp"
}
download agenttuning_os.jsonl agenttuning_os/full_sft/full_sft_openhands.jsonl 7879f2122fd07fab5382b6289beeb25c352f5c35c4b305e359f9718fd1805e61
download codeactinstruct.jsonl codeactinstruct/full_sft/full_sft_openhands.jsonl d01452a99787e722a1e86ff62cf965c668f39ddbf968a0cfcac428cb4b3aab80
