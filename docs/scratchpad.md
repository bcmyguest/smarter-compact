# Scratchpad

The scratchpad is a small notes file the agent keeps for its own task state. Every trim note shows it, so the goal, plan and key facts survive a trim even when the turns that held them leave the context. It is on by default; `CONTEXT_SCRATCHPAD=0` turns it off, set for both the proxy (or the OpenCode 2 plugin) and the MCP server.

```text
prompt ≥ 0.75 × trim_at, no nudge this cycle
  → proxy appends <compact-scratchpad-nudge> to the newest item
agent → smart_compact_scratchpad(command="create", ...)       → scratchpad/<sid>.md
prompt ≥ trim_at
  → trim note opens with the pad (labelled possibly stale); copy saved as <sid>.<n>.md
  → pad file stays; the nudge re-arms
agent → str_replace / create when the goal, plan or a key fact changes
next trim → shows the pad again, rewritten or not
```

## Nudge

When the estimated prompt passes `CONTEXT_SCRATCHPAD_NUDGE_AT` (default `0.75`) of the trim threshold, the proxy appends one `<compact-scratchpad-nudge>` block to the newest user or tool-result item. The text is fixed for a session and tool name and stays on that item, byte for byte, until the next trim, so the prompt cache misses once. It asks the agent to create a pad if it has none, listing what to write:

- goal and how it is checked
- status: what works, what fails
- decisions and why
- facts it would otherwise re-discover (paths, commands, values)
- the exact next step

and to leave out logs, code and command output. If a pad exists, it asks for an update only when the goal, plan or a key fact changed. The nudge re-arms after each trim. A request that trims sends no nudge, so a single step that jumps past the threshold skips it for that cycle. Nor does a request that cannot reach the tool: the nudge needs `smart_compact_scratchpad` in the request's tools, or, when Claude Code defers MCP tools behind ToolSearch, named in its deferred-tool list.

## Tool

`smart_compact_scratchpad(session_id, command, ...)`, offered unless `CONTEXT_SCRATCHPAD=0`. The commands follow [Anthropic's memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool):

| Command | Arguments | Effect |
| --- | --- | --- |
| `view` | none | The pad with numbered lines |
| `create` | `file_text` | Replaces the whole pad |
| `str_replace` | `old_str`, `new_str` | Replaces `old_str`, which must occur exactly once |
| `insert` | `insert_line`, `insert_text` | Inserts after line `insert_line` (`0` = top) |

The pad is capped at 2,500 characters. An edit whose result would exceed the cap fails with the current size and the cap, and leaves the file unchanged. Writes are atomic (temp file and rename). The file is `<store root>/scratchpad/<sid>.md` ([context store](context-store.md#location)).

## At a trim

The trim note opens with the pad, ahead of the recall instructions:

```text
Your scratchpad, kept across trims (possibly stale; update it if the goal, plan or a key fact changed):
<compact-scratchpad>
...pad text, at most 2,500 characters...
</compact-scratchpad>
[smarter-compact: session ...] ...
```

The pad file stays in place, so a later trim shows it again even if the agent never rewrites it. Each trim also saves a copy as `<sid>.<n>.md` (`n` the next free integer), which keeps a record of what each trim carried. An empty or missing pad adds no block. The trim's `events` row records `scratchpad_chars`, the characters of pad the note carried (`NULL` with the flag off; [events](context-store.md#events)).

## Observed use

Terminal-Bench 2.1, Claude Code with Haiku 5.5, trim at 95,000, eight long tasks; 34 trials with the scratchpad on, from the runs before the pad persisted across trims:

| Measure | Result |
| --- | --- |
| Trials that wrote a pad | 21 of 34 |
| Nudges answered with a write | 25 of 32 |
| Trims that carried a pad | 19 of 33 |
| Trims with no nudge first (one step jumped past the threshold) | 7 of 33 |
| Commands used | `create` only; never `view`, `str_replace` or `insert` |
| Pad size | 315–3,670 characters before the 2,500 cap; median about 1,700 |
| Steps from write to trim | 1–13, median 4 |

No run so far shows a pass-rate gain from the pad's content: in the matched runs, trials passed with and without a pad in context. The run that made the scratchpad a default found the same pass rate at lower cost: [Terminal-Bench 2.1, scratchpad, RTK and native](context-research.md#terminal-bench-21-scratchpad-rtk-and-native-2026-11-07). Method and prior art: [context research](context-research.md#scratchpad-prior-art).
