# Reddit launch: r/ClaudeAI

One subreddit, one post. Assets are in `promo/out/` (rebuild with `promo/build.sh`).

**Before posting:** publish a new npm version. npm still serves 0.3.2, and the post tells people to `npm install`.

## What to upload

Post **`terminal-demo.mp4`** as a native video. It shows a Claude Code session the way readers know it: three prompts, the trim when the context crosses 95k, then a question about logs that already left the prompt, answered with `smart_compact_sql` and `smart_compact_get`. It ends on the 72% average. Reddit autoplays MP4 in the feed; a GIF gets re-encoded and looks worse.

Put the body below in the post. Then add `card-results-2400x1350.png` (the savings bars) as your first comment, for the people who want the chart.

Keep `smarter-compact-demo.mp4` (the graphs video) for X and LinkedIn later.

## Posting

- Use the project/showcase flair and check r/ClaudeAI's self-promotion rules on the day.
- Post on a weekday morning, US time.
- Stay around for the first two hours and answer every question. Early replies decide whether it climbs.

## Title options

- **Smarter Compact: keeps long coding-agent sessions small by moving older turns to a local SQLite store the agent can search. 72% lower cost on average in benchmarks**
- **I built a context manager for Claude Code: every turn kept verbatim, prompt stays under your limit, agent queries its own history**
- **Long Claude Code sessions with small, cached prompts: older turns go to SQLite, the agent searches them when it needs a detail**

## Body

> I built Smarter Compact to keep long coding-agent sessions fast and cheap without losing anything.
>
> **What it does**
>
> - Every turn is saved word for word to a local SQLite file.
> - Once the prompt reaches your token limit (95k on Haiku), older turns leave the prompt. The cut is sticky, so the prompt cache keeps hitting.
> - The agent gets three MCP tools to pull history back: `smart_compact_search` (full-text, ranked), `smart_compact_sql` (read-only SQL, so "how many X" is a `count(*)`) and `smart_compact_get` (an exact message).
>
> **Example: one long log-triage session** (ContextBench L12800, Claude Haiku 5.5)
>
> | | Keep everything | Smarter Compact |
> | --- | --- | --- |
> | Peak prompt | 762k | 95k |
> | Cost per trial (list price) | $6.07 | $0.38 |
> | Lookup questions | 0.71 | 1.00 |
> | Count questions | 0.00 | 1.00 |
>
> **On average: 72% lower cost** across four task sizes, seed-matched against keeping everything (−87%, −19%, −89%, −94%). The smallest size's saving isn't significant; on the log tasks, the longer the session, the bigger the saving. Answers got better too: +0.47 score on the log tasks (95% CI 0.34–0.58, eight pairs).
>
> Honest limits: synthetic tasks, small samples (2–3 runs per size), and real-session results (Compact-Bench) are still pending. Claude Code's own compaction never fired in these runs, so it behaved like keep-everything.
>
> Works with Claude Code, Codex, OpenCode and Pi. Local only; it has no API key of its own.
>
> ```
> npm install -g smarter-compact
> smarter-compact enable -g
> ```
>
> Repo, full tables and methods: https://github.com/bcmyguest/smarter-compact
>
> Happy to answer questions, especially about where it doesn't help.

## Replies to have ready

- **"Isn't this just RAG over the transcript?"** Close. The difference is what stays in the prompt: recent turns stay verbatim, the cut is deterministic so the cache keeps hitting, and SQL handles counts that similarity search gets wrong.
- **"Why not Claude Code's /compact?"** That replaces history with a model-written summary. Here every turn stays retrievable word for word, and no model call is spent on a summary.
- **"Does it slow things down?"** Slightly: 8.8 min vs 8.2 min per trial on average, with more requests because the agent searches.
- **"Coding sessions, not log puzzles?"** Compact-Bench (real SWE-bench sessions) is the next run. On Terminal-Bench, most sessions trimmed only once or twice.

## Numbers that are safe to say

- **72% average cost saving** is the mean of four paired cost ratios (0.130, 0.811, 0.109, 0.062 → 0.28). Say "on average across four benchmark sizes", not "72% cheaper" flat: per size it ranges from 19% (not significant) to 94%.
- 762k → 95k peak prompt and $6.07 → $0.38 apply only to the L12800 example.
- Count questions 0.00 → 1.00 and lookups 0.71 → 1.00 at L12800.
- +0.47 pooled score (95% CI 0.34–0.58), eight pairs.
- Always mention synthetic tasks, Haiku 5.5, and 2–3 runs per size.

The example chart's line shape is illustrative; its peaks and costs are measured, and its trims follow that run's settings (at 95k, down to about 29k).

## Later: other channels

When Reddit has run, the same assets fit X (video plus the results card as a second image), LinkedIn (video), and Show HN. HN shows no images, so set `github-social-preview-1280x640.png` under GitHub → Settings → Social preview first.
