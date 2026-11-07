# Tool-result clearing and masking

A trim can shrink the context without deleting turns: it replaces old tool outputs with a short placeholder and keeps every call, so the agent still sees what it did, only not what it read. Two settings do this, and both are on by default.

| Setting | Default | What a trim replaces |
| --- | --- | --- |
| `CONTEXT_CLEAR_KEEP_RESULTS` | `10` | Every tool result except the 10 newest, across the whole kept context |
| `CONTEXT_MASK_OBSERVATIONS` | `1` | The tool results in the kept region before the latest run of tool calls, except the message right before that run |

A replaced result reads:

```text
[smarter-compact: tool output masked to save context; the call is kept and the output is stored, see the trim pointer]
```

## Clearing (`CONTEXT_CLEAR_KEEP_RESULTS`)

When a request reaches the trim threshold, the proxy first tries clearing alone: it keeps the current cut (or the first user turn, dropping nothing), and replaces every tool result older than the `N`-th newest. If the estimate then falls under the trim target (`CONTEXT_TRIM_TO_TOKENS`), that is the whole trim, and the note only says how to fetch a cleared result back. Otherwise it also drops the oldest turns, as a plain [trim](context-management.md) does. The clear bound only moves forward, and later requests re-render it byte for byte, so the prompt cache stays warm between trims. `0` turns clearing off.

## Masking (`CONTEXT_MASK_OBSERVATIONS`)

Masking applies to the turns a trim keeps. In the region before the latest run of tool calls, each tool result whose call is also in that region becomes the placeholder; the message right before the latest run keeps its results. Masking wins over `CONTEXT_STRIP_PROSE`, which would remove the calls as well. `0` turns it off.

With clearing on, masking mostly touches results clearing has already replaced.

## Getting a result back

Every result stays in the session database. The trim note tells the agent how to fetch one: find the call with `smart_compact_search`, then read the next `tool_result` row of the same tool with `smart_compact_sql`:

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

No run has tested the shipped default, clearing and masking together, without the scratchpad. The research behind clearing, and how this masking differs from the papers' observation masking: [context research](context-research.md#clearing-versus-turn-deletion-2026-11-05). Anthropic's server-side alternative: [context proxy](context-proxy.md#server-side-tool-result-clearing).
