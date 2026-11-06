# Configuration

Every setting is an environment variable. The **Read by** column names the component that reads it:

- **proxy**: the LiteLLM context proxy (`proxy/smarter_compact/settings.py`), which trims for Claude Code, Codex and OpenCode 1.
- **OpenCode 2**: the in-process trim (`trimSettings` in `plugin/lib/context-trim.mjs`), configured by env or [plugin options](#opencode-plugin-options).
- **MCP**: the `smart_compact_*` tool server (`plugin/lib/mcp/`).
- **CLI**: `smarter-compact`, the agent hooks, and the proxy auto-start (`plugin/lib/brand.mjs`, `plugin/lib/proxy-daemon.mjs`).

A setting the proxy reads and OpenCode 2 does not has no effect on OpenCode 2 sessions.

## Precedence

1. Process environment.
2. `~/.config/smarter-compact/proxy.env`, for `CONTEXT_PROXY_PORT` and the [keys listed there](#config-files) only.
3. `~/.config/smarter-compact/config.toml`, for trim thresholds only.
4. Built-in default.

OpenCode 2 plugin options override the environment for the in-process trim. Under Docker compose, the host shell or `./.env` sets the variables `compose.yaml` lists; `proxy.env` supplies the rest.

## Trim

| Variable | Default | Read by | Effect |
| --- | --- | --- | --- |
| `CONTEXT_PROXY_ENABLED` | `1` | proxy, OpenCode 2 | `0` forwards requests unchanged: no trim, compression, store or standing instruction |
| `CONTEXT_TRIM_AT_TOKENS` | unset | proxy, OpenCode 2 | Set: one threshold in estimated prompt tokens for every model. Unset: the model id picks a threshold from `CONTEXT_TRIM_AT_OVERRIDES` merged over built-in `haiku` 95000, else `default` 125000 |
| `CONTEXT_TRIM_AT_OVERRIDES` | unset | proxy, OpenCode 2 | JSON `{"<family>": tokens, "default": tokens}`; a family matches a substring of the lowercased model id. The CLI fills it from `config.toml` `[context_proxy.trim_at]`; set it by hand only where `config.toml` is not read (Docker) |
| `CONTEXT_TRIM_TO_FRACTION` | `0.53` | proxy, OpenCode 2 | Older in-turn tool steps stay while the request is under this fraction of the threshold; `0 < x < 1`, else the default |
| `CONTEXT_TRIM_MIN_GAIN_TOKENS` | `0` | proxy, OpenCode 2 | Skips a new trim that would free fewer estimated tokens than this; a forced trim (`/compact`) ignores it; `0` off |
| `CONTEXT_CLEAR_KEEP_RESULTS` | `0` | proxy, OpenCode 2 | `N` > 0: a trim first masks every tool result but the N newest and keeps all turns; turns drop only when that is not enough. Experimental ([research](context-research.md#clearing-versus-turn-deletion-2026-11-05)) |
| `CONTEXT_MASK_OBSERVATIONS` | `1` | proxy | Keeps tool calls in the kept region before the latest run and replaces their longer results with a placeholder; wins over `CONTEXT_STRIP_PROSE`; `0` off ([research](context-research.md#prose-strip-versus-observation-masking-2026-11-05)) |
| `CONTEXT_STRIP_PROSE` | `0` | proxy | `1` strips tool calls and results from kept turns, keeping their text; under review ([research](context-research.md#terminal-bench-21-trim-shape-arms-2026-11-05)) |
| `CONTEXT_PROGRESS_NOTE` | `1` | proxy | Appends files edited and the last build/test commands to the trim pointer; `0` off ([research](context-research.md#terminal-bench-21-trim-shape-arms-2026-11-05)) |
| `CONTEXT_CHARS_PER_TOKEN` | `2.5` | proxy, OpenCode 2 | Token estimate ratio until the session reports usage ([token estimate](context-proxy.md#token-estimate)); must be > 0 |
| `CONTEXT_COMPRESS_OVER_TOKENS` | `4000` | proxy, OpenCode 2 | Tool results above this become a header plus `smart_compact_get(id=...)`; `0` off |
| `CONTEXT_STANDING_INSTRUCTION` | `1` | proxy, OpenCode 2 | `0` omits the system note telling the agent to search before guessing |

## Recall on trim

Recall runs in the proxy only. OpenCode 2 reads these values but makes no recall call.

| Variable | Default | Read by | Effect |
| --- | --- | --- | --- |
| `CONTEXT_COMPACTION_RECALL` | `0` via the CLI, hooks and OpenCode 2; `1` in the proxy core and Docker compose | proxy | `1` adds a recall block from the memory bank the session registration names ([format](context-store.md#proxy-inserted-text)) |
| `CONTEXT_COMPACTION_MAX_CHARS` | `12000` | proxy | Recall block size cap, in characters |
| `CONTEXT_RECALL_TIMEOUT_S` | `5.0` | proxy | Recall request timeout, in seconds |
| `HINDSIGHT_URL` | `http://127.0.0.1:8888` | proxy | Memory service the recall queries |

## Server-side tool-result clearing

Anthropic route (`/v1/messages`) to Claude models only. Details: [context proxy](context-proxy.md#server-side-tool-result-clearing).

| Variable | Default | Read by | Effect |
| --- | --- | --- | --- |
| `CONTEXT_SERVER_CLEAR` | `0` | proxy | `1` appends a `clear_tool_uses_20250919` context edit to each request |
| `CONTEXT_SERVER_CLEAR_TRIGGER_TOKENS` | `100000` | proxy | The edit's `trigger`: clearing starts above this many input tokens; must be > 0 |
| `CONTEXT_SERVER_CLEAR_KEEP_TOOL_USES` | `3` | proxy | The edit's `keep`: newest tool use/result pairs never cleared |
| `CONTEXT_SERVER_CLEAR_AT_LEAST_TOKENS` | `5000` | proxy | The edit's `clear_at_least`, the cache-break gate; `0` omits it |

## Proxy process

| Variable | Default | Read by | Effect |
| --- | --- | --- | --- |
| `CONTEXT_PROXY_PORT` | `4101` (Docker compose: `4100`) | CLI, proxy | LiteLLM listen port on `127.0.0.1`; env wins over `proxy.env`; the proxy names its status file after it ([proxy port](context-proxy.md#proxy-port)) |
| `SMARTER_COMPACT_AUTO_PROXY` | `1` | CLI | `0`, `false` or `no` stops hooks and plugins from starting the proxy |
| `LITELLM_MASTER_KEY` | generated | LiteLLM | Key agents send in `x-litellm-api-key`. The CLI generates it into `proxy.env` (mode 600) and passes it to the proxy it starts |
| `SMARTER_COMPACT_ROOT` | package location | CLI | Package root holding `proxy/smarter_compact_hook.py`, when the proxy files moved |
| `CONTEXT_PROXY_ENV_FILE` | `~/.config/smarter-compact/proxy.env` | Docker compose | `env_file` path for the container |
| `HOST_UID`, `HOST_GID` | required | Docker compose | User the container runs as, so the store stays host-owned |

## Store and search

| Variable | Default | Read by | Effect |
| --- | --- | --- | --- |
| `SMARTER_COMPACT_STATE_DIR` | `~/.local/share/smarter-compact` | all | Store parent; files live under `context/` ([location](context-store.md#location)). `OPENMEM_STATE_DIR` is the pre-rename name, still read |
| `CONTEXT_STORE_TTL_DAYS` | `30` | proxy, MCP | Sessions idle longer are deleted at start, at most once a day; `0` keeps everything ([retention](context-store.md#retention)) |
| `CONTEXT_EMBEDDING_MODEL` | empty (vectors off) | proxy, OpenCode 2, MCP | Embedding model for hybrid search ([vectors](context-store.md#vectors)) |
| `CONTEXT_EMBEDDING_URL` | `http://127.0.0.1:11434/v1` (Docker compose: `http://host.docker.internal:11434/v1`) | proxy, OpenCode 2, MCP | OpenAI-compatible base URL; requests go to `POST <url>/embeddings` |
| `CONTEXT_EMBEDDING_API_KEY` | empty | proxy, OpenCode 2, MCP | Bearer token, when the endpoint needs one |
| `CONTEXT_EMBEDDING_TIMEOUT_S` | `30` | proxy, OpenCode 2 | Seconds per row: a call of n rows times out after n × this |
| `CONTEXT_EMBEDDING_MAX_CHARS` | `4000` | proxy, OpenCode 2 | Characters of each row's head that are embedded |

## Install

| Variable | Default | Read by | Effect |
| --- | --- | --- | --- |
| `SMARTER_COMPACT_INSTALL_CURSOR` | unset | CLI | `1` lets `smarter-compact enable -g` install the Cursor MCP and hooks when `~/.cursor` exists |

## Config files

| File | Written by | Read by | Content |
| --- | --- | --- | --- |
| `~/.config/smarter-compact/proxy.env` | `smarter-compact enable` | CLI, Docker compose (`env_file`) | `KEY=value` lines. The CLI reads `LITELLM_MASTER_KEY`, `CONTEXT_PROXY_PORT`, and, when unset in the environment, `CONTEXT_TRIM_AT_TOKENS`, `CONTEXT_TRIM_TO_FRACTION`, `CONTEXT_COMPRESS_OVER_TOKENS`, `CONTEXT_COMPACTION_MAX_CHARS`, `CONTEXT_CHARS_PER_TOKEN`, `CONTEXT_STANDING_INSTRUCTION`, `CONTEXT_PROXY_ENABLED`; it ignores other keys |
| `~/.config/smarter-compact/config.toml` | `smarter-compact context set` | CLI | `[context_proxy] trim_at_tokens` (one threshold for every model) and `[context_proxy.trim_at]` (`<family> = tokens`, `default = tokens`). Applied only when `CONTEXT_TRIM_AT_TOKENS` is unset |

Edit `config.toml` with the CLI, then restart the proxy or start a new session:

```sh
smarter-compact context list
smarter-compact context set haiku=95000    # per model family
smarter-compact context set trim=120000    # every model
smarter-compact context unset haiku
```

## OpenCode plugin options

OpenCode 2 maps each option to its variable for the in-process trim (`src/options.mjs`); a boolean option becomes `1` or `0`.

| Option | Variable | Effect in OpenCode 2 |
| --- | --- | --- |
| `enabled` | `CONTEXT_PROXY_ENABLED` | yes |
| `standingInstruction` | `CONTEXT_STANDING_INSTRUCTION` | yes |
| `trimAtTokens` | `CONTEXT_TRIM_AT_TOKENS` | yes |
| `trimToFraction` | `CONTEXT_TRIM_TO_FRACTION` | yes |
| `compressOverTokens` | `CONTEXT_COMPRESS_OVER_TOKENS` | yes |
| `charsPerToken` | `CONTEXT_CHARS_PER_TOKEN` | yes |
| `stateDir` | `SMARTER_COMPACT_STATE_DIR` | trim store only; session registration and `/compact` flags use the environment |
| `compactionRecall` | `CONTEXT_COMPACTION_RECALL` | none: OpenCode 2 has no recall |
| `compactionMaxChars` | `CONTEXT_COMPACTION_MAX_CHARS` | none |
| `recallTimeoutS` | `CONTEXT_RECALL_TIMEOUT_S` | none |
| `proxyPort` | `CONTEXT_PROXY_PORT` | none: proxy auto-start reads the environment and `proxy.env` |

OpenCode 1 trims in the proxy, which reads its own environment: set the variables or `proxy.env`, not plugin options.
