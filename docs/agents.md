# Agents

Smart compaction only works when something **sees and rewrites the model request** before it is sent. Each agent gets that in one of two ways, or not at all.

| Tier | Agents | How |
| --- | --- | --- |
| **Full trim (proxy)** | Claude Code, Codex, OpenCode 1 | `smarter-compact enable` points the CLI at the loopback LiteLLM proxy; every turn is stored and older turns drop off the wire at the token threshold ([context proxy](context-proxy.md)). |
| **Full trim (in-process)** | OpenCode 2, Pi | JS port of the trim core in the agent's **`context` hook**, with prose strip, observation masking and the progress note. No proxy URL. |
| **Search only** | Cursor Agent, Copilot CLI | MCP tools and session hooks; the model context is not trimmed. |

| Agent | On-wire trim | How | Setup |
| --- | --- | --- | --- |
| Claude Code | yes | Loopback proxy via `smarter-compact enable claude` | [Quick start](quick-start.md#claude-code) |
| Codex | yes | `model_provider = "smarter-compact"` + LiteLLM Codex pass-through | [Quick start](quick-start.md#codex) |
| OpenCode 2 | yes | Native `context` hook in this package | [OpenCode](opencode.md#opencode-2) |
| OpenCode 1 | yes | Proxy via `smarter-compact enable opencode` | [OpenCode](opencode.md#opencode-1) |
| Pi | yes | Pi extension → `context` hook ([pi.dev docs](https://pi.dev/docs/latest/extensions#context)) | [Quick start](quick-start.md#pi) |
| Cursor Agent | no | MCP + hooks only | `smarter-compact install cursor`, reload Cursor, enable the local plugin |
| Copilot CLI | no | MCP + hooks only; no proxy base URL | `smarter-compact plugins add copilot` |

**Pi / OpenCode 2** use the same `trimContext` path as the proxy. **Cursor / Copilot** register sessions and expose the `smart_compact_*` search tools but do not shrink the model context; `enable -g` does not install Cursor unless you set `SMARTER_COMPACT_INSTALL_CURSOR=1`.

Claude Code keeps its own auto-compact as a safety net if the proxy is down. `/context` in Claude Code counts the full local transcript, not the trimmed request the proxy sends, so it can read higher than API usage.
