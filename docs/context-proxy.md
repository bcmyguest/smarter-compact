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

The OpenCode 2 `proxyPort` option does not move the auto-started proxy; set `CONTEXT_PROXY_PORT` instead.

Auto-start is off when `SMARTER_COMPACT_AUTO_PROXY=0`. Logs: `~/.config/smarter-compact/proxy.log`, rotated to `proxy.log.1` at 5 MB on each start.

## Configuration

Every proxy setting, its default and which settings OpenCode 2 also reads: [configuration reference](configuration.md#trim).

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

1. Pre-call hook counts request characters and keys them by `litellm_call_id`. Tools with `defer_loading: true` are left out: they cost no prompt tokens until loaded, and Claude Code sends hundreds of KB of them.
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
