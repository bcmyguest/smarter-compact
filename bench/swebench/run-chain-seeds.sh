#!/usr/bin/env bash
# Multi-seed chained SWE sessions: N seeds x arms (none = native compaction, smart = trim + recall),
# one 8-task django chain per arm per seed. Each seed shuffles the chain order (identical for both
# arms) and gets its own run id, so seeds are independent sessions.
#
# Tasks: the chain-django8 set (first 8 django ids of data/hard45.json, sorted).
# Trim 40k: flat hard45-cairn-40k already trimmed 1-2 times on 6 of these 8 tasks alone, and
# chained django grew ~15k peak per task (chain-smoke-5: 27k -> 43k -> 58k), so an 8-task chain
# should cross 40k well over 3 times. Gate: chain_report.py trims column >= 3 per smart chain.
#
# Usage:
#   ./run-chain-seeds.sh                      # seeds 1..3, cairn2 Qwen
#   SEEDS="1 2 3 4 5" PARALLEL=2 ./run-chain-seeds.sh
#
# Model policy: cairn2 only (see run-hard45.sh); the anthropic provider bills CLAUDE_API_KEY.
set -euo pipefail
cd "$(dirname "$0")"

PROVIDER="${PROVIDER:-cairn2}"
MODEL="${MODEL:-Qwen/Qwen3.8-Flash-Next-FP8}"
CONTEXT_LIMIT="${CONTEXT_LIMIT:-102400}"
SEEDS="${SEEDS:-1 2 3}"
ARMS="${ARMS:-none,smart}"
TRIM_AT="${TRIM_AT:-40000}"
PARALLEL="${PARALLEL:-2}"
TIMEOUT="${TIMEOUT:-3600}"
PY="${PY:-venv/bin/python}"
INSTANCES="${INSTANCES:-$("$PY" -c "import json; print(','.join(sorted(i['instance_id'] for i in json.load(open('data/hard45.json'))['instances'] if i['repo'] == 'django/django')[:8]))")}"

if [ "$PROVIDER" = "anthropic" ]; then
  echo "refusing provider=anthropic: chained runs stay on cairn2 (see run-hard45.sh model policy)" >&2
  exit 1
fi

for seed in $SEEDS; do
  run_id="chain-django8-t$((TRIM_AT / 1000))k-s$seed"
  echo "run-id=$run_id seed=$seed arms=$ARMS trim=$TRIM_AT model=$MODEL instances=$INSTANCES"
  BENCH_MLFLOW=1 "$PY" run_bench.py \
    --run-id "$run_id" \
    --instances "$INSTANCES" \
    --arms "$ARMS" \
    --provider "$PROVIDER" \
    --model "$MODEL" \
    --trim-at "$TRIM_AT" \
    --context-limit "$CONTEXT_LIMIT" \
    --parallel "$PARALLEL" \
    --timeout "$TIMEOUT" \
    --chain \
    --chain-seed "$seed" \
    --skip-batch-score
  "$PY" chain_report.py --run-id "$run_id"
done
