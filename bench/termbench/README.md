# termbench — Terminal-Bench runner for the smarter-compact arms

Same arms, same agent, same trimming and the same metrics as the SWE-bench runner in
`../swebench`, run over Terminal-Bench's task set instead of GitHub issues. Terminal-Bench
is where the arms have headroom: hard45 is nearly saturated for these models, so an arm
difference there is hard to measure, while TB's 241 local tasks carry an author-declared
`difficulty` and grade with pytest against a task's own `test_outputs.py`.

```
bench/termbench/
├── tb_bench.py          # runner (thin: TB-specific parts only)
├── tb/original-tasks/   # vendored Terminal-Bench tasks (241 dirs)
└── results/             # per-trial artefacts (gitignored)
```

**The trial itself is not reimplemented here.** Container mounts, the trial-local
`opencode.json` (provider, compaction, plugin, MCP), token extraction, the trim/recall
ledger, the request forwarder and cost all come from `../swebench/run_bench.py`, which has
a measured history behind it. What lives in `tb_bench.py` is only what TB does
differently: `task.yaml` prompts, a docker build per task, and scoring through the task's
own tests.

## Running

Needs docker, the SWE-bench venv, and the cairn2 proxy answering on `127.0.0.1:8800` (trials
run with `network=host`, so they reach the host's proxy directly). The plugin needs no
build step: `package.json` main is `./index.mjs`, and the runner bind-mounts the repo into
each trial, so opencode loads the plugin straight from source — an edit to `index.mjs`
takes effect on the next trial.

```bash
../swebench/venv/bin/python tb_bench.py --run-id tb-probe \
    --sample easy=4,medium=4,hard=4 --arms smart,none \
    --provider cairn2 --model Qwen/Qwen3.8-Flash-Next-FP8
```

| flag | default | meaning |
|---|---|---|
| `--sample` | `easy=4,medium=4,hard=4` | how many tasks per `difficulty`, drawn with `--seed` (7) |
| `--tasks` | – | comma-separated task names; bypasses sampling |
| `--arms` | `smart` | `smart`, `none`, or both |
| `--trim-at` / `--native-at` | 95000 | smart-arm trim / native-compaction thresholds |
| `--context-limit` | 200000 | model context as seen by opencode |
| `--parallel` | 4 | concurrent trials |
| `--timeout` / `--test-timeout` | 900 / 600 | agent wall clock / grader wall clock |

Provider is `cairn2` by default and bench runs stay on it: the `anthropic` provider would
bill the `CLAUDE_API_KEY` in the repo-root `.env` rather than anything exported in the
shell, so its spend does not show up where you would look for it.

## Which tasks are eligible

A task enters the sampled pool only if all of these hold, so that failures mean something
about context management and nothing about the harness:

- `tests/test_outputs.py` exists — otherwise there is nothing to score.
- Its own `Dockerfile` contains a `FROM` — tasks that build through a `docker-compose`
  context we don't reproduce are skipped rather than guessed at.
- Its name misses `EXCLUDE` (GPU, kernel/QEMU builds, model training, database servers,
  anything needing a third-party download).

That leaves **215 of 241** tasks: 62 easy, 106 medium, 47 hard. Both `t-bench` base images
are pre-pulled, so builds run with `pull=False`.

## Grading

`run-tests.sh` is a *sibling* of `tests/` in a TB task, not inside it; the runner copies
`tests/` plus that script into the trial directory, which is mounted at `/opt/trial`, and
runs it with `TEST_DIR=/opt/trial/tests` in the same container and workdir the agent used.
If a task has no `run-tests.sh`, `pytest /opt/trial/tests/test_outputs.py -rA` is used.
`exit 0` means resolved.

A grader that cannot start (126/127) is recorded `resolved: null` and excluded from the
pass rate, so a harness problem never reads as a model failure — same for
`error: "agent: timeout"`.

## How the threshold reaches the plugin, and the control that proves it

`--trim-at` becomes `CONTEXT_TRIM_AT_TOKENS` in the agent's exec environment; the plugin
reads it in `plugin/lib/context-config.mjs` (via a passed `env` object, which is why a
`process.env.CONTEXT_TRIM_AT_TOKENS` grep comes up empty — the real knob is
`env.CONTEXT_TRIM_AT_TOKENS`, and `~/.config/smarter-compact/config.toml` fills it only
when unset). Arms are separated by config, not by code path: `run_bench.write_agent_config`
adds `plugins: [<repo>]` **only** for `arm == "smart"`, so the `none` arm is opencode's
native compaction alone.

Proven with a control rather than assumed — same task (`pandas-etl`), same model, two
thresholds:

| `--trim-at` | peak context | trims |
|---|---|---|
| 8,000 | 11.6k | 6 |
| 190,000 | 13.6k | 0 |

Nonzero at 8k and zero at 190k means the threshold drives trimming. Re-run that control
after changing anything in the trim path: a run where both arms trim identically, or where
a 190k threshold still trims, means the knob broke.

**Native compaction cannot be pinned arbitrarily high.** `write_agent_config` sets
`reserved = max(output_limit, context_limit − native_at)`, and opencode compacts at
`context_limit − reserved`. With `--context-limit 200000 --native-at 195000` the
`output_limit` of 16384 wins, so native fires at **183.6k**, not 195k. Reaching 195k would
need an output budget ≤5000 tokens. What matters for the comparison is the ordering —
95k trim happens ~88k before native — but read `--native-at` as "at or below", not exact.

## Interpreting

Same rules as the SWE-bench side: compare arms only at equal `model`, read `resolved` next
to peak context and trim counts, and discard a run where no trial reached `trim_at` — the
summary prints that warning explicitly.

Task length is the binding constraint here, not the threshold: TB tasks are much shorter
than SWE-bench issues, and easy ones peak around **12–14k in ~25s**. At the 95k threshold a
random sample trims nowhere, so pick tasks by declared budget rather than by `--sample` —
the offline hard tasks declaring `max_agent_timeout_sec: 1800`
(`llm-inference-batching-scheduler`, `protein-assembly`, `dna-assembly`,
`feal-differential-cryptanalysis`) are the ones with any chance of reaching 95k. Lowering
`--trim-at` instead would make the arms differ, but at a pressure point the product never
runs at.

Per-trial `meta.json` additionally records `difficulty`, `category`, `image`, `workdir`,
`capture_port`, the trim/recall counts, and `req_*` request-capture facts for the smart
arm.

## Harbor runs: verifiers without a GitHub download

Terminal-Bench 2.x verifiers install uv 0.9.5 from astral.sh and Python 3.13 from GitHub on every trial. In the TB2.1 runs of 2026-11-07, about 16 of 35 failures were these downloads returning 504, in every arm. `harbor/pin-verifier-uv.sh <dataset> <out dir>` copies the dataset with each verifier using a uv and Python mounted at `/opt/sc-uv` instead; the job then adds a bind mount of `/tmp/opencode/sc-uv` at `/opt/sc-uv` and points its dataset path at the copy. The agent's environment is unchanged except for that directory.
