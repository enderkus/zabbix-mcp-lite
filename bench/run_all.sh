#!/usr/bin/env bash
# Full benchmark matrix: loads each model in LM Studio one at a time (only one model in
# memory) and runs every tool set. Results go to bench/results/<timestamp>.jsonl.
#
#   caffeinate -i bash bench/run_all.sh          # macOS: keep the machine awake
#   TOOLSETS=lite bash bench/run_all.sh          # only some tool sets
#
# Model keys are the ones used in LM Studio; adjust MODELS for your own downloads.
set -euo pipefail
cd "$(dirname "$0")/.."
LMS="${LMS:-$HOME/.lmstudio/bin/lms}"
OUT="${OUT:-bench/results/$(date +%Y%m%d-%H%M%S).jsonl}"
TOOLSETS="${TOOLSETS:-official helper lite}"

# model key | context length | runs per tool set
MODELS=(
  "qwen3.5-4b-mlx|32768|3"
  "qwen/qwen3-4b-2507|32768|2"
  "qwen/qwen3-4b-thinking-2507|32768|1"
  "google/gemma-4-12b|16384|2"
)

for entry in "${MODELS[@]}"; do
  IFS='|' read -r model ctx runs <<<"$entry"
  echo "=== $model (context $ctx, $runs runs per tool set)"
  "$LMS" unload --all >/dev/null 2>&1 || true
  "$LMS" load "$model" --context-length "$ctx" -y >/dev/null
  for toolset in $TOOLSETS; do
    python3 -I bench/run_bench.py --model "$model" --toolset "$toolset" --runs "$runs" --out "$OUT"
  done
done

echo
python3 -I bench/run_bench.py --report "$OUT"
