# Reddit launch

Start with Reddit. Assets are in `promo/out/` (rebuild with `promo/build.sh`).

**Before posting:** publish a new npm version. npm still serves 0.3.2, and the post tells people to `npm install`.

## What to upload

Upload **`smarter-compact-demo.mp4`** as a native Reddit video post. Reddit plays MP4 smoothly and autoplays it in the feed; a GIF gets re-encoded and looks worse. Put the text below in the body (video posts allow a body on most subs) or as your first comment if the sub doesn't.

Use `card-results-2400x1350.png` if a sub only allows image posts.

## Where, in order

1. **r/ClaudeAI**: the biggest audience for Claude Code users. Use the project/showcase flair, and check the self-promotion rules on the day.
2. **r/ClaudeCode**: smaller, more technical. Post a day later and lead with the Claude Code setup.
3. **r/LocalLLaMA**: only if you lead with the mechanism (SQLite + FTS5, MCP tools, LiteLLM proxy) and the methods link. This crowd punishes marketing tone.

Post on a weekday morning US time, and stay around for the first two hours to answer questions. Early replies decide whether it climbs.

## Title options

- **Smarter Compact: older turns of a long agent session go into SQLite, and the agent queries them back. Peak prompt 762k → 95k on a long log task**
- **I built a context manager for Claude Code: every turn kept verbatim in local SQLite, prompt stays under 95k, agent searches what it needs**
- **Long Claude Code sessions at 95k tokens instead of 762k: a local SQLite store the agent can search and run SQL on**

## Body

> I built Smarter Compact to keep long coding-agent sessions fast and cheap without losing anything.
>
> Once the prompt reaches a token threshold (95k on Haiku), older turns move into a local SQLite file, word for word. The turn after the cut carries a pointer, and the agent gets three MCP tools to pull history back:
>
> - `smart_compact_search`: full-text and ranked search over every turn
> - `smart_compact_sql`: read-only SQL, so "how many X" becomes a `count(*)`
> - `smart_compact_get`: fetch an exact message
>
> The cut is sticky: later requests get the same prefix byte for byte, so the prompt cache keeps hitting.
>
> **Results** (ContextBench log-triage, largest size, Claude Haiku 5.5, 2–3 runs per arm):
>
> | | Keep everything | Smarter Compact |
> | --- | --- | --- |
> | Peak prompt | 762k | 95k |
> | Cost per trial (list price) | $6.07 | $0.38 |
> | Lookup questions | 0.71 | 1.00 |
> | Count questions | 0.00 | 1.00 |
>
> Pooled over all log sizes it scored +0.47 over keep-everything (95% CI 0.34–0.58, eight seed-matched pairs). Claude Code's own compaction never fired in these runs, so it behaved like keep-everything.
>
> Honest limits: these are synthetic tasks and small samples, the cost saving at the smallest size isn't significant, and real-session results (Compact-Bench) are still pending.
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

- 762k → 95k peak prompt and $6.07 → $0.38 apply only at log-triage L12800. Don't say "16× cheaper" in general: at L1600 the ratio is 0.81 and its interval crosses 1.
- Count questions 0.00 → 1.00 and lookups 0.71 → 1.00 at L12800.
- +0.47 pooled (95% CI 0.34–0.58), eight pairs.
- Always mention synthetic tasks, Haiku 5.5, and 2–3 runs per arm.

The animation's line shape is illustrative; its peaks and costs are measured, and its trims follow that run's settings (at 95k, down to about 29k).

## Later: other channels

When Reddit has run, the same assets fit X (video plus the results card as a second image), LinkedIn (video), and Show HN. HN shows no images, so set `github-social-preview-1280x640.png` under GitHub → Settings → Social preview first.
