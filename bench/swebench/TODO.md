# Benchmark TODO

Bench: SWE-bench Verified (the benchmark of The Complexity Trap / JetBrains, the
compaction study this repo builds on). Model under test: Qwen 3.8 Flash Next
(102k window). Arms: `none` (native opencode compaction at --native-at) vs
`smart` (smarter-compact trim at --trim-at, native behind as guard).

## Install path (docs-first)
- [x] Verify `opencode plugin` installs the plugin per docs — `plugin add smarter-compact` (npm) and `<user>/smarter-compact` (git) work; local paths rejected by `plugin add`
- [x] Fix plugin/bench/docs wherever the documented install diverges from reality — docs rewritten; plugin self-registers MCP via `ctx.mcp.add`; local clones documented via config path
- [x] Smoke bench (`scripts/bench-opencode2.mjs`) passes through the documented install (turn 4 proves smart_compact_search self-registers and is called)

## SWE-bench Verified runs
- [x] Smoke: 1 instance x both arms, patch collected, harness scores it — resolved 1/1 in BOTH arms (smoke3)
- [x] full1: 6 instances x none/smart @ trim 30k (native default, model limits unset) — none 5/6, smart 4/6; only pylint crossed 30k (3 and 14 trims); smart used 20-63% fewer input tokens on every instance
- [~] full3: 23 instances x both arms — cancelled mid-run at 20 trials (superseded by mini pilot; results kept in results/pilot30/)
- [~] mid47: **JetBrains SWE-bench-Verified-mini** (MariusHobbhahn/swe-bench-verified-mini, distribution-matched 50: 25 django + 25 sphinx), filtered per methodology: dropped 6 <15-min-fix instances (tiny ref patch + single failing test + stub statement) and none >4h (no empirical timeouts seen), + 3 requests smoke = 47 x 2 arms @ trim 75k / native 80k, parallel 4, --evict-images (repo-grouped, no mid-run prune)
- [x] mid47 final: 44/47 both arms (93.6%), 0 trims (peaks 31.7k median / 64.6k max vs 75k threshold — tasks too easy, mechanism never engaged). Verdict: null by design failure, not mechanism failure.
- [~] hard45 staged: official Verified hard tail (42x "1-4 hours" + 3x ">4 hours") in data/hard45.json; the discriminating run at trim 30k / native 32k (none=LLM-summary compaction vs smart=verbatim trim+recall). Launcher run-hard45.sh: `ANTHROPIC_API_KEY=... ./run-hard45.sh anthropic <model-id>`; --provider anthropic resolves from env when absent from host config; 20 unused images evicted (55G free); images pull lazily per trial with repo-grouped order + evict.
- [ ] Claude-vs-Qwen matrix on hard45 once runs land
- [x] Status-report forensics (mid-run incident): plugin exonerated (pointer-only, no summaries); false "mini50 running" report was agent confabulation. Hardening fix shipped: pointer note + standing instruction now state explicitly that no summary exists (JS/Py twins byte-identical; 24/24 JS + 87/87 Py tests green). Reporting policy: every status claim cites a raw log line; post-trim numbers only via smart_compact_search citation.
- [x] Why the smart arms never recalled (hard45-haiku sweep) — two causes, both fixed:
  **(a) wrong code deployed.** `127.0.0.1:4100`, which `~/.claude/settings.json` (`ANTHROPIC_BASE_URL`) and
  `~/.codex/config.toml` (`base_url`) both point at, was served by the deprecated openmem compose container
  mounting `~/personal-memory/proxy`. Nothing changed in this repo could reach a running agent. Fixed: repo-root
  `compose.yaml` runs the pinned LiteLLM image with *this* `./proxy` mounted (same host port, same session store,
  so no agent config moved); openmem's `context-proxy` stopped — revert with
  `docker compose --profile context-proxy start context-proxy` in `~/personal-memory`.
  **(b) pointer named tools the client was never offered.** Both texts hard-coded bare `smart_compact_search`;
  harnesses namespace it (`smarter-compact_smart_compact_search` in OpenCode — underscore, not the dot this
  entry used to claim), so the note ordered a tool absent from the request. Names now resolve from each
  request's own `tools` (Py `recall_tool_names` / JS `recallToolNames`).
- [x] **(b) was NOT fixed on the JS/OpenCode-2 path — the shape, not the separator, was the bug.** This entry
  claimed `src/v2.mjs:132` already resolved names, so the bug was proxy-only. It was wrong: `recallToolNames`
  iterated `Array.isArray(tools) ? tools : []`, but OpenCode's `context` hook hands tools over **keyed by id**
  (`delete q.tools.edit` in its own bundle), so `offered` was always empty and both texts took the bare-name
  fallback in silence. Proven live, not inferred: a cairn2 trim (`results/namerun`, `0011-tools0-pointer`)
  ordered `smart_compact_search` in a request whose only recall tools were `smarter-compact_smart_compact_*`.
  Fixed by accepting list / string-list / keyed-object / `{definitions:[]}` shapes, with `seen` reported so a
  shape we cannot read can't masquerade as "recall not installed" (`standing_instruction … recall=3/15`).
  Re-verified end to end: `results/namerun2` pointer names are the offered names.
- [x] **`request_capture.py` counted our own tool description as a trim pointer.** `facts()` scanned
  `json.dumps(whole body)` for `[smarter-compact: continuing session`, and `smart_compact_search`'s
  description quotes exactly that phrase — so every request that merely *offered* recall read as pointed
  (254/528 in hard45-haiku-60k-cap), and `pointer_in_request` could never raise "trimmed but no pointer
  reached the model". Now scans messages+system only; `test_request_capture.py` pins both directions
  (marker in a description → not pointed; marker in a message → pointed) and fails on the old detector.
- [ ] Rerun the hard45 smart arm with `request_capture.py` on: it records `recall_tools` per request, which is
      the datum whose absence made the last sweep uninterpretable ("model ignored the tool" vs "tool never offered").
- [ ] 500 (full Verified) only as the final headline artifact; needs image pull→run→evict pipeline
- [x] Capture per-trial tokens, wall time, trims, resolved (meta.json + mlflow)

## Multi-agent long-session test
- [x] Long OpenCode coding session with subagents; context trims mid-session (run3: 59 trims, 6 explore-subagent calls, PASS)
- [x] Verify trimmed turns are retrieved via smart_compact_search (seed planted 300+ msgs back recalled after trims)

## Documentation
- [ ] Results table + failures log in docs (interim, dated)
- [x] File/fix every failing that is fixable; record the rest with reasons (FAILINGS.md H1-H9)
