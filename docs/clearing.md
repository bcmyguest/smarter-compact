# Tool-result clearing and masking

A trim can shrink the context without deleting turns: it replaces old tool outputs with a short placeholder and keeps every call, so the agent still sees what it did, only not what it read. Two settings do this, and both are on by default; a third shrinks the long inputs of those calls.

| Setting | Default | What a trim replaces |
| --- | --- | --- |
| `CONTEXT_CLEAR_KEEP_RESULTS` | `10` | Every tool result except the 10 newest, across the whole kept context |
| `CONTEXT_MASK_OBSERVATIONS` | `1` | The tool results in the kept region before the latest run of tool calls, except the message right before that run |
| `CONTEXT_CLEAR_INPUTS_OVER_CHARS` | `2000` | Each string over this many characters in the input of a call whose result is cleared, masked or evicted; `0` off |

A replaced result names its own blob:

```text
[smarter-compact: tool output cleared to save context; smart_compact_get(id="<id>") returns it]
```

`<id>` is the first 16 hex characters of the SHA-256 of the result's text (the [blob id](context-store.md#location)), so every later request renders the same bytes and the prompt cache holds. A result is replaced only when its text is longer than its placeholder. Before the request leaves, the proxy (or the OpenCode 2 plugin) writes the original text to `blobs/<id>.txt`; a result already [compressed](context-proxy.md) names the blob its compression wrote.

## Clearing (`CONTEXT_CLEAR_KEEP_RESULTS`)

When a request reaches the trim threshold, the proxy first tries clearing alone: it keeps the current cut (or the first user turn, dropping nothing), and replaces every tool result older than the `N`-th newest. With `CONTEXT_STRIP_OLD_REASONING` on (the default) the same estimate also takes off the thinking of every assistant message but the latest ([reasoning](context-store.md#signed-reasoning)), and a trim with no result to clear still tries stripping alone. If the estimate then falls under the trim target `CONTEXT_TRIM_TO_TOKENS`, that is the whole trim, and the note only says how to fetch a cleared result back (or, when only reasoning went, that every message is still there). Otherwise it also drops the oldest turns down to the target, as a plain [trim](context-management.md) does. A higher bar that left headroom below the threshold (`CONTEXT_CLEAR_FIT`) was tried and did not help; see [context research](context-research.md). The clear bound only moves forward, and later requests re-render it byte for byte, so the prompt cache stays warm between trims. `0` turns clearing off.

## Masking (`CONTEXT_MASK_OBSERVATIONS`)

Masking applies to the turns a trim keeps. In the region before the latest run of tool calls, each tool result whose call is also in that region becomes the placeholder; the message right before the latest run keeps its results. Masking wins over `CONTEXT_STRIP_PROSE`, which would remove the calls as well. `0` turns it off.

With clearing on, masking mostly touches results clearing has already replaced.

## Input clearing (`CONTEXT_CLEAR_INPUTS_OVER_CHARS`)

Clearing keeps every call whole, so a written file, an edit's old and new strings, or a heredoc in a Bash command stays in the prompt after its result is gone. With input clearing on, every string field of a call's input longer than `N` characters (strictly over; nested strings in objects and arrays too) becomes a placeholder when that call's result is cleared, masked or [evicted](eviction.md), in the same step:

```text
[smarter-compact: tool input cleared to save context; 8412 chars; smart_compact_get(id="<id>") returns it]
```

`<id>` is the blob id of the original string, written to `blobs/<id>.txt` before the request leaves. Every other field stays: keys, order, file paths, short arguments, numbers and booleans, so the input is still valid JSON of the same shape. The rule needs no tool list, so it works for every harness: Anthropic `tool_use.input`, OpenAI Chat and Responses function arguments, and the OpenCode 2 / Pi plugin's tool calls. A call counts as cleared when its result sits before the clear bound, even when the result itself is too short to replace (a Write's "File written"). The bound only moves forward, so later requests re-render the same bytes. The clearing estimate counts the shrunk inputs, so clearing alone settles more trims. The TTL sweep keeps these blobs: it keeps the blob id of every string of a stored call's input that a placeholder could name. `0` turns it off and restores the earlier bytes.

## Getting a result back

One call returns a cleared result: `smart_compact_get(id="<id>")` with the id from its placeholder, in 12,000-character pages. Addressable Recall Compaction ([ARC](https://arxiv.org/abs/2607.25066)) motivates this: recalling compacted tool outputs by id beat similarity search on needle-in-a-haystack, 99.40% against 88.12%.

The trim note says so, and keeps the older route as a fallback, since every result also stays in the session database: find the call with `smart_compact_search`, then read the next `tool_result` row of the same tool with `smart_compact_sql`:

```sql
SELECT text FROM messages
WHERE kind = 'tool_result' AND name = '<tool>' AND seq > <call seq>
ORDER BY seq LIMIT 1
```

## Evidence

Terminal-Bench 2.1, Claude Code with Haiku 5.5, eight long tasks, 23–24 trials per arm (run `tb21-h55-publish`):

| Arm | Passed | Cost (USD) |
| --- | --- | --- |
| Native compaction | 16/23 | 4.08 |
| Trim only | 13/24 | 1.68 |
| Trim + masking, no clearing | 14/23 | 2.00 |
| Trim + clearing 10, no masking | 14/23 | 1.46 |

Clearing alone had to land under the target, which at a 40,000-token target it almost never did: in run `tb21-h55-idevict` the clearing arm made 21 trims that dropped 574 messages, and 1 of 24 trials showed a placeholder. Clearing keeps every tool call whole: right after such a trim, calls' inputs (Bash commands and written files) filled about 40% of the prompt, the system prompt and tools 27%, and tool results 25%.

Thinking is the larger share in Claude Code sessions on Haiku 5.5. In four `tb21-h55-clearfit` trials it billed 42,000 to 68,000 tokens (41% to 73% of the prompt) at the first trim, while the old estimate charged its signatures nothing, so a trim that freed 64,000 billed tokens recorded 99,228 → 99,015 estimated ([reasoning](context-store.md#signed-reasoning)). An offline replay of runs `tb21-h55-idevict` and `tb21-h55-rtk` (threshold 95,000, target 40,000, `N` = 10, input clearing `2000`; each request billed as text at the trial's fitted ratio plus signature characters / 3.2, fed back as usage so the proxy calibrates as it does live; the 59 of 96 trajectories that pass 95,000):

| `CONTEXT_STRIP_OLD_REASONING` | Trims | Settled without dropping a message | Trims per trial | Messages dropped per trial | Median billed after a trim | Median estimate error after a trim | Billed input per trial |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `0` | 76 | 0 | 1.29 | 48.5 | 27,300 | +23,000 | 1.98M |
| `1` | 94 | 36 | 1.59 | 31.3 | 41,000 | −500 | 2.09M (+6%) |

With signatures charged, a trim lands at the target instead of 13,000 tokens under it, so it drops a third fewer messages, but the fuller context bills 6% more input and trims 0.3 more times per trial (each a cache rewrite, not counted here). The replay re-sends what the agent did, so it measures requests, not pass rates.

No run has tested the shipped default, clearing and masking together, without the scratchpad. The research behind clearing, and how this masking differs from the papers' observation masking: [context research](context-research.md#clearing-versus-turn-deletion-2026-11-05). Anthropic's server-side alternative: [context proxy](context-proxy.md#server-side-tool-result-clearing).
