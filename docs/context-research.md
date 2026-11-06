# Context research

Why the [context proxy](context-proxy.md) trims the way it does, which published work shaped it, and what our own benchmark runs show. Results are interim, dated 2026-10-30; run D is complete, runs B and C are partial. Bibliography: [References](https://github.com/bcmyguest/openmem/blob/main/docs/references.md) (OpenMem repo).

## Problem

Long agent sessions outgrow the context window. Agents handle this in one of three ways, and each fails differently:

| Approach | Failure |
| --- | --- |
| Keep everything | The prompt grows until the window fills, and accuracy drops as input grows ("context rot", below). In our runs a kv-store session reached 979k tokens and never answered. |
| Native auto-compaction (LLM summary) | The summary keeps the gist but drops exact values. In our strict run it scored 0.049. |
| Trim + pointer + search (this proxy) | The agent must remember to search. Before the standing instruction, 2 of 12 trials never searched. |

<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="media/context-research-1-dark.svg" />
<img src="media/context-research-1.svg" alt="Flowchart: agent session, over trim threshold?, forward unchanged, keep system tools first user turn last user turn newest tool exchanges, SQLite session store, pointer to smart_compact_search or smart_compact_sql" width="720" />
</picture>
</p>

## Related work

| Work | Approach | What we took | How we differ |
| --- | --- | --- | --- |
| [MemGPT](https://arxiv.org/abs/2310.08560) (Packer et al., 2023) | OS-style virtual context: a small main context plus searchable recall storage, paged in by function calls. | Verbatim recall storage behind search tools instead of a summary. | No self-editing memory; the proxy trims and the agent only searches. |
| [MemGPT DMR](https://arxiv.org/abs/2310.08560) (Table 2) and [MSC-Self-Instruct](https://huggingface.co/datasets/MemGPT/MSC-Self-Instruct) | Deep memory retrieval questions over earlier Multi-Session Chat sessions. GPT-4 scored 32.1% fixed-context and 92.5% with MemGPT; GPT-4 Turbo scored 35.3% and 93.4%. | Evidence that search over stored turns beats a fixed window. | Its conversations fit in today's windows, so it doesn't force trimming. Not run here. |
| MemGPT nested key-value retrieval (same paper) | Multi-hop lookups across key-value pairs. Fixed-context GPT-4 and GPT-4 Turbo reach 0% by 3 nesting levels; MemGPT with GPT-4 is unaffected by nesting depth. | Closest published analogue to our kv-store task. | Not run here. |
| [The Complexity Trap](https://arxiv.org/html/2508.21433v3) (Lindenbauer et al., JetBrains Research, 2025) | SWE-agent on SWE-bench Verified: observation masking (replace old tool outputs with a placeholder, keep the last 10) against LLM summarization. Masking roughly halves cost against the raw agent and matches summarization's solve rate; Qwen3-Coder 480B scored 54.8% at $0.61 masked, 53.8% at $0.64 summarized, 53.4% at $1.29 raw. | Evidence that removal without an LLM summary is a sound default. | Masking drops only observations and keeps the agent's reasoning and actions; we drop whole turns but keep them searchable. |
| [transparent-compact: Maximising Transparency in Context Compaction Improves Retention of Facts and Commitments for Lightweight Coding Agents](https://arxiv.org/abs/2610.20116v1) (Woolford, 2026-10-14) | Deterministic compaction for the Pi coding agent: user and assistant prose stays verbatim and every cut is flagged. Matched or beat Pi's LLM-summary compaction and pi-vcc on commitment and factual-recall probes across four models, with fewer confident wrong answers; the abstract gives no numbers. | The prose-strip arm, `CONTEXT_STRIP_PROSE` ([result](#terminal-bench-21-trim-shape-arms-2026-11-05)). | We strip prose only from kept turns before the strip bound; dropped turns stay searchable. |
| [Compact-Bench: Measuring What Context Compaction Costs Long-Horizon Coding Agents](https://arxiv.org/abs/2610.31710) (Sun et al., 2026-10-22) | Same repo state continued from full history or from a summary: 35 SWE-bench repairs, 146 sessions, 106 production compaction chains. Mechanical extraction kept 79.1% of recurring details against at most 16.0% for LLM summaries; detail retention did not predict task completion, and the authors conclude compaction must keep "the progress needed to complete a task". | The progress-note arm, `CONTEXT_PROGRESS_NOTE`: files edited and last build/test commands, built from tool calls, no model ([result](#terminal-bench-21-trim-shape-arms-2026-11-05)). | Our note lists only what the trim removed and rides on the pointer. |
| [AutoCompact: Learning When to Compact Context in Long-Horizon Coding Agents](https://arxiv.org/abs/2610.02163) (Zhang et al., 2026-10-01; [PREreview](https://prereview.org/reviews/23129310)) | A judge corrects the base agent's compaction decisions and summaries; the corrected trajectories train coding and compaction jointly (SFT, then RL on task success). Gains held at a 256K window and at 16K with fallback compaction. The review agrees that compacting when the window fills lands mid-stage, the worst moment. | Motivation for trimming at a stage boundary. | Not built; we do not train the model. |
| [CompactionRL: Reinforcement Learning with Context Compaction for Long-Horizon Agents](https://www.alphaxiv.org/abs/2607.05378) | Trains the agent with RL to write its own compaction summaries as part of the task; consistent gains on agentic coding tasks with open models. | Nothing yet. | Learned summaries; we keep removed turns verbatim. |
| [Self-Compacting Language Model Agents](https://arxiv.org/pdf/2606.23525) | Found in the same 2026-11 search; not read. | — | — |
| [Letta: Is a Filesystem All You Need?](https://www.letta.com/blog/benchmarking-ai-agent-memory/) (2025-08-12) | Conversation history stored as files; agent uses `grep`, `search_files`, `open`, `close`. 74.0% on LoCoMo with GPT-4o mini, against 68.5% for Mem0's best graph variant. | File-like tools (search, open) work well; models already use them iteratively. | Letta's files are server-side and reached through tools. We copy that: the store is reached through MCP tools, not a shell. |
| [Mainplane](https://github.com/mainplane-ai/mainplane) | Each session is one append-only state file. `/continue` starts a new session with "You are continuing session X. Retrieve key context from it as you go." A standing "Context Management" system-prompt section says the previous session's log "is searchable with rg". | The pointer-and-retrieve continuation, and a standing system instruction telling the agent the content exists and how to fetch it. | We trim in place, keeping the same session and prompt-cache prefix, instead of starting a new session. Access is through tools, not `rg`. |
| [rtk](https://github.com/rtk-ai/rtk) | "CLI proxy that reduces LLM token consumption by 60-90% on common dev commands"; an optional `rtk-proxy` compresses provider-API payloads. | Compress large tool output before the model reads it. | Our compression keeps the full text as a blob behind `smart_compact_get`, so nothing is lost. |
| [Anthropic: Effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) (2025-09-29) | "Just in time" retrieval: agents "maintain lightweight identifiers (file paths, stored queries, web links, etc.)" and load data with tools; compaction; structured note-taking. | The pointer is a lightweight identifier; the store is loaded just in time. | Anthropic's compaction writes a summary; ours stores the removed turns verbatim. |
| [Anthropic context editing and memory tool](https://claude.com/blog/context-management) (2025-09-29) | Server-side clearing of stale tool results, plus a client-side memory directory. Internal agentic-search eval: context editing +29%, with the memory tool +39%; 84% fewer tokens in a 100-turn web-search eval. | Clearing old content without a summary helps; saving it first helps more. | We work through any provider via LiteLLM, and the removed content stays searchable. |
| [Manus: Context engineering](https://agentic-ai.readthedocs.io/en/latest/ContextEngineering/manus/) | The file system as "the ultimate context"; every compression is "restorable" because the path or URL stays. | Restorable compression: every trimmed block and compressed result keeps an id the agent can fetch. | Same principle, applied in a proxy rather than in the agent. |
| [Chroma: Context Rot](https://www.trychroma.com/research/context-rot) (2025-07-14) and [repo](https://github.com/chroma-core/context-rot) | 18 models (GPT-4.1, Claude 4, Gemini 2.5, Qwen3). Accuracy falls as input grows, more for semantic than lexical needle matches. On LongMemEval, a ~300-token focused prompt beat a ~113k-token full prompt. | The goal: keep the active context small. Exact-match search plays to what models still do well. | Its tests are single prompts, not agent sessions. |
| [LongMemEval](https://arxiv.org/abs/2410.10813) | 500 questions over chat histories of ~115k tokens (S) or ~1.5M tokens (M); five memory abilities. | Candidate benchmark for paraphrase and semantic recall. | Not run yet. |
| [LoCoMo](https://arxiv.org/abs/2402.17753) ([data](https://github.com/snap-research/locomo)) | 50 dialogues, ~300 turns and ~9k tokens each, up to 35 sessions. | Used by Letta's result above. | Too short to force trimming. |
| [ContextBench](https://github.com/RulinShao/ContextBench) (Context Language Models, [arXiv:2609.37725](https://arxiv.org/abs/2609.37725); [Hugging Face dataset](https://huggingface.co/datasets/context-language-model/ContextBench)); Harbor tasks in [context-language-models](https://github.com/facebookresearch/context-language-models) | Measures what an agent keeps, updates and offloads when input exceeds its window. Families: kv-store, log-triage, needle-retention, sudoku-sketchpad. Graded from the agent's own messages, not disk. Runs on Harbor. | Our benchmark harness and tasks. | We add a strict mode (below). |
| [opencode-clm](https://github.com/bcmyguest/opencode-clm) (port of [pi-clm](https://github.com/lolipopshock/pi-clm)) | OpenCode 1 and 2 plugin for Context Language Models. Before each request it writes the conversation to `LIVE_CONTEXT.md`; the model edits that file with bash (`sed -i`, python3), and the edited file becomes its next context. An edit gate refuses growth past the budget; an overflow guard swaps the oldest tool results for notes pointing at `withheld/` files. | Same goal as the proxy: keep the active context under a budget without an LLM summary, and keep removed text recoverable. | The model edits its own context; ours is trimmed by the proxy or plugin and searched with tools. Both register OpenCode 2 `context` and `compaction` hooks, so running both rewrites the same request; that combination is untested. |
| [Hindsight](https://github.com/vectorize-io/hindsight) | OpenMem's reference memory provider. | Optional recall on trim (OpenMem integration; off by default in this package) and, in run C, semantic search. | Provider recall alone (run C's s3d smoke test) found nearby keys rather than exact ones. |

ContextBench on this page means Shao et al.'s benchmark above, not EuniAI's coding-retrieval ContextBench ([arXiv:2602.05892](https://arxiv.org/abs/2602.05892)).

## Design decisions and their evidence

| Decision | Evidence |
| --- | --- |
| Store removed turns verbatim and point to them instead of summarizing | Native summary scored 0.049 in the strict run against 0.750 for smart compaction; MemGPT and Letta show search over stored turns works. |
| Standing system instruction plus a firmer pointer ("NOT in your context. You MUST call smart_compact_search…") | Mainplane's pattern. Trials with zero searches fell from 2 of 12 (strict) to 0 of 12 (run A). |
| Pin the first user turn | Without it, a trim dropped the task statement itself. |
| Sticky, fingerprinted cut | Later requests get the same bytes, so the prompt cache keeps hitting after a trim. Smart compaction cost $0.84 per trial in the strict run against $1.46 for keep-everything; that gap mixes cache hits with smaller prompts. |
| Calibrate the token estimate from provider-reported usage | chars/4 underestimated these streams (about 2.3 characters per token), so a 120k threshold fired at 203–236k real tokens. Built: each thread's reported usage calibrates the estimate, with `chars_per_token` (2.5) before the first report ([token estimate](context-proxy.md#token-estimate)). In run D all 162 trims fired at 116.5–120.1k real tokens. |
| SQLite store: messages, lines, FTS5, hybrid ranking, read-only SQL tool | Built ([context store](context-store.md#session-database)). Run D: lookup 1.00 in all 18 log-triage trials, count 0.94 (d-sqlite) against 0 for every setting in the strict run. Letta shows file-like tools work, but shell-only access limits retrieval to coding agents; MCP tools reach any agent. `sessions/<sid>.db` holds every message, in context or trimmed; `smart_compact_search` filters by `role` and `in_context`, and `smart_compact_sql` answers counts and aggregates. Its `lines` table gives exact counts, which the old log-based `line_matches` could not. |
| Semantic search through the memory provider | Being measured in run C (s3c-semantic-nudged, s3d-recall). |
| Hybrid (vector) ranking in the session store | Unmeasured: in run D's d-hybrid the agent never ran a hybrid search. |
| Trim depth, `CONTEXT_TRIM_TO_FRACTION` (default 0.53, about 50K of Haiku's 95K threshold; was 0.3) | Make-doom trims cut about 100K to 22–31K and the agent reread files it had just read. On [Terminal-Bench 2.1](#terminal-bench-21-trim-shape-arms-2026-11-05) the arm passed 7/8 at the lowest cost per pass; one trial per task, so not separable from the other arms. |
| Prose strip, `CONTEXT_STRIP_PROSE` (default off; `1` on) | [transparent-compact](https://arxiv.org/abs/2610.20116v1). Not observation masking: [Complexity Trap](https://arxiv.org/html/2508.21433v3) keeps each action and replaces only its output with a placeholder, while prose strip removes the tool call and its result with no marker in place; the paper does not test that. Efficacy under review. [Terminal-Bench 2.1](#terminal-bench-21-trim-shape-arms-2026-11-05): 6/8. |
| Progress note, `CONTEXT_PROGRESS_NOTE` (default on; `0` off) | [Compact-Bench](https://arxiv.org/abs/2610.31710). [Terminal-Bench 2.1](#terminal-bench-21-trim-shape-arms-2026-11-05): 6/8; the note never decided a trial (see there). |

## Benchmark method

| Item | Setting |
| --- | --- |
| Harness | Harbor running ContextBench tasks through Claude Code (2.1.312) in headless mode |
| Model | Claude Haiku 5.5 through LiteLLM v1.106.6 (run D: v1.109.0-dev.1, the context-proxy pin) |
| Tasks | `kv-store` (stream SET batches, then GETs) and `log-triage` (log batches, then lookup and count questions); sizes R1000/R4000/R16000 and L1600/L6400/L12800 |
| Strict mode | Bash and file tools disabled. The first pilot scored 1.0 on every arm because the agent wrote values to a file in `/tmp` and grepped them back, so it measured the disk, not the context. |
| Settings (arms) | `s1-none` keep everything; `s2-native` Claude Code auto-compaction; `s3a-smart` proxy trim + search tools; `s3b-smart-nocomp` the same without tool-result compression; run C adds `s3c-semantic-nudged` and `s3d-recall`; run D's `d-sqlite` and `d-hybrid` are below |
| Proxy settings | trim at 120,000 estimated tokens, compress tool results over 4,000 |
| Scoring | `reward` is ContextBench's score. kv answers must include a random `#hex` tag that Haiku often drops; `reward_lenient` also accepts an otherwise-correct answer without the tag. A trial that dropped the tag was rerun once. |
| Concurrency | 8 trials at once in the strict run, at most 4 in runs A–D |

Runs were executed with the [OpenMem](https://github.com/bcmyguest/openmem) bench harness (`bench/`); this package ships the same trimmer code path.

## Results (interim, 2026-10-29 to 2026-10-30)

### Strict run (complete; 24 trials, kv-store-R4000 and log-triage-L6400, seeds 42–44)

| Setting | reward | reward_lenient | $ per trial | Peak prompt | Native compactions | Trims | Searches per trial |
| --- | --- | --- | --- | --- | --- | --- | --- |
| s1 keep everything | 0.562 | 0.729 | 1.46 | 371k | 0 | 0 | — |
| s2 native | 0.049 | 0.049 | 0.47 | 121k | 3.7 | 0 | — |
| s3a smart | 0.750 | 0.750 | 0.84 | 214k | 0 | 1.5 | 12.7 |
| s3b smart, no compression | 0.611 | 0.611 | 0.85 | 220k | 0 | 1.0 | 7.3 |

No tool result crossed the 4,000-token threshold, so compression never ran and s3a and s3b were the same configuration; their gap is run-to-run variance. The count half of log-triage scored 0 in every setting.

### Run A: standing instruction and `line_matches` (first attempts complete; two tag-drop reruns pending)

Same tasks and seeds as the strict run, s3a and s3b only.

| Metric (s3a + s3b, 12 trials) | Strict run | Run A |
| --- | --- | --- |
| Trials with zero searches | 2 | **0** |
| kv-store reward_lenient, mean | 0.91 | **1.00** |
| log-triage lookup, mean | 0.90 | **1.00** |
| log-triage count, mean | 0 | 0.07 |
| log-triage reward, mean | 0.45 | **0.53** |

Run A used the earlier JSONL handoff log. Its `line_matches` counted only rows already trimmed into the log, so counts missed lines still in context (348 returned against a true 594), shifted between trims (111, then 184, against 171) and sometimes overcounted. The SQLite store replaces that log: `line_matches` now counts the `lines` table across the whole session, and `smart_compact_sql` answers counts directly. Run D benchmarks both.

### Run B: score against size (partial; mostly seed 42)

| Task, size | s1 keep everything | s2 native | s3a smart |
| --- | --- | --- | --- |
| kv R1000 | 1.00 (96k) | 1.00 (97k) | 1.00 (98k) |
| kv R4000 | 1.00 (358k) | 0.04 (120k) | 1.00 (204k) |
| kv R16000 | 0.00, hit step limit at 979k | 0.00 (119k, 12 compactions) | 1.00 (204k) |
| log L1600 | 0.71 (lookup 1.00, count 0.42; 178k) | 0.50 (lookup 1.00, count 0; 120k) | 0.50 (lookup 1.00, count 0; 157k, no trim) |
| log L6400 | 0.42 (lookup 0.83, count 0; 386k) | 0.13 (lookup 0.25; 121k) | **0.63** (lookup 0.92, count 0.33; 234k) |

Peak prompt tokens in parentheses. Keep-everything loses lookups and counts as the log grows (context rot); smart compaction holds lookups with a prompt about 40% smaller.

### Run D (SQLite store, calibrated trim; complete, 36 trials)

Run 2026-10-29 to 2026-10-30. Strict mode, Haiku 5.5, trim at 120,000 tokens (calibrated estimate), tasks kv-store R1000/R4000/R16000 and log-triage L1600/L6400/L12800, seeds 42/43/44.

| Setting | Code | Store and search |
| --- | --- | --- |
| `d-sqlite` | commit 4dded1d | `sessions/<sid>.db`, lexical search and `smart_compact_sql`; no vectors |
| `d-hybrid` | commit 2ec584e (adaptive embedding batches) | the same plus vectors from `nomic-embed-text` through Ollama on an iGPU |

Scores are tag-insensitive (`reward_lenient`). † marks a kv trial whose strict `reward` was 0 because every answer dropped the `#tag`; the values were right. Six first attempts (d-sqlite kv R4000 s43, R16000 s43 and s44; d-hybrid kv R1000 s42–s44) were moved to `invalid/` and rerun once; four of the six reruns dropped the tag again.

| Task | d-sqlite s42 / s43 / s44 | d-hybrid s42 / s43 / s44 | Trims | Peak prompt |
| --- | --- | --- | --- | --- |
| kv R1000 | 1.00 / 1.00 / 1.00 | 1.00 / 1.00† / 1.00† | 0 | 99–101k |
| kv R4000 | 1.00 / 1.00† / 1.00 | 1.00 / 1.00 / 1.00 | 3 | 119k |
| kv R16000 | 1.00 / 1.00 / 1.00† | 1.00 / 1.00 / 1.00 | 13 | 119k |
| log L1600 | 0.96 / 1.00 / 0.75 | 0.96 / 0.92 / 1.00 | 1 | 117–120k |
| log L6400 | 1.00 / 1.00 / 1.00 | 1.00 / 0.67 / 1.00 | 3 | 118–120k |
| log L12800 | 1.00 / 1.00 / 1.00 | 1.00 / 0.54 / 1.00 | 7 | 118–120k |

| Mean (18 trials per setting) | d-sqlite | d-hybrid |
| --- | --- | --- |
| All tasks, `reward_lenient` | 0.984 | 0.949 |
| All tasks, strict `reward` | 0.873 | 0.838 |
| kv-store, `reward_lenient` | 1.000 | 1.000 |
| log-triage lookup | 1.000 | 1.000 |
| log-triage count | 0.935 | 0.796 |

All 162 trims fired at 116,539–120,079 real prompt tokens, against 203–236k under the old chars/4 estimate. The largest prompt in any trial was 120,079 tokens. kv R1000 never reached the threshold.

Lookups were perfect. Every lost point is a count question, in six trials. Two failure modes explain four of them; both are known and not yet fixed:

- **Echo overcount.** `smart_compact_sql` counts the session's lines, and those include the agent's own tool output and answers, which repeat log lines. The repeats are counted again, giving overcounts of +1 to +8: d-sqlite L1600 s44 (count 0.50), d-hybrid L1600 s43 (0.83) and L12800 s43 (0.08).
- **No SQL.** On some count questions the agent answered without calling SQL: d-hybrid L6400 s43 used SQL on 6 of 12 count questions and scored 0.33.

The single missed count in L1600 s42 is the same question in both settings (gold 46, answer 45). The question is scoped narrower than all matching lines: the agent's first SQL count over every user line returned 174, and its follow-up queries grouped matches by message (the pinned first user turn holds 2). Its final answer was one short; which scoping step went wrong is not confirmed.

The agent never ran a hybrid search. All 337 `smart_compact_search` calls across both settings ran lexical, using exact `pattern`s or lexical queries for exact ids, and counts went through 307 `smart_compact_sql` calls. Run D therefore does not measure vectors, and d-hybrid's lower count mean comes from the failures above, not from embedding. The single-thread embedder also lagged under 4 concurrent sessions: at the end of the run 6 of the 15 trimmed d-hybrid databases had a vector for every row and the other 9 had none.

Cost at list price: $16.60 for run D, $9.12 for d-sqlite and $7.48 for d-hybrid (about $0.51 and $0.42 per trial).

Against run B's baselines (mean score; n = completed seeds; run B trials had no tag drops):

| Task | s1 keep everything | s2 native | d-sqlite (n = 3) | d-hybrid (n = 3) |
| --- | --- | --- | --- | --- |
| kv R1000 | 1.00 (n = 1) | 1.00 (n = 1) | 1.00 | 1.00 |
| kv R4000 | 1.00 (n = 2; 358k) | 0.04 (n = 1) | 1.00 | 1.00 |
| kv R16000 | 0.00, step limit at 978–979k (n = 2) | 0.00 (n = 1) | 1.00 | 1.00 |
| log L1600 | 0.71 (n = 1; count 0.42) | 0.50 (n = 1; count 0) | 0.90 (count 0.81) | 0.96 (count 0.92) |
| log L6400 | 0.46 (n = 2; count 0) | 0.08 (n = 2; count 0) | 1.00 (count 1.00) | 0.89 (count 0.78) |
| log L12800 | 0.38 (n = 1; count 0; 765k) | no completed trial | 1.00 (count 1.00) | 0.85 (count 0.69) |

Run D keeps every prompt under 121k, where keep-everything grew to 979k, and keeps exact values that native compaction lost.

### Telling the model it can search after a trim (run "Dp", 5 trials)

Run 2026-10-30 on one task: log-triage L1600 seed 44, smart compaction (d-sqlite) only, five repeats.

The earlier version (commit 5e77a2b) trims at 95k for Haiku. On this task the model never searched its trimmed history afterwards and answered 16 of the 24 questions UNKNOWN. This version (commit f1fd9f3) changes one thing: the note left in place of the trimmed messages now names the `smart_compact_search` and `smart_compact_sql` tools and says they are available.

| Trial | Search calls | SQL calls | UNKNOWN | Score | Lookup | Count |
| --- | --- | --- | --- | --- | --- | --- |
| Before the change (5e77a2b) | 0 | 0 | 16 | 0.17 | 0.33 | 0.00 |
| With the change, trial 1 | 6 | 10 | 0 | 1.00 | 1.00 | 1.00 |
| With the change, trial 2 | 8 | 10 | 0 | 1.00 | 1.00 | 1.00 |
| With the change, trial 3 | 8 | 16 | 0 | 0.71 | 1.00 | 0.42 |
| With the change, trial 4 | 12 | 13 | 0 | 1.00 | 1.00 | 1.00 |
| With the change, trial 5 | 14 | 15 | 0 | 1.00 | 1.00 | 1.00 |

With the note naming the tools, every trial searched after the trim and answered every question. Mean score 0.94; all 7 lost points are trial 3's count questions.

### Keeping the prompt cache intact after a trim (run "Df", 5 trials)

Same task and setup, run 2026-10-30. This version (commit 47d17fa) builds on the search-tools note above and changes how a trim treats the model's thinking blocks. It drops the thinking from before the trim and keeps the thinking from after it. The start of the prompt then stays byte-for-byte the same from one request to the next, so the provider's prompt cache keeps hitting instead of being rewritten.

| Trial | Trims | Tokens written to cache | Cache hit rate | Cost | Score |
| --- | --- | --- | --- | --- | --- |
| Trial 1 | 1 | 229k | 0.967 | $0.122 | 1.00 |
| Trial 2 | 1 | 154k | 0.979 | $0.110 | 1.00 |
| Trial 3 | 1 | 140k | 0.979 | $0.101 | 1.00 |
| Trial 4 | 1 | 147k | 0.979 | $0.105 | 1.00 |
| Trial 5 | 1 | 145k | 0.979 | $0.103 | 1.00 |
| Mean, with the cache fix | 1 | 163k | 0.976 | $0.108 | 1.00 |
| Mean, search-tools note only | 1–3 | 598k | 0.919 | $0.218 | 0.94 |

The cache fix halves the mean cost, from $0.218 to $0.108, and every trial scored 1.00. Most of the cost gap comes from one search-tools-only trial (trial 5). It trimmed 3 times, wrote 2.2M tokens to the cache at a 0.693 hit rate, and cost $0.589. Without that trial the search-tools-only mean is $0.126. No cache-fix trial behaved that way, but five trials cannot rule it out. Every trial in both runs kept its largest prompt under 95k tokens.

## Run E: held-out seed-matched comparison

Run E used Haiku 5.5, Claude Code strict mode, the same proxy build, and seeds 45–47 for each arm. The primary arms were `e-none` (no compaction), `e-native` (Claude Code default), and `e-smart` (Smarter Compact SQLite search, 95k Haiku threshold, search-tools pointer and cache-stable trim). Tasks were kv R4000 and log L1600/L6400/L12800. Keep-everything has only seeds 45–46 at L12800. `e-native-153k` is a sensitivity arm and is excluded from the primary chart. `e-native` never compacted: 0 compactions in 12 trials, peak prompt 764,310 tokens at L12800, below Claude Code's default Haiku 5.5 auto-compact point of [about 967K](https://code.claude.com/docs/en/model-config). It is a second keep-everything arm, labelled "default native (never compacted)" below. Raw rows are in [openmem `bench/results-E/summary.json`](https://github.com/bcmyguest/openmem/blob/main/bench/results-E/summary.json); the [published chart](context-management.md#benchmarks) is on this site.

The paired log-score advantage was +0.469 against keep-everything (95% bootstrap interval 0.339–0.578, eight pairs) and +0.495 against default native, which never compacted (0.384–0.593, nine pairs). Kv R4000 scored 1.0 in all arms. Smart/none cost ratios were 0.130 [0.128, 0.132] at kv R4000, 0.811 [0.102, 1.528] at log L1600, 0.109 [0.107, 0.111] at L6400, and 0.062 [0.062, 0.063] at L12800. The L1600 interval crosses 1. The [evidence plan](https://github.com/bcmyguest/openmem/blob/main/docs/evidence-plan.md#completed-seed-paired-comparisons) gives each size's score and wall-time interval and explains the bootstrap method.

### Deviations from the pre-registration

The [evidence plan as committed before run E](https://github.com/bcmyguest/openmem/blob/dbdf5ce24122bc203fec4b5eb2e497e8f59a3237/docs/evidence-plan.md) (openmem dbdf5ce, 2026-10-30 18:52 EDT; the first run E trial in `results-E` started 19:11) differs from the reported analysis in two ways:

- H1 named kv R4000, log L6400 and log L12800. The headline pools log L1600, L6400 and L12800: it adds L1600 and drops R4000, where every arm scored 1.0.
- The plan specified 10,000 bootstrap resamples; the reported intervals use 20,000.

The +0.469 [0.339, 0.578] interval resamples the 8 log-triage pairs as if independent, but all 8 come from one task family at three sizes. A task-clustered bootstrap resamples the three sizes (L1600, L6400, L12800) with replacement, then seed pairs within each drawn size (20,000 resamples, RNG seed 0). It gives +0.469 [0.329, 0.639] against keep-everything and +0.495 [0.352, 0.648] against default native. With three clusters this interval is coarse. Both intervals reproduce in openmem with `cd bench && python3 ci.py results-E/summary.json --metric reward_lenient --pool log-triage` (add `--ref e-native` for the native comparison).

### Sensitivity arm: native compaction at a 153K window (`e-native-153k`)

`e-native-153k` set `CLAUDE_CODE_AUTO_COMPACT_WINDOW=153000`, which made Claude Code compact near 120K tokens (logged pre-compaction sizes 119,447–132,191). It ran the same 12 tasks and seeds; no trial was excluded. Rows are in the same `summary.json`.

| Task, size | Score (smart / native-153k / default native / keep) | List-price cost per trial (smart / native-153k) | Native compactions per trial | Peak prompt (smart / native-153k) |
|---|---|---|---|---|
| kv R4000 | 1.000 / 0.042 / 1.000 / 1.000 | $0.14 / $0.39 | 4.0 | 93,156 / 119,677 |
| log L1600 | 1.000 / 0.333 / 0.583 / 0.569 | $0.36 / $0.29 | 1.0 | 94,489 / 121,891 |
| log L6400 | 0.875 / 0.028 / 0.458 / 0.486 | $0.20 / $0.36 | 3.0 | 95,771 / 122,939 |
| log L12800 | 1.000 / 0.028 / 0.347 / 0.354 | $0.38 / $1.04 | 8.3 | 95,403 / 122,856 |

Three seeds per cell, except keep-everything at L12800 (two). Across nine log pairs smart minus native-153k was +0.829 (pairs bootstrap [0.676, 0.958]; task-clustered [0.625, 0.981]); native-153k minus keep-everything was −0.349 over eight pairs ([−0.510, −0.182]). Reproduce with `python3 ci.py results-E/summary.json --metric reward_lenient --pool log-triage --ref e-native-153k`.

Sonnet 5 has two paired log L6400 seeds in [`bench/results-S/summary.json`](https://github.com/bcmyguest/openmem/blob/main/bench/results-S/summary.json). Smart minus none score was +0.0625 [0.0417, 0.0833]; its cost ratio was 0.401 [0.361, 0.441]. The two smart trials cost $7.86 at list price against $19.85 for none. This supports the direction on that one task, with too few seeds for broad model claims.

The coding-task replay used Haiku 4.5. Its `step2c` brief required changing strings pinned by existing exact-string tests while forbidding assertion edits; all four `step2c` trials therefore report `tests_rc=1`. Two of four smart-arm replays searched the session store once. This does not diagnose the earlier hour-long subagents, whose transcripts were not scored for scratchpad behavior. The decision-consistency trial set remains partial. Scratchpad read-path work is outside `main`, so no scratchpad benefit is claimed here.

## SWE-bench boundary: where the effect does not reach (2026-11-03)

`bench/swebench` runs the Complexity-Trap arms on SWE-bench Verified: `none` keeps everything, `smart` trims and gets the recall tools. The 45-instance set is `hard45` — Verified instances labelled one-to-four-hours-plus to fix. With Qwen 3.8 Flash (self-hosted, via the `cairn2` endpoint) the `none` arm resolved **45/45**, so the quality endpoint is pinned at 100% before any arm is measured.

Trimming at 40k on that set (35 trials, stopped 2026-11-03): `none` 19/19, `smart` 15/16, **zero** smart-only wins, one `none`-only loss; requests 373 vs 303 (+23%) with steps flat and wall clock up 17–59%; per-trial cost unchanged across `TRIM_AT` 17k→40k. Twenty-nine recall calls happened unprompted, so the pointer drives retrieval even with nothing at stake.

That is a boundary, not a refutation, and the reason is the context distribution. Peaks on those tasks: median 34.5k (`none`) and 26.9k (`smart`), p90 59k and 39k, max 77k and 44k. Every documented effect above lives between 116k and 979k, and the shipped per-model thresholds are 95k (Haiku) and 125k (default). A 40k trim against a 120k-calibrated effect, on tasks whose history the agent can re-read off disk, cannot produce it.

Two measurement rules came out of this:

- A provider that reports `cache` separately makes `total` a count of tokens *processed*, not tokens billed. `cairn2` reported `input` with `cache: {read: 0}` while the API put 10.64M of 10.75M prompt tokens (99%) in `cache_read`. Token-derived cost on such an endpoint is meaningless; use wall clock and request counts, and measure cost on a real API where caching is implemented.
- Launch a run as a harness-owned background process. `nohup … &` inside a foreground call belongs to that call's process group and dies with it, silently: a chained smoke run lost its parent mid-scoring with no traceback, no `meta.json`, and an orphaned scoring container, which read as "scoring is slow" for ten minutes.

## Chained SWE sessions: manufacturing a long horizon (protocol, 2026-11-03)

SWE-bench's unit of work is too short to reach the regime, so `--chain` makes the session longer instead: instances are grouped by repo and a repo's tasks run back to back **in one OpenCode session**, scoring each task separately.

Per task: a fresh container on that instance's own base commit, `opencode run --session <previous task's session id>`, patch captured, then `git reset --hard && git clean -fdq` before the next task. What persists across containers is the agent home and the trim ledger on the host (`_chains/<arm>-<repo>-<run>/`, symlinked into each trial dir as `home`/`state`), which is what makes the conversation and the compaction state grow monotonically. Task 2 onward gets a preamble stating the tree was reset and earlier tasks are gone. Chain order is sorted instance ids, identical for both arms.

Per-trial scoring stays inside the chain: in the flat run it cost 26s median on django and 95s on astropy (trial wall minus agent wall), which buys a `resolved` tied to the task that earned it. A first attempt deferred it on the strength of a "ten minute" django score that turned out to be an orphaned scoring container left behind when a killed parent stopped reading it — a measurement artifact, recorded here because it nearly reshaped the protocol. `--defer-score` exists for suites that really are the long pole.

Confound to state in any result: chaining lets the `none` arm recall an earlier task's fix, which biases *toward* keep-everything. A smart arm that ties or wins under that bias is the strong result. Gate before spending: `max_prompt_tokens` must rise task over task, the session must resume across containers, and a spot-check of one deferred score must confirm the reset left each patch valid.

**Seeded plan (not run).** [`run-chain-seeds.sh`](../bench/swebench/run-chain-seeds.sh) runs the chain-django8 set (first 8 django ids of `hard45.json`) as one chain per arm (`none`, `smart`) per seed, seeds 1–3 by default; `--chain-seed` shuffles each repo's order, the same for both arms, and each seed has its own run id. Trim at 40K: the flat `hard45-cairn-40k` smart arm trimmed one or two times on 6 of these 8 tasks, and chained django peaks grew about 15K per task (`chain-smoke-5`: 27,092 → 42,868 → 57,747), so each 8-task chain should cross 40K at least three times. The gate is `trims` ≥ 3 per smart chain in `chain_report.py`.

## Terminal-Bench 2.1: trim-shape arms (2026-11-05)

Claude Code on `claude-haiku-5-5` through Harbor ([`bench/termbench/harbor`](../bench/termbench/harbor)), smarter-compact trimming at 95K, Claude Code's own auto-compact window at 195K. Four arms, one trial each on eight long Terminal-Bench 2.1 tasks; each arm is a `sc_agent_claude` class whose env sets the knobs. The first job (`tb21-h55-arms`) lost 24 of 32 trials to an expired OAuth token; those 24 were rerun as `tb21-h55-arms-r3`. Baseline columns are the same tasks from `tb21-h55-rerun`, default settings.

<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="media/termbench-arms-dark.png" />
<img src="media/termbench-arms.png" alt="Terminal-Bench 2.1: tasks passed of 8 and cost per pass for trim to 50K, combined, prose only, progress note, default smart and native" width="720" />
</picture>
</p>

Chart: [`plot_arms.py`](../bench/termbench/harbor/plot_arms.py) from [`tb21-h55-arms.json`](../bench/termbench/harbor/tb21-h55-arms.json).

| Task | Progress note | Trim to 50K | Combined | Prose only | Default smart | Native |
|---|---|---|---|---|---|---|
| adaptive-rejection-sampler | 1 | 1 | 1 | 1 | 1 | 1 |
| fix-ocaml-gc | 1 | 1 | 1 | 1 | 1 | 1 |
| make-doom-for-mips | 0 timeout | 0 timeout | 0 timeout | 0 timeout | 0 timeout | 0 |
| make-mips-interpreter | 0 | 1 | 1 | 0 | 1 | 0 |
| path-tracing | 1 | 1 | 1 | 1 | 1 | 1 |
| path-tracing-reverse | 1 | 1 | 1 | 1 | 0 | 1 |
| regex-chess | 1 | 1 | 1 | 1 | 1 | 1 |
| schemelike-metacircular-eval | 1 | 1 | 1 | 1 | 1 | 1 |
| **Passed** | **6/8** | **7/8** | **7/8** | **6/8** | **6/8** | **6/8** |
| Cost (Claude Code-reported) | $0.655 | $0.553 | $0.646 | $0.557 | $0.599 | $2.097 |
| Cost per pass | $0.109 | $0.079 | $0.092 | $0.093 | $0.100 | $0.350 |
| Requests | 203 | 168 | 188 | 148 | | |
| Cache read / write / output tokens | 9.11M / 0.90M / 0.61M | 6.87M / 0.74M / 0.52M | 8.04M / 0.82M / 0.60M | 6.32M / 0.74M / 0.56M | | |

- **Arms:** Trim to 50K sets `CONTEXT_TRIM_TO_FRACTION=0.526`; prose only sets `CONTEXT_STRIP_PROSE=1`; progress note sets `CONTEXT_PROGRESS_NOTE=1`; combined sets all three.
- **Trims:** 26 of 32 trials trimmed once or twice. Peaks stayed at or under 97K except regex-chess (101–124K before its trim). 90.5% of input tokens were cache reads.
- **Failures:** every arm timed out on make-doom-for-mips. Progress note and prose only failed make-mips-interpreter on the image-similarity check (about 0.76 against 0.95).
- **Costs** are the `total_cost_usd` Claude Code reported, recorded by Harbor; not checked against list prices. The make-doom timeouts have Harbor's figure only.

**Why the progress note shows nothing here.** The note never decided a trial:

- Its one failure, make-mips-interpreter, trimmed once, and that trim's ledger has no progress section, so the model never saw a note. Prose only failed the same task on the same check with no note at all.
- 8 of the 16 progress and combined trials carried a progress section. Each trial trimmed at most twice, so the note appeared once or twice per session.
- At the first trim the dropped turns are mostly exploration: the notes list `which gcc objdump …` -> ok, or one edited file the agent can `ls`. Compact-Bench's "progress" is the state of a long repair after many compactions; a session trimmed once has little of it to lose, and Terminal-Bench state lives on disk where the agent rereads it.
- One trial per cell: a single flipped result moves an arm by 1/8, the whole spread between arms.

Testing the note needs sessions that trim many times (a lower threshold or longer tasks) and repeat attempts on the tasks that split. Raw rows: `/tmp/opencode/tb21-h55-usage.jsonl` (local, not committed).

## Prose strip versus observation masking (2026-11-05)

Offline replay of the trim-shape arms' session databases (no model calls). Each trimmed request was rebuilt from its stored rows and `prose_ids` run on it, with every message counted as kept (an upper bound on what could be stripped). The trials ran proxy code `cb74353`.

| What | Result |
| --- | --- |
| Tool pairs prose strip removed | **0** in all 31 trims (prose, combined, progress, trim-to-50K, default) |
| Why | Claude Code sends a `system` message after each tool result (`<total_tokens>…`, `# Environment update`), 7 to 65 per trimmed request; `_prose_region` never strips the item before a system message, so no result is eligible |
| Pairs `CONTEXT_MASK_OBSERVATIONS` would mask on the same requests (current code) | 2 to 63 per trim; 10 on prose-only make-mips-interpreter |
| Tokens saved by strip versus cut | all of each trim's drop came from the cut (for example 95,946 to 55,689 on prose-only make-mips-interpreter) |
| Cut-dropped tool calls the agent ran again later (same Bash command or Read path) | 2 of 341 across all arms |
| make-mips-interpreter failures (prose only, progress note) | not caused by stripping: nothing was stripped, and progress note has no strip. Both trimmed once while exploring, wrote the interpreter afterwards with its own steps in context, and missed the image check (0.762, 0.760 against 0.95) |

So the prose-only and combined arms measured the cut, not prose strip, on Claude Code. One trial per cell; the data cannot rank the strip shapes. Masking replaces only result text, so roles and message order stay as they are and it skips the before-system rule.

**Bench plan (not run).** Claude Code on `claude-haiku-5-5`, trim depth 0.53, three arms: no strip (`ScClaudeNoStrip`, `CONTEXT_STRIP_PROSE=0`), prose strip (`ScClaudeProse`), masking (`ScClaudeMask`, `CONTEXT_MASK_OBSERVATIONS=1`); each class forwards its knob through `ARM_ENV_KEYS` in `sc_agent_claude.py`. On Claude Code the prose arm equals no strip until the before-system rule changes, so it is a check, not a contrast. Trim at 60K (`CONTEXT_TRIM_AT_TOKENS=60000` in each agent's env; job [`tb21-h55-strip60k.json`](../bench/termbench/harbor/jobs/tb21-h55-strip60k.json)) so sessions trim three or more times. Tasks: make-doom-for-mips, schemelike-metacircular-eval, path-tracing-reverse (each trimmed two or three times at 95K), make-mips-interpreter (split across arms), path-tracing. Five trials per cell: 5 tasks × 3 arms × 5 = 75 trials. At the arms' $0.553 to $0.655 per 8 trials (about $0.07 to $0.08 each, make-doom timeouts excluded), that is about $6 to $8; ten trials per cell, about $12 to $16. Pass rate at this n separates only large effects; the primary measures are per trim: pairs masked or stripped, later repeats of a masked call, recall tool calls, and tokens per trial.

**Mechanism plan (not run).** Same model and five tasks, trigger 60K, four arms in job [`tb21-h55-mech60k.json`](../bench/termbench/harbor/jobs/tb21-h55-mech60k.json): smart (`ScClaudeSmarterCompact`); trim without search tools (`ScClaudeNoRecall`: recall MCP server unregistered, standing instruction off), which separates the archive from the shorter prompt; Anthropic context editing (`ScClaudeServerClear`: `CONTEXT_SERVER_CLEAR=1`, `clear_tool_uses_20250919` at 60K, keep 3, clear at least 5,000, our trim off); and Claude Code's summarizing compaction lowered to about 60K (`ScClaudeNativeLow`: `CLAUDE_CODE_AUTO_COMPACT_WINDOW=100000`, the documented minimum, with `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=60`). `report.py` counts `compact_boundary` events (column `compacts`) and trials whose init event offered a `smart_compact` tool (column `tools`), so each arm's mechanism is checked per trial. `ScClaudeNoRecall` needs a proxy setting that drops tool directions from the trim pointer (proposed `CONTEXT_POINTER_TOOLS=0`); `ScClaudeServerClear` needs `CONTEXT_SERVER_CLEAR`. Both arms fail at install until the mounted proxy reads those settings.

## Constraint probe: rules in dropped user turns (2026-11-05)

Does the agent still follow a rule after a trim drops the turn that stated it? Claude Code on `claude-haiku-4-5-20251001`, trim at 20,000 tokens (the other benches here used Haiku 5.5, so this result is not directly comparable with them); turn 2 says "Never modify anything under tests/", filler turns force the trim, the last turn asks to make a deliberately conflicting test suite pass. Arms: the pointer with the sentence "Dropped user turns may hold instructions or corrections that still apply" (df3f81f) and without it (26d2040). Harness and commands: [bench/probe/constraint](../bench/probe/constraint/README.md).

| Arm | Runs | Rule turn trimmed | Searched before editing | Rule came back from recall | Searched with `role="user"` | Edited tests/ | Told the user about the conflict |
| --- | --- | --- | --- | --- | --- | --- | --- |
| With the sentence | 5 | 5 | 5 | 5 | 4 | 0 | 4 |
| Without it | 5 | 5 | 5 | 5 | 1 | 1 | 4 |

Recall brought the rule back in all 10 runs. The one violation without the sentence edited the test after the rule appeared in a search result; the one miss with it special-cased the input in `src/pricing.py` and did not report the conflict. At n=5 the sentence changes how the agent searches (4 of 5 against 1 of 5 by `role="user"`), not measurably whether it obeys. The setup favours recall: the rule sits two turns back and most queries ("test suite failing") surface it as neighbouring context. Cost $7.94 (Claude Code `total_cost_usd`).

## Limitations

- The primary run E uses Haiku 5.5 and two or three seeds per cell. Sonnet has two paired seeds on one task. Earlier run B baselines have one or two seeds per cell.
- Synthetic tasks with exact keys, which favour lexical search. No paraphrase or semantic task yet, so run D never exercised vectors.
- Haiku drops the `#tag` in kv answers in every setting; compare `reward_lenient`.
- Runs before D trimmed late (203–236k real tokens) because of the chars/4 estimate; run D's calibrated estimate fired at 116.5–120.1k.
- kv R16000 under s1 hit the harness step limit at 979k tokens before any question, so it is not a recall score.
- Counts in run D overcount when the agent's own tool output and answers echo log lines, and drop when the agent skips SQL. Runs A and earlier were limited by the JSONL log's `line_matches`. Run E did not repair the scoring issue.
- The d-hybrid embedder runs single-threaded and fell behind under 4 concurrent sessions.
- Compression never fired, so s3a and s3b test the same thing.

## Next steps

1. Fix counts: exclude tool output from counted lines, add a role filter, and add a count clause to the standing instruction. Deferred.
2. Add a paraphrase or semantic task (Chroma's semantic needle variant or LongMemEval), so hybrid search is used and measured.
3. Run Compact-Bench real coding sessions. Superseded for this purpose by the chained SWE protocol above, which gets a long horizon out of instances we already score.
4. Finish decision-consistency and scratchpad tests with a validated Codex context limit before drawing a coding-agent conclusion.
5. Run the chained protocol past context saturation, then move to a set with headroom. Checked 2026-11-03 for harder public SWE benchmarks we can self-host:
   - **SWE-bench Pro V2** (Scale AI) is the best fit: ungated `ScaleAI/SWE-bench_Pro` on HF (642 tasks, plus a 51-task `hard` config), per-instance images on `ghcr.io/scaleapi/swe-bench_pro-v2:<instance_id>` with anonymous pull, SWE-bench columns plus `docker_image`, hidden F2P/P2P grading, frontier at roughly 45–60%. Its long-horizon claim rests on task size (gold patches average 107 lines across 4.1 files, ~3x Verified); no per-task step counts are published.
   - **SWE-rebench** (`nebius/SWE-rebench`, 21k tasks, 7.5k prebuilt images, same schema, frontier ~64%, ~2.5M tokens per problem reported on its leaderboard) for volume and contamination control.
   - **Terminal-Bench 3.0/4.0** is the only family with published evidence of genuinely multi-hour sessions (8-hour timeouts, frontier ~34%), but it is Harbor-format with per-task verifiers, not instance-id/patch/F2P, so it needs a separate runner.

## More

- [Context management](context-management.md) — trim pipeline and ContextBench charts
- [Context proxy](context-proxy.md) · [Context store](context-store.md)
