# Smarter Compact

Smart compaction keeps long coding-agent sessions under a token limit **without an LLM summary**. Trimmed turns stay verbatim in a local SQLite store; the agent searches them with `smart_compact_search`, `smart_compact_sql`, and `smart_compact_get`.

## Documentation

| Page | Contents |
| --- | --- |
| [Smart compaction overview](context-management.md) | How trimming works, when to use it, agent support |
| [Context proxy](context-proxy.md) | LiteLLM proxy, configuration, **proxy port**, token estimate |
| [Context store](context-store.md) | File layout, schema, trim rules, MCP tools |
| [OpenCode](opencode.md) | Plugin install and options |

Install and quick start: [README](https://github.com/bcmyguest/smarter-compact#readme) on GitHub.
