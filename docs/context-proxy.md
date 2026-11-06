# Context proxy

For Claude Code, OpenCode 1, and Codex, **Smarter Compact** runs a local **LiteLLM** process (`smarter-compact proxy`, or auto-started by the plugin) that rewrites each request before forwarding to the real model API:

1. **Tool-result compression.** Results over `CONTEXT_COMPRESS_OVER_TOKENS` become a short header plus `smart_compact_get(id=...)`; full text in `blobs/<id>.txt`.
2. **Store.** Messages go into `sessions/<sid>.db` ([session database](context-store.md#session-database)).
3. **Trim.** At `CONTEXT_TRIM_AT_TOKENS` or when `sessions/<sid>.force` exists, older turns drop from the request; dropped rows get `in_context = 0`; a pointer replaces them at the start of the kept turn.
4. **Recall on trim.** Optional when `CONTEXT_COMPACTION_RECALL=1` and the session registration names a bank ([format](context-store.md#proxy-inserted-text)).
5. **Standing instruction.** Every request gets a constant system note to use `smart_compact_search` before guessing about trimmed content.

The cut is sticky for prompt-cache stability. **OpenCode 2** applies the same rules in-process (`plugin/lib/context-trim.mjs`); it does not send traffic through this proxy.

Code: `proxy/smarter_compact/`, `proxy/smarter_compact_hook.py`, `proxy/config.yaml`. Tests: `python3 -m unittest discover -s proxy/tests`.

You need **LiteLLM v1.109.0-dev.1** (or newer with PR #41522) on `PATH` as `litellm` for Codex ChatGPT pass-through.

## Proxy port

Default listen port is **4101** on `127.0.0.1`.

| Mechanism | Precedence |
| --- | --- |
| `CONTEXT_PROXY_PORT` environment variable | wins |
| `CONTEXT_PROXY_PORT=` in `~/.config/smarter-compact/proxy.env` | used when env unset |
| Built-in default | `4101` |

Change port before or during enable:

```sh
export CONTEXT_PROXY_PORT=4200
smarter-compact enable claude
```

Or add `CONTEXT_PROXY_PORT=4200` to `proxy.env`, then run `smarter-compact enable` again so agent URLs match.

OpenCode 2 plugin JSON: `"proxyPort": 4200` (maps to `CONTEXT_PROXY_PORT` for settings and auto-start).

Auto-start is off when `SMARTER_COMPACT_AUTO_PROXY=0`. Logs: `~/.config/smarter-compact/proxy.log`, rotated to `proxy.log.1` at 5 MB on each start.

## Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `CONTEXT_PROXY_ENABLED` | `1` | `0` forwards requests unchanged |
| `CONTEXT_TRIM_AT_TOKENS` | unset → per-model | **Set** (env, `proxy.env`, OpenCode `trimAtTokens`, or `[context_proxy] trim_at_tokens` / `context set trim=`) → one threshold for every model. **Unset** → `[context_proxy.trim_at]` in `~/.config/smarter-compact/config.toml` merged with built-in `haiku=95000`, fallback `default=125000`. CLI: `smarter-compact context set haiku=88000`. |
| `CONTEXT_TRIM_TO_FRACTION` | `0.53` | In-turn tool retention fraction |
| `CONTEXT_TRIM_MIN_GAIN_TOKENS` | `0` | Skips a new trim that would free fewer estimated tokens than this (pointer note not counted); a forced trim (`/compact`) ignores it; `0` off. Without it, a trim fires whenever any message can drop: `CONTEXT_TRIM_TO_FRACTION` is a target, not a floor, and a long in-flight turn can leave a trim that frees little |
| `CONTEXT_STRIP_PROSE` | `0` | Strips tool calls and results from kept turns, keeping their text; `1` on, under review ([research](context-research.md#terminal-bench-21-trim-shape-arms-2026-11-05)) |
| `CONTEXT_MASK_OBSERVATIONS` | `1` | On by default (`0` turns it off): keeps each tool call in the same region and replaces its result (when longer) with a fixed placeholder; the trim pointer gains how to fetch it back from the store. Wins over `CONTEXT_STRIP_PROSE`. Proxy only; the OpenCode plugin trim does not mask ([research](context-research.md#prose-strip-versus-observation-masking-2026-11-05)) |
| `CONTEXT_CLEAR_KEEP_RESULTS` | `0` | `N` > 0: each trim first replaces every tool result except the N newest with the mask placeholder, keeping all turns and calls; oldest turns drop only when the request is still above `CONTEXT_TRIM_TO_FRACTION`. The cleared set stays fixed until the next trim. Experimental ([research](context-research.md#clearing-versus-turn-deletion-2026-11-05)) |
| `CONTEXT_SERVER_CLEAR` | `0` | `1` adds Anthropic's server-side tool-result clearing edit to Claude requests on `/v1/messages`, [below](#server-side-tool-result-clearing) |
| `CONTEXT_SERVER_CLEAR_TRIGGER_TOKENS` | `100000` | The edit's `trigger`: clearing starts once the prompt exceeds this many input tokens (Anthropic's default) |
| `CONTEXT_SERVER_CLEAR_KEEP_TOOL_USES` | `3` | The edit's `keep`: most recent tool use/result pairs never cleared (Anthropic's default) |
| `CONTEXT_SERVER_CLEAR_AT_LEAST_TOKENS` | `5000` | The edit's `clear_at_least`, the cache-break gate; `0` omits it |
| `CONTEXT_PROGRESS_NOTE` | `1` | Appends files edited and last build/test commands to the trim pointer; `0` off ([research](context-research.md#terminal-bench-21-trim-shape-arms-2026-11-05)) |
| `CONTEXT_CHARS_PER_TOKEN` | `2.5` | Estimate before usage calibration |
| `CONTEXT_COMPRESS_OVER_TOKENS` | `4000` | Tool compression threshold; `0` off |
| `CONTEXT_COMPACTION_MAX_CHARS` | `12000` | Max recall block size |
| `CONTEXT_COMPACTION_RECALL` | `0` in this package | `1` enables recall on trim |
| `CONTEXT_STANDING_INSTRUCTION` | `1` | `0` omits standing search note |
| `CONTEXT_PROXY_PORT` | `4101` | LiteLLM `--port` (see above) |
| `SMARTER_COMPACT_STATE_DIR` | `~/.local/share/smarter-compact` | Store parent; files under `context/` |
| `CONTEXT_STORE_TTL_DAYS` | `30` | Idle session cleanup at proxy start |
| `CONTEXT_EMBEDDING_MODEL` | empty | Hybrid search embeddings (optional) |

Secrets: `LITELLM_MASTER_KEY` in `~/.config/smarter-compact/proxy.env` (mode 600), created by `smarter-compact enable`.

## Server-side tool-result clearing

`CONTEXT_SERVER_CLEAR=1` (off by default) asks Anthropic to clear old tool results on its side ([context editing](https://docs.claude.com/en/docs/build-with-claude/context-editing)). On the Anthropic route (`/v1/messages`) to a model whose id contains `claude`, the proxy appends one edit to the request body:

```json
{"context_management": {"edits": [{"type": "clear_tool_uses_20250919",
  "trigger": {"type": "input_tokens", "value": 100000},
  "keep": {"type": "tool_uses", "value": 3},
  "clear_at_least": {"type": "input_tokens", "value": 5000}}]}}
```

- **Merge, not replace.** Claude Code already sends `context_management` on its requests, `{"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}` on all 25,310 requests that carried the field in 400 Terminal-Bench Claude Code transcripts (`keep: "all"` clears nothing); no `clear_tool_uses` edit appeared. The proxy appends after the client's edits (the API wants `clear_thinking` first) and leaves a request that already carries a `clear_tool_uses_20250919` edit unchanged.
- **Beta header.** LiteLLM's Anthropic messages transform adds `context-management-2025-06-27` to `anthropic-beta` for a body with `context_management` (`litellm/llms/anthropic/messages/native/transformation.py`).
- **`clear_at_least` is the cache-break gate.** Clearing rewrites the prompt from the first cleared result, so the cached prefix after that point is written again. The edit applies only when it frees at least this many input tokens; otherwise the request goes up uncleared and the cache stays warm.
- **The store keeps everything.** The session store records the client's request before any edit is applied upstream, so cleared results stay searchable with `smart_compact_search` / `smart_compact_sql`.
- **Logged per call.** Each `usage` [event](context-store.md#events) gets `applied_edits`: the response's `context_management.applied_edits` as compact JSON (`[]` when nothing cleared, `NULL` when not reported). For a stream, it comes from the final `message_delta`; with the flag on, the hook reads the tail of each stream for it, passing chunks through unchanged.

Live check, 2026-11-05: Claude Code 2.1.318 `claude -p` on `claude-haiku-5-5` through a proxy from this tree (trigger 20000, keep 1, clear_at_least 1000), four `seq` tool calls: five requests, all `200`, the last two recorded `[{"cleared_input_tokens":1518,"cleared_tool_uses":2,"type":"clear_tool_uses_20250919"}]` and `cleared_input_tokens` 1526, cache reads stayed 42,989 and 44,432 tokens on those calls, and the store held all four tool results. Cost $0.16.

## Token estimate

The trim uses a **calibrated estimate** from provider usage:

1. Pre-call hook counts request characters and keys them by `litellm_call_id`.
2. Success hook records `{chars, tokens}` per conversation thread in `.trim.json`.
3. The next request in the thread extrapolates with the observed chars/token ratio (bounded 1–8).

The estimate only decides **whether** to cut; it does not change an existing sticky cut.

## Enable

```sh
npm install -g smarter-compact   # or clone this repo
smarter-compact enable claude                 # writes settings + starts proxy if needed
```

Claude Code (`~/.claude/settings.json`):

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:4101",
    "ANTHROPIC_CUSTOM_HEADERS": "x-litellm-api-key: Bearer <LITELLM_MASTER_KEY>"
  }
}
```

Replace `4101` with your `CONTEXT_PROXY_PORT`. Do not put `ANTHROPIC_API_KEY` in the proxy environment; the client’s own credentials pass through.

Codex (`~/.codex/config.toml`):

```toml
model_provider = "smarter-compact"

[model_providers.smarter-compact]
name = "Smarter Compact context proxy"
base_url = "http://127.0.0.1:4101/backend-api/codex"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
http_headers = { "x-litellm-api-key" = "Bearer <LITELLM_MASTER_KEY>" }
```

`smarter-compact enable codex` writes this block and keeps ChatGPT login via `requires_openai_auth`.

## Run it in Docker (from this repo)

`compose.yaml` at the repo root runs the same proxy without installing LiteLLM: it starts the pinned LiteLLM image with `./proxy` mounted at `/app/proxy`, so **the requests are rewritten by the code in this checkout** — `docker compose restart context-proxy` is the deploy step.

```sh
HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose up -d context-proxy   # or put both in ./.env
docker compose logs -f context-proxy
```

It needs `LITELLM_MASTER_KEY` in `~/.config/smarter-compact/proxy.env` (mode 600; `CONTEXT_PROXY_ENV_FILE` overrides the path), listens on `127.0.0.1:${CONTEXT_PROXY_PORT:-4100}`, and writes the session store to `~/.local/share/smarter-compact/context` — falling back to the deprecated openmem service's `~/.local/share/openmem/context` when only that exists, so switching which repo serves it changes no agent config and orphans no session a running agent can still recall. Set `CONTEXT_PROXY_PORT=4101` to move onto the CLI default instead.

## Updates

The proxy writes `proxy-<port>.json` to the context dir at start, and at most every 5 s while it serves requests: its `package.json` version, pid, start time and last request. At each session start (and on `smarter-compact enable`), the plugin compares that version with the installed package:

| Proxy on the port | Idle 60 s or more | Busy |
| --- | --- | --- |
| Started by the plugin (`proxy.pid`, same pid as the status file) | Stopped and started from the installed package | Warning |
| Docker compose container whose checkout's `package.json` matches the installed version | `docker restart <container>` | Warning |
| Compose container on another checkout version, a manual `smarter-compact proxy`, or no status file | Warning | Warning |

Claude Code and Codex show the warning as a session-start system message; `enable` prints it to stderr. A warning names what to restart or update.

## Limits

- **One LiteLLM worker.** Session locking is in-process; do not run multiple workers.
- **Fail open.** Rewrite errors log one line and forward the request untrimmed; `fail_open` events in the session DB.
- **Codex pass-through** requires the dev LiteLLM build noted above.
- **OpenAI-format clients** need explicit routes in `proxy/config.yaml` unless using Anthropic models (see comments in that file).

Overview and agent matrix: [smart compaction](context-management.md).
