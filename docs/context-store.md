# Context store contract

This file is the context-store contract for **Smarter Compact**. The Python package is `proxy/smarter_compact`; the JavaScript port is `plugin/lib/`. `SMARTER_COMPACT_STATE_DIR` defaults to `~/.local/share/smarter-compact`; files live under `$SMARTER_COMPACT_STATE_DIR/context`. Wire text (`[smarter-compact: …]`, `smart_compact_*`) matches OpenMem for compatibility.

See [how it works](context-management.md) and [context proxy & port](context-proxy.md).

The context proxy, the OpenCode plugin (which trims in-process), and the `smart_compact_*` MCP tools exchange state through local files. Below, "proxy" means whichever of the two trims a session. This page is the contract between them; change it before changing any side.

## Location

Root: `$SMARTER_COMPACT_STATE_DIR/context`, default `~/.local/share/smarter-compact/context` (`~/.local/share/openmem/context` is still used when it exists and the new one does not). Surrounding ASCII whitespace (space, `\t`, `\n`, `\v`, `\f`, `\r`) is trimmed from `SMARTER_COMPACT_STATE_DIR`; an empty value after trimming means the default. Directories are mode 700, files mode 600. The context-proxy container bind-mounts this directory read-write and runs as the host UID/GID.

| Path | Writer | Reader | Content |
| --- | --- | --- | --- |
| `sessions/<sid>.json` | SessionStart hook, OpenCode plugins | proxy, MCP | Registration: `{"session_id", "agent", "cwd", "registered_at"}` (ISO 8601 UTC) |
| `sessions/<sid>.force` | prompt hook or OpenCode plugin on `/smarter-compact` | proxy | Empty flag. The proxy trims on the next request for that session, then deletes the flag. |
| `sessions/<sid>.trim.json` | proxy | proxy | Proxy-private trim state (cut point, fingerprints, reported-usage samples for the token estimate, see [context-proxy.md](context-proxy.md#token-estimate)). Other components must not depend on its shape. |
| `sessions/<sid>.db` (+ `-wal`, `-shm`) | proxy | MCP `smart_compact_search`, `smart_compact_sql`, shell `sqlite3` | [Session database](#session-database): every message of the session, in or out of context. The only store of record for message content. |
| `blobs/<id>.txt` | proxy, OpenCode 2 plugin | MCP `smart_compact_get` | Full text of a compressed or [cleared](clearing.md) tool result, served back in 12,000-char pages. `id` = first 16 hex chars of SHA-256 of the UTF-8 content. |
| `scratchpad/<sid>.md` | MCP `smart_compact_scratchpad` | proxy | The agent's pad (unless `CONTEXT_SCRATCHPAD=0`). Every trim copies it, capped at 2,500 characters, into the start of the note under a possibly-stale label, keeps the file, and saves a snapshot `<sid>.<n>.md` (n the next free integer). |
| `tool-calls.jsonl` | MCP `smart_compact_*` tools | `smarter-compact context stats` | One JSON line per search / sql / get call (`at`, `tool`, hit/row/char counts). Best-effort telemetry; not part of the session rewrite contract. Moves to `tool-calls.jsonl.1` at 5 MB, replacing the previous generation; stats read both. |
| `.ttl-stamp` | proxy, MCP server | proxy, MCP server | Empty; its mtime records the last [retention](#retention) sweep |

Earlier releases wrote `sessions/<sid>.log.jsonl` handoff logs. Nothing writes or reads them now and they are not migrated; the retention sweep deletes them with their session.

## Session id

`<sid>` is the agent's session id after this rule, applied identically by the hooks and plugins (`sanitizeSessionId` in `cli/coding-agent/context-store.mjs`), the proxy (`sanitize_sid` in `proxy/smarter_compact/store.py`) and the MCP tools:

1. Trim surrounding ASCII whitespace (space, `\t`, `\n`, `\v`, `\f`, `\r`); other whitespace is not trimmed.
2. Replace every Unicode code point outside `[A-Za-z0-9._-]` with one `_` (`😀` becomes `_`, not `__`).
3. Reject an empty result: hooks write no file, the proxy tries the next source below, the MCP tools return an error.

Dots are kept, so `..` survives (`ses/../x` becomes `ses_.._x`). That is safe because step 2 removes every path separator and each file name appends a suffix (`.json`, `.db`, ...), so no sid names a path outside `sessions/`. Registration and trimming accept such ids. The MCP tools additionally reject an explicit `session_id` argument that contains `/` or `\`, or that is `.` or `..`; pointers always give the agent the sanitized id, which never has a separator.

The proxy takes the first non-empty id, in order, from:

1. the `x-smarter-compact-session-id` request header (sent by Smarter Compact's OpenCode plugin through its `chat.headers` hook);
2. the `x-claude-code-session-id` header (Claude Code; equals the hook `session_id`);
3. `metadata.user_id` JSON `session_id`, then the legacy `user_id` form ending in `_session_<id>` (8 or more of `[0-9A-Za-z-]`); the request's own `metadata` first, then `proxy_server_request.body.metadata`;
4. `prompt_cache_key` (Codex sends its conversation id there), from the request then from the original body;
5. the `session_id`, `x-session-id` and `conversation_id` headers, in that order.

Sources 3 (legacy form), 4 and 5 cover clients Smarter Compact does not wire yet; only Claude Code and OpenCode are wired. When no source yields an id, the proxy uses `fp-<first 16 hex of SHA-256(system prompt + first user message)>`; such sessions have no registration, but trimming still works.

## Trim

A trim keeps, in their original order:

- pinned messages before the cut (Anthropic `system` messages, which Claude Code uses for its environment, model id and deferred-tool list; Chat and Responses `system` / `developer` messages; OpenCode 2 `system` messages). Pinned messages are not written to the store;
- the **first real user turn**, verbatim: Anthropic, the first `user` message that is not only `tool_result` blocks; Chat, the first `user` message; Responses, the first user `message` item; OpenCode 2, the first `user` message that is not only `tool-result` parts. It usually states the task, so every trim keeps it ahead of the pointer;
- the **reply before the cut**: the agent message just before the cut turn, when it is a final reply (an assistant message with no tool calls; Responses, a `message` item), so the cut turn reads as an answer to it;
- the cut turn, with the pointer block prepended;
- everything after the cut turn except the **in-turn gap**.

A trim runs when the `.force` flag exists or the request's estimated prompt tokens reach the threshold. The estimate is calibrated from the provider's reported usage, with `CONTEXT_CHARS_PER_TOKEN` (default 2.5) as the fallback ratio ([context-proxy.md](context-proxy.md#token-estimate)); it never moves an existing cut.

The cut is the last real user turn after the previous cut whose kept part shares no tool call/result pair with the dropped part. When no such turn drops anything (for example, while the first user turn is still in flight), the cut is the existing cut, else the last real user turn.

The in-turn gap drops the oldest tool exchanges of the cut turn's own agent loop, so one long prompt can still be trimmed. A step is one tool exchange: Anthropic, Chat and OpenCode 2, an assistant message and the results after it; Responses, a `reasoning` item, or a call or reply not preceded by reasoning or a call, and what follows it. The tail after the gap keeps the newest 3 steps, then older steps while the request stays under `CONTEXT_TRIM_TO_TOKENS` (default `haiku` 50000, else 66000; capped at the trim threshold); the step right after the cut turn is never kept in the tail, and a step is never split. A turn with 3 steps or fewer has no gap. Later trims of the same cut only move the gap forward; a new cut starts a new gap. The cut and gap are stored with fingerprints, so later requests render the same bytes until the next trim.

### Signed reasoning

A trimmed view drops **signed reasoning** from every kept assistant message before the latest assistant message at trim time (with `CONTEXT_STRIP_OLD_REASONING=0`, before the latest assistant run: the trailing run of assistant items ending at the last one); that message goes out unchanged. A Responses `reasoning` item and the calls after it count as one message. The trim records where it starts as `strip` / `strip_fp` in the thread state, next to `cut` and `gap`. With `CONTEXT_CLEAR_KEEP_RESULTS` on it also records `clear` / `clear_fp`, the first message whose tool results stay whole; every kept result before it renders as its mask placeholder (`[smarter-compact: tool output cleared to save context; smart_compact_get(id="<id>") returns it]`, the id of a blob holding its text) until a later trim moves the bound forward. A thinking signature covers the whole prefix before it, and the trim edits that prefix (dropped messages, the pointer), so a kept older block would no longer verify; Anthropic's API rejects it, and LiteLLM hides the 400 by retrying with all thinking removed. (Three probes on 2026-11-08, `claude-haiku-5-5` with adaptive thinking, a Claude Code tool loop of four assistant steps: thinking on the last step only, the same with a middle step dropped, and thinking kept on the first and last but not the third; all three returned `200`.) What counts as signed, per format:

| Format | Removed from older kept assistant messages | Kept |
| --- | --- | --- |
| Anthropic | `thinking` and `redacted_thinking` content blocks | everything else |
| Chat Completions | LiteLLM's `thinking_blocks` field (the signed Anthropic blocks) | `reasoning_content`, unsigned text that LiteLLM sends again only through `thinking_blocks` |
| Responses | `reasoning` items whose `encrypted_content` is LiteLLM's encoding of signed Anthropic thinking | OpenAI `reasoning` items: their encrypted content is not tied to the earlier messages |
| OpenCode 2 | `reasoning` parts carrying a signature (`encrypted`, or `providerMetadata.<provider>.signature` / `redactedData`) | unsigned reasoning parts and OpenAI `reasoningEncryptedContent` |

With `CONTEXT_STRIP_OLD_REASONING` on, a message that would be left with no content keeps its reasoning, so stripping never drops or empties a message; a Responses `reasoning` item is reasoning, not a message, and goes. With it `0`, such a message is dropped; it held only reasoning, so no tool call/result pair is split. Reasoning produced after the trim was generated on the trimmed view and stays, so the rendered prefix is byte-identical from one request to the next until the next trim moves `strip`. (Stripping each former latest message as newer ones arrived changed the prefix on every turn that thought and re-wrote the trimmed history to the prompt cache.) Untrimmed requests keep every block.

Only signed Anthropic reasoning is stripped. The other formats keep their reasoning because their providers need it in the tool loop: DeepSeek returns `400` when `reasoning_content` is missing from a request with `tools` ([thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/): "for requests carrying the tools parameter, the reasoning_content must be fully passed back to the API in all subsequent requests"); Gemini returns `400` when the current turn's first `functionCall` lacks its thought signature ([thought signatures](https://ai.google.dev/gemini-api/docs/thought-signatures)); OpenAI asks for every reasoning item in order ([reasoning, keeping reasoning items in context](https://developers.openai.com/api/docs/guides/reasoning#keeping-reasoning-items-in-context)); OpenRouter `reasoning_details` must match the model's output. Anthropic requires only the turn in progress: "Required: within a tool-use turn, pass thinking blocks back. Recommended: across turns, pass everything back. Allowed: outside tool use, omit prior turns' thinking" ([preserving thinking blocks](https://platform.claude.com/docs/en/build-with-claude/thinking#preserving-thinking-blocks)); the probes above show the API accepts a tool loop with thinking on its last step only.

With `CONTEXT_STRIP_OLD_REASONING` on, estimates charge signature characters at face value. Measured on four `tb21-h55-clearfit` trials (Claude Code, Haiku 5.5): `count_tokens` returns the same count with and without the thinking blocks, so it does not see them, but billed usage minus `count_tokens` of the same request without thinking (less a constant 3,754-token offset seen before any thinking) gives the thinking cost:

| Trial | Request (of the first trim) | Billed prompt | Thinking | Share | Signature chars per token |
| --- | --- | --- | --- | --- | --- |
| adaptive-rejection-sampler | 6 | 98,631 | 64,124 | 65% | 3.12 |
| regex-chess | 5 | 93,242 | 68,442 | 73% | 3.30 |
| make-mips-interpreter | 15 | 87,581 | 42,299 | 48% | 3.08 |
| make-doom-for-mips | 20 | 90,831 | about 37,000 | about 41% | about 3.25 |

At 2.3 to 3 characters per text token, face value overstates thinking by at most about 30%.

### Batched reasoning strip (`CONTEXT_STRIP_REASONING_EVERY`)

Off by default (`0`). Without a trim, every earlier thinking block stays in the request. With `N` > 0, when a request opens a new batch of N assistant steps (assistant items, as [eviction](eviction.md) counts them), the proxy and the OpenCode 2 / Pi plugin may move a recorded reasoning bound forward to the latest assistant message. They strip signed reasoning from every assistant message before it by the rules above: Anthropic `thinking` / `redacted_thinking`, Chat `thinking_blocks`, signed plugin `reasoning` parts. A message holding only reasoning keeps it, and every turn stays. A Responses `reasoning` item is left whole here, because dropping an item would shift the indices the trim state records. The bound moves only when the strip frees at least `CONTEXT_STRIP_REASONING_MIN_TOKENS` (default `8000`) estimated tokens, signatures charged at face value; otherwise it waits for the next batch. Each move rewrites the prompt cache from the first stripped block: 1-hour cache writes bill at 2× input, reads at 0.1×. On Claude 5.1+ models an edit before a kept thinking block makes the API drop that block (`input_transformations: thinking_dropped, prefix_binding_mismatch`), and the model thinks again, so moves stay rare. The bound only moves forward and is recorded in the thread state (`reason`, `reason_fp`, and `reason_batch`, the last batch checked), carried through trims. Requests between moves render the same bytes. The strip shrinks the estimate, so the threshold trim fires later; a trim also counts what the bound already took off. Each move writes a `strip_batch` [event](#events) (`tokens_before`, `tokens_after`).

Offline replay of `tb21-h55-idevict` and `tb21-h55-rtk` (96 main transcripts, Claude Code with Haiku 5.5): each request goes through the proxy (trim at 95,000, target 40,000, clearing 10, `CONTEXT_STRIP_OLD_REASONING=1`), billed as text characters over the trial's fitted ratio plus signature characters ÷ 3.2, with that usage fed back. Cache model: a request reads the longest message prefix byte-identical to the previous request's view and writes the rest. Relative cost = 2.0 × write + 0.1 × read (Haiku 5.5 ratios; uncached input is 1× but every byte here is cached or written; output and thinking-drop re-thinks not modelled):

| N / floor | Trims per trial | Dropped messages per trial | Strip moves per trial | Cache-write tokens per trial | Billed input tokens per trial | Relative cost per trial | vs off |
| --- | --- | --- | --- | --- | --- | --- | --- |
| off | 0.98 | 19.2 | 0 | 125,706 | 1,606,852 | 399,526 | — |
| 6 / 4,000 | 0.53 | 9.9 | 3.19 | 149,324 | 1,394,889 | 423,204 | +5.9% |
| 6 / 8,000 | 0.54 | 9.9 | 2.27 | 146,820 | 1,421,700 | 421,127 | +5.4% |
| 12 / 4,000 | 0.67 | 10.6 | 1.85 | 145,721 | 1,491,162 | 425,987 | +6.6% |
| 12 / 8,000 | 0.68 | 10.9 | 1.40 | 142,975 | 1,512,946 | 422,947 | +5.9% |
| 24 / 4,000 | 0.79 | 14.6 | 0.79 | 135,275 | 1,535,852 | 410,608 | +2.8% |
| 24 / 8,000 | 0.80 | 14.6 | 0.66 | 134,175 | 1,538,040 | 408,736 | +2.3% |

Every arm cuts trims by 18% to 46%, dropped messages by 24% to 48% and billed input by 4% to 13%. Every arm also costs more, because cache writes rise more than reads fall. The 8,000 floor beats 4,000 at every N, hence its default. N = 6 removes the most trims, N = 24 costs least, and N = 12 is dominated by N = 6 on both. The setting stays `0` until a benchmark arm measures pass rate against that cost; `24` / `8000` is the arm to try first.

A trim keeps the pinned first turn's and the reply's rows `in_context = 1`. `in_context` follows what the model receives: a message absent from the view (dropped, or emptied by reasoning or prose stripping) gets `0` on every row, a message that survives keeps `1` except for the tool_use / tool_result rows prose stripping removed from it and the tool_result rows observation masking (`CONTEXT_MASK_OBSERVATIONS`) replaced with its placeholder (the masked call's tool_use row stays `1`), and a stripped thinking row of a surviving message stays `1`. The pointer's `<n>` counts every message absent from the view. No trim happens, and the request passes unchanged, when nothing would drop. An OpenCode 2 compaction drops every message, the first turn included, so it marks all of the request's rows `in_context = 0`.

## Session database

`sessions/<sid>.db` is a SQLite database, one per session, in WAL mode. The proxy (Python `sqlite3`, `proxy/smarter_compact/sessiondb.py`) and the OpenCode 2 plugin (`node:sqlite`, `cli/coding-agent/session-db.mjs`) write it; the MCP tools open it read-only. The file is created mode 600; SQLite gives `-wal` and `-shm` the same mode.

### Schema (`PRAGMA user_version = 5`)

A writer that finds `user_version = 0` creates these objects in one `BEGIN IMMEDIATE` transaction, in this order and with these exact statements (both writers share the strings, `SCHEMA` then `MIGRATIONS`; the parity test compares `sqlite_master`), then sets `user_version = 5`. A writer that finds an older version runs only the statements after it (version 2 creates the `events` table, version 3 adds its usage columns, version 4 `applied_edits`, version 5 `scratchpad_chars`) in the same kind of transaction; rows already stored are untouched, and older events get `NULL` in the new columns. A writer that finds a newer version leaves the file alone and logs `UnsupportedSchema`; a reader skips it. Readers (`smart_compact_*`, `context_status`) accept versions 1 to 5; a version 1 file has no `events` table, a version 2 file has no usage columns, a version 3 file has no `applied_edits`, a version 4 file has no `scratchpad_chars`. A schema change appends to `MIGRATIONS`, bumps `user_version` and changes this section first.

```sql
CREATE TABLE messages (seq INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, thread TEXT NOT NULL,
  role TEXT NOT NULL, kind TEXT NOT NULL, name TEXT, text TEXT NOT NULL, blob_id TEXT,
  in_context INTEGER NOT NULL DEFAULT 1, trimmed_at_seq INTEGER, trimmed_at TEXT, created_at TEXT NOT NULL);
CREATE INDEX messages_thread ON messages (thread, seq);
CREATE TABLE lines (id INTEGER PRIMARY KEY, message_seq INTEGER NOT NULL REFERENCES messages (seq),
  line_no INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, UNIQUE (message_seq, line_no));
CREATE VIRTUAL TABLE messages_fts USING fts5 (text, content='messages', content_rowid='seq');
CREATE VIRTUAL TABLE lines_fts USING fts5 (text, content='lines', content_rowid='id');
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts (rowid, text) VALUES (new.seq, new.text); END;
CREATE TRIGGER lines_fts_insert AFTER INSERT ON lines BEGIN
  INSERT INTO lines_fts (rowid, text) VALUES (new.id, new.text); END;
CREATE TABLE vectors (message_seq INTEGER NOT NULL REFERENCES messages (seq), model TEXT NOT NULL,
  dim INTEGER NOT NULL, blob BLOB NOT NULL, PRIMARY KEY (message_seq, model));
-- version 2
CREATE TABLE events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL,
  location TEXT, error TEXT, tokens_before INTEGER, tokens_after INTEGER, dropped INTEGER);
-- version 3, one statement per column
ALTER TABLE events ADD COLUMN model TEXT;              -- then, in order: thread_root TEXT, trimmed INTEGER,
-- input_tokens INTEGER, cache_read_tokens INTEGER, cache_creation_tokens INTEGER, output_tokens INTEGER,
-- breakpoints INTEGER, breakpoint_at TEXT
-- version 4
ALTER TABLE events ADD COLUMN applied_edits TEXT;
-- version 5
ALTER TABLE events ADD COLUMN scratchpad_chars INTEGER;
```

| Column | Values |
| --- | --- |
| `messages.seq` | Row id, 1, 2, … in insertion order, never reused |
| `messages.fingerprint` | Chained row fingerprint, below. Unique |
| `messages.thread` | Fingerprint of the first row of the row's conversation (a subagent or side request is its own thread) |
| `messages.role` | `user` · `assistant` · `tool` |
| `messages.kind` | `text` · `tool_use` · `tool_result` · `thinking` (thinking rows carry summary text only, never signatures) |
| `messages.name` | Tool name for `tool_use` / `tool_result`, else `NULL` |
| `messages.text` | Plain text; tool inputs as compact JSON; a compressed tool result in full, as the request carried it |
| `messages.blob_id` | Blob id of a tool result the view shows compressed (`smart_compact_get`), else `NULL` |
| `messages.in_context` | `1` while the latest request that carried the row also sends it to the model; `0` once a trim, prose strip, observation masking (tool_result rows) or compaction removed it ([trim](#trim)) |
| `messages.trimmed_at_seq` | `seq` of the first row of the cut turn that dropped the row; `NULL` while in context and after a compaction (no cut turn) |
| `messages.trimmed_at` | When the row left context; `NULL` while in context |
| `messages.created_at` | When the row was stored. Timestamps are ISO 8601 UTC without fractions (`2026-10-29T17:00:00Z`) |
| `lines` | One row per `\n`-separated line of `messages.text`, `line_no` from 1, `role` copied from the message |
| `messages_fts`, `lines_fts` | FTS5 (default `unicode61` tokenizer) over `messages.text` and `lines.text`; rowid = `messages.seq` / `lines.id` |
| `vectors` | Embedding of the head of `messages.text` (first `CONTEXT_EMBEDDING_MAX_CHARS` characters, default 4000) per model: `dim` float32 values, little-endian, in `blob`. `dim = 0` with an empty `blob` marks a row skipped after repeated failures |
| `events` | One row per trim, fail-open or model call, [below](#events) |

### Rows

On every request the writer turns each message that is not pinned (system and developer messages are not stored) into rows, one per content block, as the adapters always did (`adapter.rows` in Python, `logRows` in JS). The row text comes from the request's original message, so a tool result the view compresses is stored in full and its `blob_id` names the blob.

A row's fingerprint chains it to the rows before it: `fingerprint({"prev", "role", "kind", "name", "text"})`, the trim fingerprint function (`tokens.fingerprint`: SHA-256 of sorted-key compact JSON, first 32 hex), with `prev` the previous row's fingerprint (`""` for the first). It names the row and its position; it is not the dedup key. Dedup keys a row by `(thread, content, n)`: `content` is `fingerprint({"role", "kind", "name", "text"})`, and `n` counts the earlier rows of the same thread with the same content (in message order within a request, in `seq` order over stored rows). A resent history therefore yields the same keys whatever its wire form (cache markers, string or block content); the same text said twice yields two rows (`n` 0 and 1); and an edited earlier message adds one row for its new text while the unchanged rows after it keep their keys. Claude Code does edit old messages: it appends a queued prompt (`[MESSAGE FROM NON-USER SOURCE - NOT USER INPUT]` and the text) to an earlier hook message, which under a pure chain re-inserted 642 rows of one session in one request. Limits: when a request drops or edits an earlier copy of repeated content, a later new copy takes its occurrence number and is not stored; editing the first row starts a new thread. The keys are rebuilt from the stored rows when a writer opens the database, so existing databases need no migration. The chain is over rows, not wire messages, so the proxy's Chat, Anthropic and Responses adapters and the OpenCode 2 plugin produce identical rows for the same conversation.

Writes are idempotent and cheap: each writer keeps an in-memory index of the stored rows' dedup keys (extended with new rows when `PRAGMA data_version` shows another connection committed), inserts only rows whose key it has not seen, and updates `in_context` only for rows whose flag changes, all in one `BEGIN IMMEDIATE` transaction. A request that adds nothing and moves nothing writes nothing. A database error (locked past the 1 s busy timeout, disk full, newer schema) logs one `smarter-compact: fail-open <type> at <file:line>` line and the request goes on with its rewritten view. A rewrite error after the session id is known also writes a `fail_open` [event](#events), and that request goes on unmodified.

Rows that a later request no longer carries (a rewritten history) keep their last `in_context` value.

### Events

The writer appends one `events` row per trim, per scratchpad nudge, per eviction batch that evicts something, per request that adds repeated-result or reuse pointers, per request that reduces test/build logs, per batched reasoning strip and per fail-open it can attribute to the session, and one `usage` row per model call whose request it handled. `smarter-compact context status` and the `context_status` MCP tool summarize them across sessions; `smart_compact_sql` reads one session's rows. `smarter-compact context stats` adds MCP tool counts from the append-only `tool-calls.jsonl` next to `sessions/` (one JSON line per `smart_compact_search` / `smart_compact_sql` / `smart_compact_get` call) plus message and blob inventory for those sessions.

| Column | Values |
| --- | --- |
| `at` | ISO 8601 UTC without fractions |
| `kind` | `trim` (a new cut, including a `/smarter-compact` one) · `nudge` (the scratchpad nudge was added; `tokens_before` set) · `evict` ([lifecycle eviction](eviction.md) evicted more results at a batch boundary) · `dedup` (new tool results became [repeated-result pointers](clearing.md#repeated-results-context_dedup_results); `dropped` = how many) · `reuse` ([ledger](clearing.md#ledger-context_ledger) reuse pointers were added; `dropped` = how many) · `log_reduce` ([test/build logs](clearing.md#test-and-build-logs-context_log_reduce_over_chars) were reduced; `dropped` = how many) · `strip_batch` (a [batched reasoning strip](#batched-reasoning-strip-context_strip_reasoning_every) moved its bound; `tokens_before`, `tokens_after`) · `fail_open` (the rewrite raised; the request went upstream unmodified) · `usage` (a model call finished) |
| `version` | The writer: `proxy:<12 hex>`, the first 12 hex of SHA-256 over `proxy/smarter_compact/*.py` (each file's name, NUL, bytes, NUL, sorted by name; `core.code_hash`, `proxyCodeVersion`), or `opencode:<package version>` for the OpenCode 2 plugin. Status compares the newest `proxy:` value with the checkout's hash to spot a proxy running old code |
| `location`, `error` | Fail-open only: `<file>:<line>` (JS adds `:<column>`) and the exception type. Never message content |
| `tokens_before`, `tokens_after` | Evict: the estimate with the batch's new evictions left whole and applied. Trim: the writer's calibrated estimate ([token estimate](context-proxy.md#token-estimate)) of the request before and after the cut, not provider-billed counts. `tokens_after` still counts the thinking blocks the cut voids and drops, so status measures the saving as `tokens_before` minus the billed prompt of the next usage row with `trimmed` = 2 |
| `scratchpad_chars` | Trim only, scratchpad on: characters of pad the note carried (`0` none); `NULL` with the flag off |
| `dropped` | Trim: messages absent from the trimmed view (the pointer's `<n>`). Evict: tool results the batch newly evicted |
| `model` | Usage only: the request's model (proxy: the `model` string sent to LiteLLM; OpenCode: `<providerID>/<modelID>`) |
| `thread_root` | Usage only: fingerprint of the request's first row, as in `messages.thread` |
| `trimmed` | Usage only: `0` the request went up whole, `1` it carried an earlier cut, `2` it made a new cut (the request a `trim` row describes) |
| `input_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `output_tokens` | Usage only: provider-reported counts. `input_tokens` excludes cache reads and writes, so the prompt is their sum. A count the provider did not report is `NULL`, never `0` |
| `breakpoints`, `breakpoint_at` | Usage, proxy Anthropic route (`/v1/messages`) only: the incoming request's `cache_control` markers, counted before the rewrite, and their positions in prefix order, comma-separated: `tools`, `system`, or `m-<k>` (the k-th message from the end, `m-1` the last; markers inside a `tool_result` count for its message). Other routes and OpenCode leave both `NULL`. No content is stored |
| `applied_edits` | Usage only: the response's `context_management.applied_edits` as compact JSON, e.g. `[{"cleared_input_tokens":1518,"cleared_tool_uses":2,"type":"clear_tool_uses_20250919"}]`; `[]` when the provider applied none, `NULL` when it did not report the field ([server-side clearing](context-proxy.md#server-side-tool-result-clearing)) |

Usage counts come from the provider's report, not the trim estimate:

| Source | `input_tokens` | `cache_read_tokens` | `cache_creation_tokens` | `output_tokens` |
| --- | --- | --- | --- | --- |
| LiteLLM `Usage` (proxy, `async_log_success_event`) | `prompt_tokens` minus read and creation | `cache_read_input_tokens`, else `prompt_tokens_details.cached_tokens` | `cache_creation_input_tokens`, else `prompt_tokens_details.cache_creation_tokens` / `cache_write_tokens` | `completion_tokens` |
| Responses API usage (Codex) | `input_tokens` minus `input_tokens_details.cached_tokens` | `input_tokens_details.cached_tokens` | `NULL` | `output_tokens` |
| OpenCode 2 `session.step.ended` `tokens` | `input` | `cache.read` | `cache.write` | `output` |

LiteLLM's Anthropic usage includes cache tokens in `prompt_tokens`; Chat Completions backends that report no cache detail leave both cache columns `NULL`. OpenCode reports a count its provider omitted as `0`, so an OpenCode `0` can mean "not reported". A proxy call whose response carries no usage falls back to `standard_logging_object` prompt and completion tokens, else stores all four as `NULL`.

Two fail-opens cannot be recorded and stay on stderr only: those raised before the session id is known, and those from the session database itself (the events write fails silently, so each incident logs one line). OpenCode's native compaction replacement records no event.

### Concurrency

One writer per session at a time is the design: the proxy serializes a session under its in-process lock, and OpenCode 2 sessions are written only by the plugin. Readers use WAL, so the MCP server reads while the proxy writes (`scripts/tests/test_session_db.mjs` runs a Python writer against a JS reader). The proxy writes from its container and the MCP server reads on the host over the bind mount. WAL needs shared memory between the processes (the `-shm` file), so this works only when both run on the same host kernel: Linux Docker with a bind mount. Docker Desktop (macOS, Windows), network filesystems and remote containers are unverified and not supported.

## Search

`smart_compact_search` (`cli/mcp/context-store.mjs`) opens each searched session's database read-only.

| Parameter | Effect |
| --- | --- |
| `query` | Words; each becomes an FTS5 string (`"word"`), so matching is case-insensitive and by token (`KEY-7` matches the tokens `key 7`). A row matches when it contains any word; rows with more words rank first, then by BM25 |
| `pattern` | JavaScript regex, at most 200 characters, tested against each row's first 20000 characters (row hits) and each line's first 20000 characters (line count), within a 1 s budget |
| `role` | `user`, `assistant`, `tool`, a list, or `all`; default `["user", "tool"]` |
| `in_context` | `true`: rows still in context; `false`: trimmed rows; omitted: both |
| `mode` | `hybrid` (default when the session has vectors) or `lexical` |
| `context` | 0–5 neighbouring rows of the same `thread`, any role, before and after each hit |
| `session_id`, `limit` | One session by the id the pointer gave, or recent sessions registered under the workspace's git root when omitted; result count (default 8). Resolution rules below |

| Field | Counts |
| --- | --- |
| `total_matches` | Rows that match lexically under the filters (`query`: at least one word; `pattern`: the regex matches the row) |
| `vectors` | Background embedding progress of the searched sessions for their model: `filled` (rows with a vector), `pending` (non-blank rows still waiting), `skipped` (rows given up on). Present when `CONTEXT_EMBEDDING_MODEL` is set or a session has vectors; absent for `pattern` searches |
| `line_matches` | Exact count of `lines` rows that match under the same filters, across the whole session: every word (`lines_fts MATCH "w1" AND "w2"`) for `query`, the regex for `pattern` (`^` and `$` per line, as `grep -c`). When the pattern budget runs out the response carries the partial count and `timed_out: true` |

`mode` in the response says which ranking ran. **Hybrid**: the MCP server embeds the query with the model that has the most vectors in the database (the configured `CONTEXT_EMBEDDING_MODEL` when it has vectors), takes the 50 nearest rows by brute-force cosine under the same filters, and merges them with the lexical ranking by reciprocal rank fusion (score Σ 1/(60 + rank)). Vector-only hits carry `similarity` and `matched_terms: 0`. When the query embedding fails, the response is lexical and carries `embedding_error`. `pattern` searches are always lexical.

Results carry `session_id`, `seq`, `role`, `kind`, `name`, `in_context`, an excerpt, `matched_terms` (query), `similarity` (hybrid), `blob_ids`, and `before`/`after` with `context`. `databases` maps each searched session to its database path.

## Which session a call reads

The recall tools are offered from the first request, but the session id reaches the agent only inside a trim pointer. So a call can arrive with no id, or with one the agent made up because no pointer had arrived yet. What each means:

| `session_id` | Reads |
| --- | --- |
| absent | Sessions registered under the workspace's git root, newest first (at most 20) |
| an id whose `sessions/<sid>.db` exists | That session alone |
| `current` / `this` / `these` / `latest` / `self` / `default` / `now` / `recent`, or any unknown id where the workspace has exactly one session | The workspace's sessions, newest first, and the response carries `session_id_note` naming the id that did not resolve |
| any other unknown id, with several sessions in the workspace | Nothing: the call fails and names the ids that do exist |

The middle row exists because the alternative was a `total_matches: 0` from a store that never existed, which reads to the agent as "that is not in my history" and sends it re-deriving what it already had. Substitution is never hidden: `sessions_searched` lists what was actually read and `session_id_note` says why. `smart_compact_sql` resolves the same way and runs its one statement against the newest resolved session, noting that in `session_id_note` when more than one matched.

## SQL

`smart_compact_sql(session_id, sql)` runs one `SELECT` or `WITH … SELECT` against one session database:

1. A statement check blanks comments, string literals and quoted identifiers, then rejects anything whose first word is not `SELECT` / `WITH`, any `;` before trailing whitespace, and any of `ATTACH DETACH PRAGMA INSERT UPDATE DELETE UPSERT CREATE DROP ALTER VACUUM REINDEX ANALYZE BEGIN COMMIT ROLLBACK SAVEPOINT RELEASE TRANSACTION` or `REPLACE INTO` (the `replace()` function is allowed).
2. A child process (`cli/mcp/context-sql-runner.mjs`) opens the database with `readOnly: true` (`SQLITE_OPEN_READONLY`, the same as `?mode=ro`). On Node ≥ 24.10 it also installs an authorizer that allows only SELECT, READ, FUNCTION, RECURSIVE and FTS5's internal `PRAGMA data_version`.
3. The parent kills the child after 2 s. At most 200 rows return, with `truncated: true` when there were more; text cells over 2000 characters are cut, and the whole result is halved until it fits 30000 characters (also `truncated`).

## Vectors

Vectors are off unless `CONTEXT_EMBEDDING_MODEL` names a model. The stack ships no embedding route: the LiteLLM configs route only chat models. To turn vectors on, serve an embedding model behind an OpenAI-compatible `POST <url>/embeddings` and set `CONTEXT_EMBEDDING_MODEL` and `CONTEXT_EMBEDDING_URL`.

Variables and defaults: [configuration reference](configuration.md#store-and-search).

For Ollama: `ollama pull nomic-embed-text`, then `CONTEXT_EMBEDDING_MODEL=nomic-embed-text`. Embedding runs off the request path: after a request stores new rows, the proxy queues the database for one daemon thread and the OpenCode 2 plugin starts one background run per database. Each run embeds rows without a vector for the model in this order: trimmed rows (`in_context = 0`) first, since those are what search needs, then newest first (`ORDER BY in_context, seq DESC`). The MCP server embeds only the query. The model must be the same at every end; vectors of another model are ignored by a search that picks a different one.

**Input size.** Each row sends only its first `CONTEXT_EMBEDDING_MAX_CHARS` characters (code points), so a row's vector covers its head and the rest of a long tool result is reachable only lexically. nomic-embed-text has a 2048-token context (`nomic-bert.context_length` in Ollama's `/api/show`; its `num_ctx 8192` parameter does not raise it). Code and logs run at about 2–3 characters per token, so 4000 characters stay inside it: on Ollama 0.40.1, 4000 characters of JavaScript were 1254 tokens, while 8000 hit the 2048 cap. Ollama 0.12.6 hung on inputs past the context. Rows are truncated, not chunked: several vectors per row would multiply the embedding work on the slow hardware this targets, and the `vectors` key (`message_seq`, `model`) would need a schema change.

**Batches.** Calls start at 4 rows. A full batch that finishes under a quarter of its timeout doubles the next one, up to 32. A failed call of n > 1 rows halves the batch, and the rest of that run grows no larger than the halved size. The size persists per endpoint and model between runs, so each run probes at most one larger batch. Every call logs one line to stderr: `smarter-compact: embedded rows=<n> seconds=<s> batch=<next>`, or `embedding batch failed <type> rows=<n> seconds=<s> batch=<next>`.

**Failures.** A row that fails alone (batch 1) is set aside for the rest of the run and retried in later runs after rows that never failed. After 3 failures, while the endpoint works for other rows, it gets a `dim = 0` vector (`embedding skipped seq=<seq> after 3 failures`) and is never retried; nearest-neighbour search ignores it. Failure counts live in memory, so a restart resets them. When 3 single-row calls fail in a row before any row succeeds in that run, the endpoint looks down: the run stops, logs `smarter-compact: embedding failed <type> at <file:line>` (proxy) or `fail-open` (OpenCode 2 plugin), and that database waits 60 s before the next try. Python (`proxy/smarter_compact/embed.py`) and JS (`embedPending` in `cli/coding-agent/session-db.mjs`) follow the same rules.

## Retention

`CONTEXT_STORE_TTL_DAYS` (default 30; `0` keeps everything). At start, the proxy (on a daemon thread) and the MCP server sweep `sessions/`, at most once a day: when `.ttl-stamp` is older than 24 h or missing, they touch it and delete every session whose newest file is older than the TTL. A session's files are grouped by suffix (`.db`, `.db-wal`, `.db-shm`, `.json`, `.trim.json`, `.force`, `.log.jsonl`); dot files and other names are left alone. After session cleanup, the same pass deletes orphaned `blobs/<id>.txt` files that no remaining `messages.blob_id` references and that are not the blob id of a remaining `tool_result` row's text (the blob a cleared result's placeholder names), nor of a string in a remaining `tool_use` row's input long enough to be cleared (the blob a cleared input's placeholder names; blobs are content-addressed and shared across sessions). If any remaining session database cannot be opened, the blob pass is skipped for that run. A writer that finds its cached database deleted or replaced reopens it on the next request.

**Contents.** The store holds tool output verbatim, unredacted, as Claude Code's own `~/.claude/projects/*.jsonl` transcripts do; redaction would break exact recall of values such as keys. `sessions/` and `blobs/` are mode 700, session files mode 600.

**Purge one session now.** Delete its files: `rm sessions/<sid>.*` under the context root. Its blobs stay in `blobs/` until the next daily sweep finds them unreferenced; to remove them at once, delete `.ttl-stamp` and start the MCP server or proxy.

**Logs.** `~/.config/smarter-compact/proxy.log` (plugin-started proxy) moves to `proxy.log.1` at 5 MB, checked each time the plugin starts the proxy; a proxy that stays up keeps appending until its next start. `tool-calls.jsonl` rotates the same way on write.

## Proxy-inserted text

Standing instruction, added to the system prompt of every request while trimming is enabled, from the first request on and before any trim:

```
Context management: to fit your context window, older turns may be removed and replaced by an [smarter-compact: …] pointer. Removed content is saved, not forgotten. Before answering anything that depends on earlier content you cannot see in your context — values, ids, keys, counts, decisions — call smart_compact_search (use `pattern` for exact ids/keys, `context` for neighbouring rows; `line_matches` gives exact counts) or smart_compact_sql for counts and aggregates over the session store; never guess or answer from memory you cannot see. This trim never summarizes removed content — a remembered "summary of earlier context" is not its record; cite a search result instead.
Don't repeat work: before reading a file or running a command, check whether its output is already in your context and reuse it; re-read only when it may have changed, and say why.
Rerun tests or builds only after the code changed or to get new evidence; save long output to a file and filter that instead of rerunning.
Revise an existing script instead of writing a near-copy; when repeated actions bring no new evidence, stop and take a different approach.
```

The last three lines are `REUSE_RULES` (`CONTEXT_REUSE_RULES`, default `1`; `0` drops them and restores the earlier bytes). They condense the developer-designed skills of Hu et al., [Analyzing and Mitigating Cost-Inefficient Behaviors in Coding Agents](https://arxiv.org/html/2609.30725v1) (arXiv 2609.30725, §4.3 and Appendix A.4.2), whose seven rules include "Reuse Context … check whether it is already available in the current context. If yes, reuse it instead of fetching it again. Re-read only when necessary, and briefly state why", "Extract test signal precisely from a prior run instead of re-running the test. Only rerun if the source code has changed since the last run" and "Do not create numbered variants such as `repro2.py` or `fix_v3.py`; revise the original artifact". Preloaded into the system prompt, the full set cut cost 41.73% at +0.50 pp pass@1 for Mini-SWE-Agent with Sonnet 4.6 on 200 SWE-bench Verified tasks, 13.94% at −1.50 pp for Claude Code with Sonnet 4.6, and 16.03% at −2.17 pp for Mini-SWE-Agent with MiniMax-M3 (Table 3). No run of this proxy has measured them. The text names no tool, so it fits every harness; it is static and ends the instruction, so it adds no cache break of its own.

It is a constant (`STANDING_INSTRUCTION` in `proxy/smarter_compact/core.py` and `cli/coding-agent/context-trim.mjs`), so the system prefix is byte-identical on every request and the prompt cache keeps hitting. It is added after trimming: the cut, the token estimate and the fingerprint session id never see it. A request whose system text already contains it is left alone. `CONTEXT_STANDING_INSTRUCTION=0`, or `[context_proxy] standing_instruction = false` in config.toml, turns it off; `CONTEXT_PROXY_ENABLED=0` turns it off with the trim. Placement:

| Format | Where |
| --- | --- |
| Anthropic | One text block appended to `system`; a string `system` becomes a single leading text block first, an absent one becomes `[block]` |
| Chat | One `system` message inserted after the leading `system` / `developer` messages (index 0 when there are none) |
| Responses | Appended to `instructions` after a blank line (or set as `instructions` when absent). `instructions` precede every input item and stay fixed for a session, so a trim of `input` never moves the instruction |
| OpenCode 2 | One `{type: "text"}` entry pushed onto `event.system`, after the session banner |

Pointer block placed at the start of the cut turn after a trim:

```
[smarter-compact: continuing session <sid>; <n> earlier messages are NOT in your context. They were delivered to you and later removed to fit your context window, so a gap in what you see (missing batches, lines, keys) is in them. The tools smart_compact_search and smart_compact_sql are available to you in this request. You MUST call smart_compact_search(session_id="<sid>", query=... or pattern=...) before answering anything that depends on them, and never answer "not found", "never received" or "no search tool" without calling it. Dropped user turns may hold instructions or corrections that still apply: search them (smart_compact_search with role="user") before changing course. To recover a file you read earlier, search for the text you need instead of reading it again, and re-read only the lines you still lack (offset/limit). Every message of this session is stored and queryable with smart_compact_search and smart_compact_sql(session_id="<sid>", sql=...).]
```

It names no file path, so the proxy (in its container) and the OpenCode 2 plugin (on the host) produce the same bytes.

The block is computed once per cut and stored in the trim state, so later requests repeat it byte for byte. The OpenCode 2 plugin puts the block in its own text part, followed by a blank line; when OpenCode 2 compacts a session, the same block (without the blank line) becomes the compaction summary.

Compressed tool result:

```
[smarter-compact: tool result compressed, <tokens> tokens — smart_compact_get(id="<id>") returns the full text]
<head excerpt>
…
<tail excerpt>
```

The compressed form is a pure function of the original content and the configured thresholds, so the same tool result renders to the same bytes on every request and the prompt-cache prefix stays valid.

`scripts/tests/test_context_parity.mjs` runs the fixture requests in `scripts/tests/fixtures/context-parity.json` through both implementations and requires identical views, session database contents (every `messages` and `lines` column, `sqlite_master`, `user_version`) and blobs. The fixture also holds the golden standing instruction and pointer text, which `STANDING_INSTRUCTION` and `pointer_text` / `pointerText` must reproduce byte for byte.

### Time remaining (`CONTEXT_TIME_BUDGET_SECONDS`)

Off by default (`0`). With a budget of `B` seconds, each request whose newest item holds new tool results (after the last assistant message) gets one line appended to that item:

```
[TIME REMAINING: 12m34s of 15m budget]
```

Remaining time is `B − (now − start)`, floored to whole seconds and at 0; `B` reads `15m`, or `15m30s` off a whole minute. `start` is the whole second of the first request the proxy or plugin sees for the session, kept in the trim state (`clock_start`), so a proxy restart keeps it. Each line is recorded (trim state `clock`: the item's index and fingerprint, and the text), so later requests re-render the same bytes and an item already sent never changes. Placement is the ledger's: an Anthropic text block after the user message's tool results, after a blank line in a Chat `tool` message or a Responses string `output`, after the last text result of an OpenCode 2 `tool` message. A request with no new tool result gets no line; the line never goes into the system prompt or a trim's note. It states the time only, with no advice.

Source: Wang et al., [On the Clock: Towards Punctual and Productive Time-Budgeted AI Agents](https://arxiv.org/html/2610.10833v1) (arXiv 2610.10833). Its MLE-Bench harness appends `[TIME REMAINING: 12m34s of 15m budget]` to the tool result "after every tool call" (§4.2, Table 3 "Per-tool clock", and the appendix's "Lines appended to a tool result"), alongside a periodic reminder and a generation-time line this proxy does not add. With Qwen3.6-27B at a 15-minute budget, the clock hooks cut time used from 161.4% to 99.7% of the budget, "with no measurable change in quality (47.5 to 47.0 percentile, within error)", and raised valid submissions from 78% to 88% (§4.2); Table 2 lists quality 49.4 → 42.1 for the same rows, which the text does not reconcile. The paper's prompt also tells the agent to re-read the clock and guard long commands with timeouts; this proxy adds no such instruction. No run of this proxy has measured it.

In the Terminal-Bench Harbor arms (`bench/termbench/harbor/sc_agent_claude.py`), `CONTEXT_TIME_BUDGET_SECONDS=auto` in a job's agent `env` becomes the trial's effective agent timeout: task.toml `[agent] timeout_sec` or the job's `override_timeout_sec`, capped by `max_timeout_sec`, times `agent_timeout_multiplier` (else `timeout_multiplier`), as Harbor's `Trial._compute_agent_timeout_sec` resolves it. Harbor's timeout covers the agent run only, not setup, and the clock starts at the first proxied request, a few seconds into the run.
