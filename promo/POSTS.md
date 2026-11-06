# Launch posts

Assets are in `promo/out/` (rebuild with `promo/build.sh`). Before posting, publish a new npm version: npm still serves 0.3.2, and every post below tells people to `npm install`.

| Asset | Use it for |
| --- | --- |
| `smarter-compact-demo.mp4` (1920×1080, 19 s) | X, LinkedIn, Bluesky: upload the video natively, don't link it |
| `smarter-compact-demo.gif` (800 px, 1.2 MB) | Reddit, GitHub README, Discord |
| `card-results-2400x1350.png` | Second image in a thread; the "numbers" slide |
| `card-hero-2400x1350.png` | Link preview / first image when the video won't play |
| `card-chart-2400x1350.png` | Reply to "show me the data" |
| `github-social-preview-1280x640.png` | GitHub → Settings → Social preview (this is what HN, Slack and X show for the repo link) |

## Numbers you can say (and how to say them)

- **Peak prompt 762k → 95k, cost $6.07 → $0.38 per trial.** Only at log-triage L12800 (the longest size). Don't call it "16× cheaper" in general: at L1600 the ratio is 0.81 and its interval crosses 1.
- **Count-question score 0.00 → 1.00 at L12800.** Lookup questions were 0.71 → 1.00. Keep-everything's 0.35 overall comes from getting every count question wrong.
- **Pooled across all log sizes: +0.47 score (95% CI 0.34–0.58), eight seed-matched pairs.**
- Always attach: ContextBench synthetic tasks, Claude Haiku 5.5, 2–3 runs per cell, not coding sessions.

The animation's line shape is illustrative; the peaks and costs on it are measured. The footers on every asset say so.

---

## X / Bluesky thread

**1/** (attach `smarter-compact-demo.mp4`)

> Long coding-agent sessions don't need an LLM summary.
>
> Smarter Compact moves older turns out of the prompt into a local SQLite store, verbatim. The agent searches them when it needs a detail back.
>
> On the longest log task: peak prompt 762k → 95k tokens.

**2/** (attach `card-results-2400x1350.png`)

> Same task, same model (Haiku 5.5), ContextBench log-triage at its largest size:
>
> • cost per trial $6.07 → $0.38
> • count questions 0.00 → 1.00
> • lookups 0.71 → 1.00
>
> 2–3 runs per arm, synthetic tasks. Smaller sizes save less.

**3/**

> How: once the prompt crosses a threshold (95k on Haiku), older turns drop out. The turn they precede gets a pointer saying they're stored.
>
> The agent gets smart_compact_search, smart_compact_sql and smart_compact_get. Counting questions become a SQL query over stored lines instead of rereading 700k tokens.

**4/**

> The cut is sticky: later requests get the same rewrite byte for byte, so the prompt cache keeps hitting after the trim.
>
> Works with Claude Code, Codex, OpenCode and Pi. Local only; no API key of its own.

**5/**

> npm install -g smarter-compact
> smarter-compact enable -g
>
> Code, benchmarks and methods: github.com/bcmyguest/smarter-compact

## LinkedIn

(attach `smarter-compact-demo.mp4`)

> Coding agents get slower and more expensive the longer a session runs. The usual fix is compaction: an LLM writes a summary and the details are gone.
>
> I built Smarter Compact to take a different approach. Once the prompt crosses a token threshold, older turns move into a local SQLite store, word for word. The agent gets search and SQL tools to pull back exactly what it needs.
>
> On ContextBench's longest log-triage task (Claude Haiku 5.5, 2–3 runs per arm):
> → peak prompt fell from 762k to 95k tokens
> → list-price cost per trial fell from $6.07 to $0.38
> → count questions went from 0.00 to 1.00
>
> Across all log sizes, eight seed-matched pairs showed a +0.47 score gain (95% CI 0.34–0.58). These are synthetic tasks, not real coding sessions, and the savings are much smaller on short sessions.
>
> Open source, Apache-2.0. Works with Claude Code, Codex, OpenCode and Pi.
> github.com/bcmyguest/smarter-compact

## Reddit (r/ClaudeAI, r/ClaudeCode, r/LocalLLaMA)

Title: **I replaced Claude Code's compaction summary with a SQLite store the agent can query (peak prompt 762k → 95k on a long log task)**

(embed `smarter-compact-demo.gif` at the top)

> When a session gets long, Smarter Compact drops older turns from the request and keeps them verbatim in a local SQLite file. The turn after the cut carries a pointer, and the agent gets three MCP tools: `smart_compact_search` (FTS5 + ranking), `smart_compact_sql` (read-only SQL, so "how many X" is a `count(*)`), and `smart_compact_get`.
>
> **Results** (ContextBench log-triage, Haiku 5.5, 2–3 runs per arm, synthetic tasks):
>
> | L12800 | Keep everything | Smarter Compact |
> | --- | --- | --- |
> | Peak prompt | 762k | 95k |
> | Cost per trial | $6.07 | $0.38 |
> | Lookup score | 0.71 | 1.00 |
> | Count score | 0.00 | 1.00 |
>
> Pooled over all log sizes, it beat keep-everything by +0.47 (95% CI 0.34–0.58, eight pairs). At the smallest size the cost saving isn't significant. Claude Code's default compaction never fired in these runs, so it behaved like keep-everything.
>
> Caveats: small samples, synthetic tasks, and the proxy path needs a LiteLLM build. Real-session results (Compact-Bench) are still pending.
>
> `npm install -g smarter-compact && smarter-compact enable -g`. Repo and full tables: github.com/bcmyguest/smarter-compact

## Show HN

HN shows no images, so the repo's social preview and the README GIF do the visual work. Set the social preview before posting.

Title: **Show HN: Smarter Compact – context trimming for coding agents, no LLM summary**

> Smarter Compact keeps long coding-agent sessions under a token limit without summarizing. Older turns move into a local SQLite store verbatim, and the agent gets search/SQL tools to pull details back. The cut is deterministic, so the prompt cache keeps hitting after a trim.
>
> On ContextBench's longest log-triage size (Haiku 5.5, 2–3 runs per arm), peak prompt went from 762k to 95k tokens and list-price cost per trial from $6.07 to $0.38. Count questions went from 0.00 to 1.00 because "how many" becomes a SQL count over stored lines. Pooled across sizes, eight seed-matched pairs gave +0.47 score (95% CI 0.34–0.58). These are synthetic tasks; the effect is much smaller on short sessions, and real-session results are pending.
>
> Works with Claude Code, Codex, OpenCode and Pi. Methods and raw tables: https://bcmyguest.github.io/smarter-compact/context-research.html
