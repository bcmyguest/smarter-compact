# Benchmarks

Three settings on the same held-out [ContextBench](https://github.com/RulinShao/ContextBench) (Context Language Models, [arXiv:2609.37725](https://arxiv.org/abs/2609.37725); [Hugging Face dataset](https://huggingface.co/datasets/context-language-model/ContextBench)) seeds through Claude Code in headless strict mode (no Bash or file tools): keep everything, Claude Code's default native compaction, and **Smarter Compact** smart compaction (same trimmer as this repo). The model is Claude Haiku 5.5. `kv-store` asks for earlier values; `log-triage` asks lookup and count questions. Each cell has two or three seeds. The default native arm recorded zero compactions in these tasks. Deeper method and limits: [Context research](context-research.md) and the [evidence plan](https://github.com/bcmyguest/openmem/blob/main/docs/evidence-plan.md#completed-seed-paired-comparisons) (bootstrap intervals; bench harness lives in OpenMem).

<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="media/contextbench-quality-dark.png" />
<img src="media/contextbench-quality.png" alt="ContextBench answer quality by task size for smart compaction, native compaction, and keep everything" width="720" />
</picture>
</p>

Source: [ContextBench](https://github.com/RulinShao/ContextBench) synthetic tasks, Claude Haiku 5.5. Score is reward_lenient (tag-insensitive), the mean over the completed seeds of each cell. † a seed dropped the `#tag` from its kv answers and scored 0 under the strict reward; the values were right. ‡ the harness step limit stopped the trial before any question, so the 0 is not a recall score.

| Task, size | Smarter Compact | Native compaction (default) | Keep everything |
| --- | --- | --- | --- |
| kv-store R4000 | 1.00 †, n = 3 | 1.00 †, n = 3 | 1.00, n = 3 |
| log-triage L1600 | 1.00 (lookup 1.00, count 1.00), n = 3 | 0.58 (lookup 1.00, count 0.17), n = 3 | 0.57 (lookup 0.97, count 0.17), n = 3 |
| log-triage L6400 | 0.88 (lookup 1.00, count 0.75), n = 3 | 0.46 (lookup 0.92, count 0.00), n = 3 | 0.49 (lookup 0.97, count 0.00), n = 3 |
| log-triage L12800 | 1.00 (lookup 1.00, count 1.00), n = 3 | 0.35 (lookup 0.69, count 0.00), n = 3 | 0.35 (lookup 0.71, count 0.00), n = 2 |

# Time

This comparison recorded no native compactions: peak prompts reached 764,310 tokens, below Claude Code's ~967K default auto-compact point for Haiku 5.5. Its default native arm is therefore a second keep-everything arm. A sensitivity arm that forced native compaction near 120K scored 0.028–0.333 on log tasks ([results](context-research.md#sensitivity-arm-native-compaction-at-a-153k-window-e-native-153k)). The saved usage log cannot identify which request paid for a new trim, so it does not support a trim-latency estimate.

<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="media/contextbench-time-dark.png" />
<img src="media/contextbench-time.png" alt="Agent wall time per trial by task size" width="720" />
</picture>
</p>

| Setting | Trials | Agent wall time per trial, mean | Requests | Request latency, mean | Prompt-cache hit rate |
| --- | --- | --- | --- | --- | --- |
| Smarter Compact | 12 | 8.8 min | 2103 | 1.83 s | 90% |
| Native compaction (default) | 12 | 8.0 min | 1831 | 2.12 s | 99% |
| Keep everything | 11 | 8.2 min | 1567 | 2.47 s | 99% |

Wall time per trial is longer with smart compaction (527 s vs 479 s for native compaction); smart compaction also makes more requests per trial (175 vs 153), which include store searches.

# Cost and prompt cache

<p align="center">
<img src="media/contextbench-savings.png" alt="Average cost saving 72%: paired cost of Smarter Compact relative to keep everything, kv-store R4000 −87%, log-triage L1600 −19% (not significant), L6400 −89%, L12800 −94%" width="720" />
</p>

Paired cost ratios (Smarter Compact ÷ keep everything, seed-matched) were 0.130 at kv R4000, 0.811 at L1600, 0.109 at L6400 and 0.062 at L12800. Their mean is 0.28: a **72% average saving** across the four sizes. The L1600 interval crosses 1, so that size shows no reliable saving; the [research page](context-research.md) gives each interval.

<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="media/contextbench-cost-dark.png" />
<img src="media/contextbench-cost.png" alt="List-price cost per trial by task size" width="720" />
</picture>
</p>

<p align="center">
<picture>
<source media="(prefers-color-scheme: dark)" srcset="media/contextbench-cache-dark.png" />
<img src="media/contextbench-cache.png" alt="Prompt-cache hit rate by task size" width="720" />
</picture>
</p>

Source: [ContextBench](https://github.com/RulinShao/ContextBench) synthetic tasks. Cost applies Claude Haiku 5.5 list prices to each request's token counts. Cache hit rate is cache reads over all prompt tokens (input, cache reads and cache writes) of a cell's requests. Peak prompt is the largest single request.

| Task, size | List-price cost per trial (smart / native / keep) | Prompt-cache hit rate (smart / native / keep) | Tokens per trial, cache reads in parentheses (smart / native / keep) | Peak prompt (smart / native / keep) |
| --- | --- | --- | --- | --- |
| kv-store R4000 | $0.14 / $1.07 / $1.05 | 90% / 98% / 98% | 4.6M (4.1M) / 16.1M (15.7M) / 15.7M (15.4M) | 93k / 359k / 359k |
| log-triage L1600 | $0.36 / $0.54 / $0.68 | 84% / 98% / 99% | 7.9M (6.5M) / 10.2M (10.0M) / 12.5M (12.3M) | 94k / 218k / 268k |
| log-triage L6400 | $0.20 / $1.90 / $1.87 | 94% / 99% / 99% | 8.9M (8.3M) / 32.6M (32.2M) / 32.3M (31.9M) | 96k / 397k / 388k |
| log-triage L12800 | $0.38 / $6.11 / $6.07 | 92% / 99% / 99% | 14.4M (13.2M) / 109.3M (108.6M) / 108.8M (108.0M) | 95k / 764k / 762k |

# What this shows

Across eight seed-matched log trials, smart compaction's mean score exceeded keep-everything by **0.469** (95% bootstrap interval 0.339–0.578; 0.329–0.639 when the bootstrap resamples the three task sizes; [deviations from the pre-registration](context-research.md#deviations-from-the-pre-registration)). Across nine pairs it exceeded default native, which never compacted in these trials, by **0.495** (0.384–0.593; task-clustered 0.352–0.648). All three arms scored 1.0 on kv R4000. Smart compaction cost less on every paired kv-store R4000 trial and every log-triage L6400 and L12800 trial. The L1600 cost interval includes no saving, and its wall-time interval includes a slowdown over 20%. These small synthetic samples do not establish the same effect for coding sessions.

On Sonnet 5 log L6400, two seed-matched pairs favored smart compaction by 0.0625 score (0.0417–0.0833) and cost 0.401 times keep-everything (0.361–0.441). [Exclusions, price basis, and pending work](https://github.com/bcmyguest/openmem/blob/main/docs/evidence-plan.md#completed-seed-paired-comparisons).

# Compact-Bench (real coding sessions)

[Compact-Bench](https://github.com/cai-jianfeng/compact-bench) Task Preservation track resumes real SWE-bench repair sessions after compaction. Results for Smarter Compact vs full history and native summaries are **pending** (harness adapter lives in the OpenMem repo).

How trimming works: [How it works](context-management.md).
