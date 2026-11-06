# Smarter Compact

**Context management (smart compaction) for coding agents.** Long sessions keep small, cached prompts: every message goes into a local SQLite session store, older turns leave the prompt once it reaches your token limit, and the turn they precede carries a pointer to them.

<p align="center">
<video src="media/smarter-compact-demo.mp4" poster="media/smarter-compact-demo-poster.png" width="720" autoplay loop muted playsinline controls aria-label="A Claude Code session: the prompt crosses 95k, Smarter Compact moves older messages to the session store, and the agent later answers from the trimmed logs with smart_compact_sql and smart_compact_get. Illustrative session.">
<img src="media/smarter-compact-demo.gif" alt="Smarter Compact in a Claude Code session (illustrative)" width="720" />
</video>
</p>

In seed-matched benchmarks it cost **72% less on average** than keeping everything, across four task sizes, and scored higher on the log tasks. [Results](context-management.md#benchmarks).

The agent searches the store with `smart_compact_search`, `smart_compact_sql`, and `smart_compact_get` when it needs a detail back. The cut stays put on later requests, so the prompt cache keeps hitting.

**Learn more**

| | |
| --- | --- |
| [Smart compaction](context-management.md) | How trimming works, **ContextBench results**, agents, MCP tools |
| [Context research](context-research.md) | Related work, design evidence, strict runs through Run E |
| [Context proxy](context-proxy.md) | LiteLLM proxy, env vars, **proxy port**, token estimate |
| [Context store](context-store.md) | File layout, schema, trim rules, search and SQL |
| [OpenCode](opencode.md) | Plugin install and options |

Install and quick start: [README](https://github.com/bcmyguest/smarter-compact#readme) on GitHub.
