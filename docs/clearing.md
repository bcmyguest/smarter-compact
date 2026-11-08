# Tool-result clearing and masking

A trim can shrink the context without deleting turns: it replaces old tool outputs with a short placeholder and keeps every call, so the agent still sees what it did, only not what it read. Two settings do this, and both are on by default; a third shrinks the long inputs of those calls.

| Setting | Default | What a trim replaces |
| --- | --- | --- |
| `CONTEXT_CLEAR_KEEP_RESULTS` | `10` | Every tool result except the 10 newest, across the whole kept context |
| `CONTEXT_CLEAR_FIT` | `0.63` | Nothing itself: clearing alone settles a trim once the prompt fits under this fraction of the threshold |
| `CONTEXT_MASK_OBSERVATIONS` | `1` | The tool results in the kept region before the latest run of tool calls, except the message right before that run |
| `CONTEXT_CLEAR_INPUTS_OVER_CHARS` | `2000` | Each string over this many characters in the input of a call whose result is cleared, masked or evicted; `0` off |

A replaced result names its own blob:

```text
[smarter-compact: tool output cleared to save context; smart_compact_get(id="<id>") returns it]
```

`<id>` is the first 16 hex characters of the SHA-256 of the result's text (the [blob id](context-store.md#location)), so every later request renders the same bytes and the prompt cache holds. A result is replaced only when its text is longer than its placeholder. Before the request leaves, the proxy (or the OpenCode 2 plugin) writes the original text to `blobs/<id>.txt`; a result already [compressed](context-proxy.md) names the blob its compression wrote.

## Clearing (`CONTEXT_CLEAR_KEEP_RESULTS`)

When a request reaches the trim threshold, the proxy first tries clearing alone: it keeps the current cut (or the first user turn, dropping nothing), and replaces every tool result older than the `N`-th newest. If the estimate then falls under `CONTEXT_CLEAR_FIT` times the trim threshold (or under the trim target `CONTEXT_TRIM_TO_TOKENS`, whichever is higher), that is the whole trim, and the note only says how to fetch a cleared result back. Otherwise it also drops the oldest turns down to the trim target, as a plain [trim](context-management.md) does. The bar sits well below the threshold on purpose: the headroom it leaves lets the next steps' output arrive without re-triggering a trim, each of which breaks the prompt cache once. At the default `0.63` and a 95,000-token threshold, clearing alone must land under 59,850 tokens, leaving about 35,000 of headroom. A value at or below target/threshold restores the earlier rule (clearing alone only under the target); `0 < x < 1`, else the default. The clear bound only moves forward, and later requests re-render it byte for byte, so the prompt cache stays warm between trims. `0` turns clearing off.

## Masking (`CONTEXT_MASK_OBSERVATIONS`)

Masking applies to the turns a trim keeps. In the region before the latest run of tool calls, each tool result whose call is also in that region becomes the placeholder; the message right before the latest run keeps its results. Masking wins over `CONTEXT_STRIP_PROSE`, which would remove the calls as well. `0` turns it off.

With clearing on, masking mostly touches results clearing has already replaced.

## Input clearing (`CONTEXT_CLEAR_INPUTS_OVER_CHARS`)

Clearing keeps every call whole, so a written file, an edit's old and new strings, or a heredoc in a Bash command stays in the prompt after its result is gone. With input clearing on, every string field of a call's input longer than `N` characters (strictly over; nested strings in objects and arrays too) becomes a placeholder when that call's result is cleared, masked or [evicted](eviction.md), in the same step:

```text
[smarter-compact: tool input cleared to save context; 8412 chars; smart_compact_get(id="<id>") returns it]
```

`<id>` is the blob id of the original string, written to `blobs/<id>.txt` before the request leaves. Every other field stays: keys, order, file paths, short arguments, numbers and booleans, so the input is still valid JSON of the same shape. The rule needs no tool list, so it works for every harness: Anthropic `tool_use.input`, OpenAI Chat and Responses function arguments, and the OpenCode 2 / Pi plugin's tool calls. A call counts as cleared when its result sits before the clear bound, even when the result itself is too short to replace (a Write's "File written"). The bound only moves forward, so later requests re-render the same bytes. The clear-fit estimate counts the shrunk inputs, so clearing alone settles more trims. The TTL sweep keeps these blobs: it keeps the blob id of every string of a stored call's input that a placeholder could name. `0` turns it off and restores the earlier bytes.

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

Before `CONTEXT_CLEAR_FIT`, clearing alone had to land under the target, which at a 40,000-token target it almost never did: in run `tb21-h55-idevict` the clearing arm made 21 trims that dropped 574 messages, and 1 of 24 trials showed a placeholder. An offline replay of the 20 trajectories from runs `tb21-h55-idevict` and `tb21-h55-rtk` that passed 95,000 estimated tokens (threshold 95,000, target 40,000, `N` = 10) chose `0.63`, the highest value that keeps 35,000 tokens of headroom:

| `CONTEXT_CLEAR_FIT` | Trims | Settled by clearing alone | Trims per trial | Messages dropped per trial | Placeholders per trial |
| --- | --- | --- | --- | --- | --- |
| target (earlier rule) | 21 | 0 | 1.05 | 45.5 | 4.8 |
| 0.5 | 21 | 2 | 1.05 | 43.4 | 5.9 |
| 0.55 | 21 | 4 | 1.05 | 39.8 | 7.5 |
| 0.63 | 21 | 6 | 1.05 | 36.2 | 9.2 |
| 0.8 | 28 | 25 | 1.40 | 9.4 | 27.0 |

Clearing alone leaves a median of about 64,000 tokens in these trajectories, because clearing keeps every tool call whole: right after such a trim, calls' inputs (Bash commands and written files) fill about 40% of the prompt, the system prompt and tools 27%, and tool results 25%; at `0.8` it settles most trims, but the smaller headroom adds a trim in 7 of 20 trials. The replay re-sends what the agent did, so it measures requests, not pass rates.

The same replay at `0.63` with input clearing (`CONTEXT_CLEAR_INPUTS_OVER_CHARS`):

| Input clearing | Trims | Settled by clearing alone | Messages dropped per trial | Result placeholders per trial | Input placeholders per trial | Median estimate after a trim |
| --- | --- | --- | --- | --- | --- | --- |
| off | 21 | 6 | 36.2 | 9.2 | 0 | 40,400 |
| `2000` | 22 | 12 | 21.9 | 17.6 | 1.1 | 43,600 |

No run has tested the shipped default, clearing and masking together, without the scratchpad. The research behind clearing, and how this masking differs from the papers' observation masking: [context research](context-research.md#clearing-versus-turn-deletion-2026-11-05). Anthropic's server-side alternative: [context proxy](context-proxy.md#server-side-tool-result-clearing).
