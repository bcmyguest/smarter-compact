# Cache-thinking probe

Asks one question: when the proxy cuts a Claude Code session, how much of Anthropic's prompt cache survives, with
extended thinking on and off? Results: [docs/context-research.md](../../../docs/context-research.md#cache-after-a-cut-with-thinking-2026-11-05).

## Setup (`run.py`)

- A probe proxy from this checkout on port 4121 (`PROBE_PORT`), started with `../constraint/compose.probe.yaml`
  under compose project `sc-probe-cache`, state under `/tmp/sc-probe-cache/state` (`PROBE_ROOT`),
  `CONTEXT_TRIM_AT_TOKENS=55000`. Claude Code's tools and system prompt alone are about 42K tokens, so a lower
  threshold cuts every request.
- Per run, a fresh `CLAUDE_CONFIG_DIR` holding only a copy of `~/.claude/.credentials.json` and a `settings.json`
  with the proxy URL, the `x-litellm-api-key` header and the arm's thinking setting.
- Ten `claude -p` turns on `claude-haiku-5-5` (`PROBE_MODEL`), the first with `--session-id`, the rest `--resume`:
  list the files, then nine times "Read notes_k.txt in full ... reply with only its last word and the number of
  lines". Each `notes_k.txt` is 16 KB of seeded random words (about 5K tokens), so the session cuts twice.

| Arm | Claude Code thinking setting |
| --- | --- |
| `think` | `alwaysThinkingEnabled: true` |
| `nothink` | `alwaysThinkingEnabled: false`, `MAX_THINKING_TOKENS=0` (the 2026-11-05 run); `CLAUDE_CODE_DISABLE_THINKING=1` added after it |

`summary.json` per run holds the main thread's `usage` events from the session database (`trimmed`, `input`,
`cache_read`, `cache_creation`, `output`), `cuts` (for each new cut: the request before it, the cut request and
the next two), `transcript_blocks` (assistant block types in Claude Code's own transcript; `thinking` blocks show
thinking was on) and `cost_usd` (Claude Code's `total_cost_usd`, which is the resumed session's running total).

```bash
python3 bench/probe/cache-thinking/run.py --arms think,nothink --n 1 --budget 2
```

## Known issue

On Claude Code 2.1.280 with `claude-haiku-5-5`, the `nothink` settings did not turn thinking off: its transcript
holds 12 signed thinking blocks, as `think` holds 11. `CLAUDE_CODE_DISABLE_THINKING=1` did (one check call, no
thinking block), so `run.py` now sets it for `nothink`; that arm has not been rerun.
