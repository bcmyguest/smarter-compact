#!/usr/bin/env bash
# The hard-tail experiment: SWE-bench Verified instances officially labeled
# "1-4 hours"/">4 hours" (45 total), under compression pressure —
# smart arm trims verbatim at 30k (+ search recovery), none arm natively
# compacts (LLM summary) at 32k. mid47 showed peaks up to 64.6k on easy tasks;
# these tasks should blow through the threshold everywhere.
#
# Usage:
#   ANTHROPIC_API_KEY=sk-... ./run-hard45.sh anthropic claude-sonnet-4-5
#   ./run-hard45.sh cairn2 Qwen/Qwen3.8-Flash-Next-FP8          # rerun Qwen for the matrix
#   CONTEXT_LIMIT=200000 PARALLEL=6 ./run-hard45.sh anthropic claude-opus-4-1
#   TRIM_AT=80000 ./run-hard45.sh anthropic claude-haiku-4-5   # trim/native move together
#
# TRIM_AT (default 40000) sets the smart arm's verbatim trim; NATIVE_AT defaults
# to TRIM_AT+2000 so the native arm compacts just after the smart one trims.
#
# Images: 43 are not local; docker pulls them lazily per trial and
# --evict-images frees them as each repo drains (jobs run repo-grouped).
# Disk headroom matters more than CPU here: run `docker image prune -f` first
# if free space drops under ~40G.
set -euo pipefail
cd "$(dirname "$0")"

PROVIDER="${1:?usage: run-hard45.sh <provider> <model-id>   e.g. anthropic claude-sonnet-4-5}"
MODEL="${2:?usage: run-hard45.sh <provider> <model-id>}"
CONTEXT_LIMIT="${CONTEXT_LIMIT:-200000}"   # claude=200k; pass 102400 for the Qwen rerun
PARALLEL="${PARALLEL:-4}"
RUN_ID="${RUN_ID:-hard45-${MODEL##*/}}"
DISNAME="${DISNAME:-${MODEL##*/}}"
TRIM_AT="${TRIM_AT:-40000}"
NATIVE_AT="${NATIVE_AT:-$((TRIM_AT + 2000))}"   # native fires just above trim so the smart arm trims first

KEY_SRC="ANTHROPIC_API_KEY (exported)"
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  # repo-root .env fallback (Claude subscription token)
  ANTHROPIC_API_KEY="$(grep -m1 '^CLAUDE_API_KEY=' ../../.env 2>/dev/null | cut -d= -f2-)"
  export ANTHROPIC_API_KEY
  KEY_SRC="repo-root .env CLAUDE_API_KEY"
fi
if [ "$PROVIDER" = "anthropic" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "ANTHROPIC_API_KEY is not exported — the trial config would get an empty key." >&2
  exit 1
fi

# Say which credential the trials authenticate with, and where it came from:
# sk-ant-api… bills to the Anthropic API console, sk-ant-usr… is the Claude
# subscription token from the repo-root .env and shows up in NO console usage page.
if [ "$PROVIDER" = "anthropic" ]; then
  echo "credential=${ANTHROPIC_API_KEY:0:10}… source=$KEY_SRC"
fi

# INSTANCES/ARMS/TIMEOUT are overridable so a single-instance probe runs through
# this same launch path as the full sweep (a probe that differs from the real run
# in how it starts opencode proves nothing).
INSTANCES="${INSTANCES:-$(venv/bin/python -c "import json; print(','.join(r['instance_id'] for r in json.load(open('data/hard45.json'))['instances']))")}"
ARMS="${ARMS:-none,smart}"
TIMEOUT="${TIMEOUT:-3600}"
echo "run-id=$RUN_ID provider=$PROVIDER model=$MODEL context=$CONTEXT_LIMIT parallel=$PARALLEL trim=$TRIM_AT native=$NATIVE_AT arms=$ARMS timeout=$TIMEOUT instances=$(echo "$INSTANCES" | tr ',' '\n' | wc -l)"

# Request capture is on by default (run_bench.py: BENCH_CAPTURE defaults to "1"),
# so every trial already writes results/$RUN_ID/<arm>/<instance>/requests/
# {index.jsonl,NNNN-*.json.gz} -- the only way to see what the model was actually
# offered (tool names, the trim pointer). Set BENCH_CAPTURE=0 to skip it.
exec env BENCH_MLFLOW=1 venv/bin/python run_bench.py \
  --run-id "$RUN_ID" \
  --instances "$INSTANCES" \
  --arms "$ARMS" \
  --provider "$PROVIDER" \
  --model "$MODEL" \
  --display-name "$DISNAME" \
  --trim-at "$TRIM_AT" \
  --native-at "$NATIVE_AT" \
  --context-limit "$CONTEXT_LIMIT" \
  --parallel "$PARALLEL" \
  --timeout "$TIMEOUT" \
  --evict-images \
  --skip-batch-score
