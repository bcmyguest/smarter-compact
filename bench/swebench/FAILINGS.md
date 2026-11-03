# Benchmark failings log

Qwen 3.8 Flash Next (local endpoint), OpenCode 2.2.5, smarter-compact working tree.
Run dirs: `bench/swebench/results/<run-id>/`.

## Fixed during harness bring-up (harness failures, not solution failures)

| # | Failure | Root cause | Fix |
|---|---------|-----------|-----|
| H1 | All trials died instantly: `exec_run() unexpected kwarg 'timeout'` | docker-py `Container.exec_run` has no timeout param | thread-based timeout + `container.kill()` |
| H2 | Scoring crash `KeyError 'model_name_or_path'` | swebench 5 predictions schema renamed the field | write both `model_name` and `model_name_or_path` |
| H3 | Scoring crash `KeyError 'eval_script'` | `princeton-nlp/SWE-bench_Verified` is the legacy format | switched to `SWE-bench/SWE-bench_Verified` (ships `image` + `eval_script`) |
| H4 | Patches 870KB of junk (`build/lib/...`, egg-info) | `git add -A` after test runs stages build byproducts | seed `.git/info/exclude` before the agent starts |
| H5 | `trims: null` despite the plugin running | store path is `$SMARTER_COMPACT_STATE_DIR/context/sessions`, trims are `events(kind='trim')` rows | query corrected |
| H6 | `find: Permission denied` on trial state dir | container writes as root; bench user couldn't read | `chmod -R a+rX /opt/trial` before container removal |

## Fixed in the product/bench (real failings of our own stack)

| # | Failure | Root cause | Fix |
|---|---------|-----------|-----|
| P1 | Docs said `opencode plugin "$PWD"` / `opencode plugin ./index.mjs`; binary rejects local paths (`plugin add` is npm/Git-only) | docs drifted from OpenCode 2.2.5 CLI | docs rewritten: `plugin add smarter-compact` / `<user>/repo`; local clones via absolute path in `plugins: []` |
| P2 | Search MCP tools were NOT available in a trimmed session unless every user hand-wrote an `mcp` config entry | v2 plugin only registered hooks | plugin self-registers via `ctx.mcp.add` (`src/v2.mjs`); smoke bench turn 4 proves a call |
| P3 | Bench script name embedded the provider | naming | renamed `bench-opencode2.mjs`, provider/model via env, required not defaulted |

## Under observation (model/agent behavior)

- O1 (smoke2, none arm): agent attempted a **web search** ("Web search cancelled") — no search key in the container; should we deny `websearch` in bench config for determinism?
- O2 (smoke2, none arm): repeated `oldString and newString are identical` edit errors — model flails on the edit tool; arm exit 1 after 36 otherwise-clean steps (no failure event in the stream). Watch whether scored resolution is affected.
- O3 (smoke2, both arms): `cache_read: 0` in every step — prompt caching is not engaging on this endpoint; trim's cache-stability benefit is invisible in these numbers.
- O4 (smoke3): `trims` count came back `None` from the host because a WAL-mode session db cannot be safely re-opened read-only outside the container (recovery/WAL loss). Fixed: count `events(kind='trim')` inside the container before removal.

## Fixed after smoke3

| # | Failure | Root cause | Fix |
|---|---------|-----------|-----|
| H7 | Trim count unreliable on host | WAL-mode db, uncheckpointed rows, read-only reopen errors (`attempt to write a readonly database`) | count in-container via the image's python before removal |
| O1 action | Web search attempted in-container (no key) | agent tool available, endpoint offline | bench config now sets `permission: {websearch: deny}` |
| H8 | First multiagent run drove the agent inside the REAL smarter-compact repo, and a later run scored `subagent calls: 0` despite the session DB showing real `explore` invocations | (1) `bench-multiagent.mjs` never passed `cwd` to the agent turns; (2) subagent counting regex `/"tool":"task"/` cannot match — the v2 JSON stream never emits tool inputs | `cwd: project` on every turn (repo working tree verified untouched); subagent calls now counted from the session DBs (`tool_use` rows containing `{"agent":"…"}`) |
| H9 | Resuming an interrupted run failed instantly: `409 Conflict … container name already in use` | trial containers use a fixed name (`scbench-<iid>-<arm>-<run-id>`); an interrupted run leaves stopped containers on those names, and resume reuses the run-id | `run_trial` force-removes any stale container on the target name before `containers.run` |
