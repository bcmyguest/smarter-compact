# Lifecycle eviction

Lifecycle eviction (`CONTEXT_EVICTION=1`, off by default) replaces old tool results the agent no longer needs with the same per-result placeholder [clearing](clearing.md) uses, a few at a time, without waiting for the trim threshold. While it is on, the threshold trim does not run; the agent's own compaction (Claude Code's autocompact) stays the guard against overflow. A forced trim (`/smarter-compact:compact`) still runs.

It follows TokenPilot ([arXiv 2606.17016](https://arxiv.org/abs/2606.17016)), whose evictor model runs every B = 3 turns, moves segments from active to completed to evictable, and purges in one pass per batch so the prompt cache breaks at most once per batch. Here deterministic rules replace the model, so eviction costs no model call and gives the same answer for the same history.

## Rules

A segment is one tool call and its result. At each batch boundary, the request whose history first holds a multiple of `CONTEXT_EVICTION_BATCH` assistant steps, the proxy evicts a result when one rule holds:

| Rule | Evicts a result when |
| --- | --- |
| Superseded file | A later call wrote the same file (`Edit`, `Write`, `MultiEdit`, `NotebookEdit`, OpenCode's `edit` / `write`), or read at least as much of it: the whole file (`Read` without `offset` / `limit`, or a plain `cat <path>` in Bash), or the same range |
| Superseded command | The same Bash command, whitespace-normalized, ran again later |
| Unreferenced and old | `CONTEXT_EVICTION_KEEP` or more tool results came after it, and none of its new distinctive tokens appears in later assistant text or tool input. Its distinctive tokens are the paths, identifiers of 6+ characters and numbers of 4+ digits in its text that appear nowhere earlier in the request |

Never evicted: results of the newest batch (the last `CONTEXT_EVICTION_BATCH` assistant steps), the first user turn, and results no longer than their placeholder.

The evicted set only grows, and requests between boundaries re-render it byte for byte. The proxy stores it per conversation thread in `sessions/<sid>.trim.json` (`evict`, `evict_batch`, and the item it was computed on, `evict_at` / `evict_fp`); a history that no longer holds that item starts over. Each evicted result is written to `blobs/<id>.txt` before the request leaves, its row gets `in_context = 0`, and `smart_compact_get(id="<id>")` returns it ([getting a result back](clearing.md#getting-a-result-back)). Each boundary that evicts something adds an `evict` [event](context-store.md#events).

Eviction needs an adapter that can mask results: the proxy's Anthropic route (`/v1/messages`). Requests on the Chat Completions and Responses routes keep the threshold trim. The scratchpad nudge stays off while evicting, because its text announces a trim that eviction never makes; the scratchpad tool stays available. The OpenCode 2 / Pi in-process trim does not evict yet; `proxy/smarter_compact/evict.py` keeps the rules as pure functions over the adapter interface so it can mirror them.

## Settings

| Variable | Default | Effect |
| --- | --- | --- |
| `CONTEXT_EVICTION` | `0` | `1` evicts at batch boundaries instead of trimming at the threshold |
| `CONTEXT_EVICTION_BATCH` | `3` | Assistant steps per batch; a positive integer |
| `CONTEXT_EVICTION_KEEP` | `12` | Later tool results an unreferenced result needs before it goes; a positive integer |

## Offline replay

The rules and K were chosen on stored Terminal-Bench 2.1 Claude Code trajectories (Haiku, runs `tb21-h55-rtk` and `tb21-h55-publish`; 130 trials with 5+ tool results, 4,425 results), replaying each main thread at B = 3. "Evicted" is the share of tool-result characters across every replayed request (each request counts the results it carries). Regret counts evicted results the agent needed again within 10 steps: "re-run" when it re-ran the command or re-read the file while every copy in context was evicted, "reference" when one of the result's distinctive tokens appeared in its later text or tool input.

| Rules | K | Evicted | Results evicted | Re-run regret | Reference regret |
| --- | --- | --- | --- | --- | --- |
| Superseded file | – | 1.8% | 166 | 0.0% | 7.2% |
| Superseded command | – | 0.2% | 82 | 0.0% | 2.4% |
| Settled failure (error, then success of the same command or tool and target) | – | 0.0% | 1 | 0.0% | 0.0% |
| Unreferenced and old | 6 | 29.4% | 2,498 | 0.2% | 6.4% |
| Unreferenced and old | 12 | 20.3% | 1,848 | 0.2% | 3.7% |
| Unreferenced and old | 20 | 12.9% | 1,252 | 0.0% | 1.8% |
| File + command + unreferenced | 6 | 30.6% | 2,571 | 0.2% | 6.7% |
| **File + command + unreferenced** | **12** | **21.6%** | **1,980** | **0.2%** | **4.1%** |
| File + command + unreferenced | 20 | 14.4% | 1,405 | 0.0% | 2.6% |
| Clearing, newest 10 kept, at every request | – | 71.8% | 3,176 | 0.2% | 23.1% |

The unreferenced rule carries the saving; the superseded rules add little in these short sessions but cost nothing. The settled-failure rule evicted one result and none the others miss (a later success of the same command or file is already a supersession), so it is left out. K = 12 keeps 71% of K = 6's saving at 0.6 of its reference regret. Clearing at every request would remove 3.3 times as much, at 5.6 times the reference regret; clearing fires only at the trim threshold, so its row is a ceiling.

Driving the same trajectories through the proxy request by request (12 trials, 200 requests) changed an earlier message in 20 requests, all at batch boundaries.
