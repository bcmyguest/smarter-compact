# Context proxy

For Claude Code, OpenCode 1, and Codex, **Smarter Compact** runs a local **LiteLLM** proxy (a Docker container by default, or a native process; see [Runtime](#runtime)) that rewrites each request before forwarding to the real model API:

1. **Tool-result compression.** Results over `CONTEXT_COMPRESS_OVER_TOKENS` become a short header plus `smart_compact_get(id=...)`; full text in `blobs/<id>.txt`.
2. **Store.** Messages go into `sessions/<sid>.db` ([session database](context-store.md#session-database)).
3. **Trim.** At `CONTEXT_TRIM_AT_TOKENS` or when `sessions/<sid>.force` exists, older turns drop from the request; dropped rows get `in_context = 0`; a pointer replaces them at the start of the kept turn.
4. **Standing instruction.** Every request gets a constant system note to use `smart_compact_search` before guessing about trimmed content.

The cut is sticky for prompt-cache stability. **OpenCode 2** applies the same rules in-process (`plugin/lib/context-trim.mjs`); it does not send traffic through this proxy.

Code: `proxy/smarter_compact/`, `proxy/smarter_compact_hook.py`, `proxy/config.yaml`. Tests: `python3 -m unittest discover -s proxy/tests`.

The proxy runs **LiteLLM 1.109.0rc1** (pinned; the first release candidate with PR #41522, needed for Codex ChatGPT pass-through).

## Runtime

| | Docker (default when `docker info` answers) | Native (no Docker, or `SMARTER_COMPACT_PROXY_RUNTIME=native`) |
| --- | --- | --- |
| What runs | Container `smarter-compact-proxy`, image `ghcr.io/berriai/litellm:v1.109.0-rc.1`, the installed package's `proxy/` mounted read-only | `litellm` on `PATH`, started detached |
| Installed by `enable` | `docker pull` of the image | `uv tool install --force 'litellm[proxy]==1.109.0rc1'` when `litellm --version` is missing or another version |
| After a reboot | Docker restarts it (`--restart always`), before any agent starts | Gone until the next session start or `enable` starts it again |
| Logs | `docker logs smarter-compact-proxy` (failed `docker` commands also go to `proxy.log`) | `~/.config/smarter-compact/proxy.log` |

`SMARTER_COMPACT_PROXY_RUNTIME=docker` forces Docker. With neither Docker nor uv, `enable` stops before it changes any agent config. Session hooks only start the proxy; they never pull or install.

The container listens on `127.0.0.1:<port>` only, runs as your user (except on Windows) and mounts the session store dir at `/state`. It gets `LITELLM_MASTER_KEY`, the `CONTEXT_*` settings and `ANTHROPIC_BASE_URL` from the same sources as the native proxy, by name (`docker run -e KEY`), so no secret is on the command line; a loopback `ANTHROPIC_BASE_URL` or `CONTEXT_EMBEDDING_URL` (default `http://127.0.0.1:11434/v1`) is rewritten to `host.docker.internal`. Settings are fixed when the container is created: after editing `proxy.env`, run `smarter-compact proxy restart`, which recreates it.

`--restart always` brings the proxy back only once the Docker daemon itself runs: on Linux `sudo systemctl enable docker`; with Docker Desktop (macOS, Windows), turn on *Start Docker Desktop when you sign in*. A session that starts while the container is still booting (a cold start takes about 15 s) waits for it instead of recreating it.

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

The OpenCode 2 `proxyPort` option sets the port that plugin probes; it starts a proxy there only when an agent config already points at that port. Other agents read `CONTEXT_PROXY_PORT`.

Auto-start is off when `SMARTER_COMPACT_AUTO_PROXY=0`. Logs: `docker logs smarter-compact-proxy`, or for the native runtime `~/.config/smarter-compact/proxy.log`, rotated to `proxy.log.1` at 5 MB on each start.

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

## Prompt-cache timing

Both settings are off by default and need the request clock: with either on, the trim state records per thread root the last request's arrival (`last_request`: epoch seconds `at`, idle gaps over 5 minutes `long`). Anthropic's [prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) docs: "Cache write tokens with a 5-minute TTL are 1.25 times the base input tokens price", "with a 1-hour TTL are 2 times", reads 0.1; "The lifetime is measured from the start of the request that writes or reads the cache entry"; "a 1-hour cache entry must appear before any 5-minute cache entries".

**Cold-cache trim** (`CONTEXT_COLD_TRIM=1`, proxy and OpenCode 2). When the gap since the thread's previous request exceeds the cache lifetime, the request rewrites its whole prefix anyway, so it runs the normal trim to the target although the prompt is under the threshold: only when the estimate is above the target and the trim frees at least `CONTEXT_COLD_TRIM_MIN_GAIN_TOKENS` (default 5000; `CONTEXT_TRIM_MIN_GAIN_TOKENS` when larger). The lifetime: on the Anthropic route, 3600 s when a message `cache_control` (or the top-level one) has `ttl` `"1h"`, 300 s for other markers (also when the TTL split below sends `"5m"`); otherwise `CONTEXT_COLD_TRIM_SECONDS` (default 3600; OpenAI's `in_memory` retention lasts "up to one hour", its `24h` retention longer). The OpenCode 2 plugin never sees `cache_control`, so it always uses `CONTEXT_COLD_TRIM_SECONDS`. The `trim` event has `location` `cold`.

**Split cache TTL** (`CONTEXT_CACHE_TTL_SPLIT=N`, proxy Anthropic route only, including Bedrock and Vertex models behind it). When tools or system carry a `"1h"` marker, every message marker's `ttl` `"1h"` goes up as `"5m"`, so each turn's new content is written at 1.25× instead of 2×, while tools and system stay cached for an hour. No breakpoint is added or moved. After a gap over 5 minutes the conversation entry has expired and the request writes everything after the system prompt again at 1.25×. From the thread's Nth such gap on, the proxy sends the client's markers unchanged, so the switch happens once, on a request whose tail is cold anyway. Every request before it gets the same decision. The OpenAI and Chat routes and the plugin ignore the setting. `usage` events record `cache_creation_1h_tokens` and `cache_creation_5m_tokens` from Anthropic's `usage.cache_creation`.

## Token estimate

The trim uses a **calibrated estimate** from provider usage:

1. Pre-call hook counts request characters and keys them by `litellm_call_id`. Tools with `defer_loading: true` are left out: they cost no prompt tokens until loaded, and Claude Code sends hundreds of KB of them. The encrypted payloads of thinking blocks (`thinking.signature`, `redacted_thinking.data`) count at face value with `CONTEXT_STRIP_OLD_REASONING` on (the default): on Haiku 5.5 a thinking block kept in a tool loop bills one token per 3.1 to 3.3 signature characters ([measured](context-store.md#signed-reasoning)), close to text. With it `0` they are left out, as before. The thinking text itself counts.
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

## Debug request shapes

Set `CONTEXT_DEBUG_SHAPES=1` (environment or `~/.config/smarter-compact/proxy.env`) and restart the proxy. Each request then writes one line to `~/.config/smarter-compact/proxy.log`, as received from the client: item count, pinned items, system prompt size in characters, tool count, the thread root fingerprint and the roles of the last 12 items. It logs counts only, never message content. Use it to tell whether a short request left the client short or was cut in the proxy.

## Run it with docker compose (from this repo)

For working on the proxy itself. `compose.yaml` at the repo root runs the same image (also `restart: always`) as the compose service `context-proxy`: it starts the pinned LiteLLM image with `./proxy` mounted at `/app/proxy`, so **the requests are rewritten by the code in this checkout** — `docker compose restart context-proxy` is the deploy step.

```sh
HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose up -d context-proxy   # or put both in ./.env
docker compose logs -f context-proxy
```

It needs `LITELLM_MASTER_KEY` in `~/.config/smarter-compact/proxy.env` (mode 600; `CONTEXT_PROXY_ENV_FILE` overrides the path), listens on `127.0.0.1:${CONTEXT_PROXY_PORT:-4100}`, and writes the session store to `~/.local/share/smarter-compact/context` — falling back to the deprecated openmem service's `~/.local/share/openmem/context` when only that exists, so switching which repo serves it changes no agent config and orphans no session a running agent can still recall. Set `CONTEXT_PROXY_PORT=4101` to move onto the CLI default instead.

## Updates

The proxy writes `proxy-<port>.json` to the context dir at start, and at most every 5 s while it serves requests: its `package.json` version, pid, start time and last request. At each session start (and on `smarter-compact enable`), the plugin compares that version with the installed package:

| Proxy on the port | Idle 60 s or more | Busy |
| --- | --- | --- |
| The plugin's `smarter-compact-proxy` container | Recreated from the installed package | Warning |
| Started by the plugin as a native process (`proxy.pid`, same pid as the status file) | Stopped and started from the installed package | Warning |
| Docker compose container whose checkout's `package.json` matches the installed version | `docker restart <container>` | Warning |
| Compose container on another checkout version, a manual `smarter-compact proxy`, or no status file | Warning | Warning |

Claude Code and Codex show the warning as a session-start system message; `enable` prints it to stderr. A warning names what to restart or update.

`smarter-compact proxy restart` restarts at once. It recreates the `smarter-compact-proxy` container, or stops the litellm in `proxy.pid` (checked to be litellm; on Windows with its child processes), or restarts another Docker container publishing the port; it refuses any other listener. Then it starts the installed version in the default runtime, so a native proxy moves to Docker once Docker runs.

## Limits

- **One LiteLLM worker.** Session locking is in-process; do not run multiple workers.
- **Fail open.** Rewrite errors log one line and forward the request untrimmed; `fail_open` events in the session DB.
- **Codex pass-through** requires the dev LiteLLM build noted above.
- **No Claude Code Remote Control.** Claude Code blocks Remote Control while `ANTHROPIC_BASE_URL` points at the proxy; run `smarter-compact disable claude` first ([quick start](quick-start.md#claude-code)).
- **OpenAI-format clients** need explicit routes in `proxy/config.yaml` unless using Anthropic models (see comments in that file).

Overview: [how it works](context-management.md). Agent matrix: [agents](agents.md).
