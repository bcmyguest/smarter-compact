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

Auto-start is off when `SMARTER_COMPACT_AUTO_PROXY=0`. Logs: `~/.config/smarter-compact/proxy.log`.

## Configuration

| Variable | Default | Effect |
| --- | --- | --- |
| `CONTEXT_PROXY_ENABLED` | `1` | `0` forwards requests unchanged |
| `CONTEXT_TRIM_AT_TOKENS` | unset → per-model | **Set** (env, `proxy.env`, OpenCode `trimAtTokens`, or `[context_proxy] trim_at_tokens` / `context set trim=`) → one threshold for every model. **Unset** → `[context_proxy.trim_at]` in `~/.config/smarter-compact/config.toml` merged with built-in `haiku=95000`, fallback `default=125000`. CLI: `smarter-compact context set haiku=88000`. |
| `CONTEXT_TRIM_TO_FRACTION` | `0.3` | In-turn tool retention fraction |
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

## Limits

- **One LiteLLM worker.** Session locking is in-process; do not run multiple workers.
- **Fail open.** Rewrite errors log one line and forward the request untrimmed; `fail_open` events in the session DB.
- **Codex pass-through** requires the dev LiteLLM build noted above.
- **OpenAI-format clients** need explicit routes in `proxy/config.yaml` unless using Anthropic models (see comments in that file).

Overview and agent matrix: [smart compaction](context-management.md).
