#!/usr/bin/env bash
set -euo pipefail
output_dir=${1:?usage: download_agentlab_adp_sft.sh OUTPUT_DIR}
revision=31a76bfb0124d77ae7322eabbb0171bf11ee2c67
expected=9b5fe4336aebb3e7f84143e1705785376b5180b919fea37adb6a976454274120
file="$output_dir/mind2web.agentlab.jsonl"
mkdir -p "$output_dir"
if [[ -f "$file" ]]; then
  printf '%s  %s\n' "$expected" "$file" | sha256sum -c -
  exit 0
fi
temp=$(mktemp "$output_dir/.download.XXXXXXXX")
trap 'rm -f "$temp"' EXIT
curl -4 -L --fail --retry 3 --connect-timeout 20 -o "$temp" \
  "https://huggingface.co/datasets/neulab/agent-data-collection/resolve/$revision/mind2web/full_sft/full_sft_agentlab.jsonl"
printf '%s  %s\n' "$expected" "$temp" | sha256sum -c -
ln "$temp" "$file"
