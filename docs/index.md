# Smarter Compact

**Context management (smart compaction)** keeps long coding-agent sessions under a token limit without an LLM summary. Every message goes into a local SQLite session store; older turns leave the context and the turn they precede carries a pointer to them. The agent searches the store with `smart_compact_search`, `smart_compact_sql`, and `smart_compact_get` when it needs a detail back.

<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="media/smart-compaction-dark.gif" />
<img src="media/smart-compaction.gif" alt="Smart compaction animation" width="640" />
</picture>
</p>

**Learn more**

| | |
| --- | --- |
| [Smart compaction](context-management.md) | How trimming works, **ContextBench results**, agents, MCP tools |
| [Context research](context-research.md) | Related work, design evidence, strict runs through Run E |
| [Context proxy](context-proxy.md) | LiteLLM proxy, env vars, **proxy port**, token estimate |
| [Context store](context-store.md) | File layout, schema, trim rules, search and SQL |
| [OpenCode](opencode.md) | Plugin install and options |

Install and quick start: [README](https://github.com/bcmyguest/smarter-compact#readme) on GitHub.
