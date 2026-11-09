# Smarter Compact

**Context management (smart compaction) for coding agents.** Long sessions keep small, cached prompts. Every message goes into a local SQLite session store. Once the prompt reaches your token limit, older turns leave it, and the next kept turn carries a pointer to them.

<p align="center">
<video src="media/smarter-compact-demo.mp4" poster="media/smarter-compact-demo-poster.png" width="720" autoplay loop muted playsinline controls aria-label="How it works: the prompt fills, turns 1–4 move to the session store, a search pulls a line back. Then a Claude Code session: the prompt crosses 95k, Smarter Compact moves older messages to the session store, and the agent later answers from the trimmed logs with smart_compact_sql and smart_compact_get (illustrative session); then the benchmark savings chart, 85% average.">
<img src="media/smarter-compact-demo.gif" alt="Smarter Compact in a Claude Code session (illustrative)" width="720" />
</video>
</p>

On ContextBench with Haiku 5.5 it cost **85% less** than keeping everything, averaged over four task sizes, and scored higher on the log tasks. [Results](benchmarks.md).

The agent searches the store with `smart_compact_search`, `smart_compact_sql`, and `smart_compact_get` when it needs a detail back. The cut stays put on later requests, so the prompt cache keeps hitting.

**Learn more**

| | |
| --- | --- |
| [Quick start](quick-start.md) | Install and set up for Claude Code, Codex, Pi, or OpenCode |
| [Agents](agents.md) | Proxy trim, in-process trim, or search only, per agent |
| [How it works](context-management.md) | How trimming works, MCP tools |
| [Benchmarks](benchmarks.md) | **ContextBench results**: quality, time, cost, prompt cache |
| [Context research](context-research.md) | Related work, design evidence, strict runs through Run E |
| [Context proxy](context-proxy.md) | LiteLLM proxy, **proxy port**, token estimate |
| [Context store](context-store.md) | File layout, schema, trim rules, search and SQL |
| [OpenCode](opencode.md) | Plugin install |
| [Configuration](configuration.md) | Every setting, its default, and the component that reads it |
