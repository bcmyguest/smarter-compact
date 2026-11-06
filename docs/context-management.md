# How it works

Context management (smart compaction) keeps long coding-agent sessions under a token limit without an LLM summary. Every message goes into a local SQLite session store; older turns leave the context and the turn they precede carries a pointer to them; the agent searches the store when it needs a detail back.

<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="media/smart-compaction-dark.gif" />
<img src="media/smart-compaction.gif" alt="Animation: the model context fills past trim_at_tokens; the system prompt, tools and task turn stay, older turns and the oldest tool step of the in-flight turn slide into the saved transcript, the cut turn gets the continuing-session pointer prepended, the last three tool steps stay, the context drops to the target, and one turn-2 excerpt returns through smart_compact_search" width="720" />
</picture>
<br />
<sub>Older turns move to the session store, the cut turn carries a pointer, and <code>smart_compact_search</code> pulls a detail back.</sub>
</p>

<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="media/context-management-1-dark.svg" />
<img src="media/context-management-1.svg" alt="Sequence diagram: Agent, proxy or plugin, context store, model API" width="640" />
</picture>
</p>

Each request goes through four steps ([context proxy](context-proxy.md) has implementation detail):

| Step | When | Effect |
| --- | --- | --- |
| Tool-result compression | A tool result exceeds `compress_over_tokens` | First 40 and last 20 lines stay, behind a `smart_compact_get(id=...)` header; the full text goes to `blobs/<id>.txt` |
| Store | Every request | Each message goes into `sessions/<sid>.db` once, keyed by a chained fingerprint, with an `in_context` flag ([context store](context-store.md#session-database)) |
| Trim | The request reaches `trim_at_tokens`, or the user typed `/smarter-compact:compact` | Turns before the last real user turn, and the oldest tool exchanges of a long in-flight turn, leave the context and their rows get `in_context = 0`; a pointer naming `smart_compact_search` / `smart_compact_sql` replaces them. System prompt, tools, the first user turn, the reply before the last user turn, that turn and at least its last 3 tool exchanges stay verbatim, except that signed thinking leaves every kept assistant message but the latest ([why](context-store.md#trim)) |
| Scratchpad (`CONTEXT_SCRATCHPAD=1`, default off) | The prompt passes `CONTEXT_SCRATCHPAD_NUDGE_AT` of the threshold, then the next trim | One nudge, appended to the newest user or tool message, asks the agent to save task state with `smart_compact_scratchpad`; the next trim note carries the pad (≤4,000 chars) in a `<compact-scratchpad>` block and archives the file |
| Standing instruction | Every request | A constant system-prompt note tells the agent to call `smart_compact_search` before answering from content it cannot see |

The cut is **sticky**: later requests with the same history get the same rewrite byte for byte, so the prompt cache keeps hitting. A trim invalidates the cache once.

## Benchmarks

ContextBench quality, time, cost and prompt-cache results: [Benchmarks](benchmarks.md).

## When to use it

| Turn it on when | Leave it off when |
| --- | --- |
| Sessions grow past `trim_at_tokens` (default 125k estimated tokens, 95k for Haiku) | Sessions stay under the limit |
| You want trimmed turns searchable, not summarized away | You cannot run `litellm` on the host (Claude, OpenCode 1, Codex point at the proxy) |
| You use Claude Code, Codex, OpenCode 2, or Pi | You only need MCP search hooks on Cursor or Copilot (no automatic trim) |

## Turn it on

```sh
smarter-compact enable claude    # or codex, opencode (OpenCode 1)
```

The plugin **starts the LiteLLM proxy in the background** when an agent is pointed at it. OpenCode 2 trims in-process: `opencode plugin smarter-compact` — no proxy URL.

Force a trim in session: `/smarter-compact:compact` (or agent equivalent).

Status: `smarter-compact status` or MCP `context_status`.

## Agent support

Which agents trim on the wire, which trim in-process, and which get search tools only: [Agents](agents.md).

## MCP tools

| Tool | Purpose |
| --- | --- |
| `smart_compact_search` | FTS / hybrid search over trimmed session rows |
| `smart_compact_sql` | Read-only SQL on one session database |
| `smart_compact_get` | Text of a compressed tool result by blob id, `BLOB_PAGE_CHARS` (12,000) per call; `offset`/`next_offset` page, `max_chars` (≤100,000) asks for more at once |
| `context_status` | Trims, fail-opens, writer versions, usage summary |
| `smart_compact_scratchpad` | Only with `CONTEXT_SCRATCHPAD=1`: `view`, `create`, `str_replace`, `insert` on the session's pad, capped at 4,000 characters |

Wire format and SQL rules: [context store](context-store.md).

## Configuration

Every setting, its default and the component that reads it: [configuration reference](configuration.md#trim).

## More

- [Context proxy](context-proxy.md) — LiteLLM rewrite pipeline
- [Context store](context-store.md) — schema, trim rules, search tools
- [OpenCode](opencode.md) — plugin install
- [Configuration](configuration.md) — every setting and default
- [Context research](context-research.md) — related work, run notes, and benchmark method
