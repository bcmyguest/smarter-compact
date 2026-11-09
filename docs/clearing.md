# Tool-result clearing and masking

A trim can shrink the context without deleting turns. It replaces old tool outputs with a short placeholder and keeps every call, so the agent still sees what it did but not what it read. Two settings do this, both on by default. A third shrinks the long inputs of those calls.

| Setting | Default | What a trim replaces |
| --- | --- | --- |
| `CONTEXT_CLEAR_KEEP_RESULTS` | `10` | Every tool result except the 10 newest, across the whole kept context |
| `CONTEXT_MASK_OBSERVATIONS` | `1` | The tool results in the kept region before the latest run of tool calls, except the message right before that run |
| `CONTEXT_CLEAR_INPUTS_OVER_CHARS` | `2000` | Each string over this many characters in the input of a call whose result is cleared, masked or evicted; `0` off |

A replaced result names its own blob:

```text
[smarter-compact: tool output cleared to save context; smart_compact_get(id="<id>") returns it]
```

`<id>` is the first 16 hex characters of the SHA-256 of the result's text (the [blob id](context-store.md#location)), so every later request renders the same bytes and the prompt cache holds. A result is replaced only when its text is longer than its placeholder. Before the request leaves, the proxy (or the OpenCode 2 plugin) writes the original text to `blobs/<id>.txt`; a result already [compressed](context-proxy.md) names the blob its compression wrote.

## Clearing (`CONTEXT_CLEAR_KEEP_RESULTS`)

When a request reaches the trim threshold, the proxy first tries clearing alone. It keeps the current cut (or the first user turn, dropping nothing) and replaces every tool result older than the `N`-th newest. With `CONTEXT_STRIP_OLD_REASONING` on (the default), the same estimate also removes the thinking of every assistant message but the latest ([reasoning](context-store.md#signed-reasoning)). A trim with no result to clear still tries stripping alone. If the estimate then falls under the trim target `CONTEXT_TRIM_TO_TOKENS`, that is the whole trim. Its note then only says how to fetch a cleared result back, or, when only reasoning went, that every message is still there. Otherwise the proxy also drops the oldest turns down to the target, as a plain [trim](context-management.md) does. Requiring extra headroom below the threshold (`CONTEXT_CLEAR_FIT`) did not help ([context research](context-research.md)). The clear bound only moves forward, and later requests re-render it byte for byte, so the prompt cache stays warm between trims. `0` turns clearing off.

## Masking (`CONTEXT_MASK_OBSERVATIONS`)

Masking applies to the turns a trim keeps. In the region before the latest run of tool calls, each tool result whose call is also in that region becomes the placeholder; the message right before the latest run keeps its results. Masking wins over `CONTEXT_STRIP_PROSE`, which would remove the calls as well. `0` turns it off.

With clearing on, masking mostly touches results clearing has already replaced.

## Input clearing (`CONTEXT_CLEAR_INPUTS_OVER_CHARS`)

Clearing keeps every call whole, so a written file, an edit's old and new strings, or a heredoc in a Bash command stays in the prompt after its result is gone. With input clearing on, every string field of a call's input longer than `N` characters (strictly over; nested strings in objects and arrays too) becomes a placeholder when that call's result is cleared, masked or [evicted](eviction.md), in the same step:

```text
[smarter-compact: tool input cleared to save context; 8412 chars; smart_compact_get(id="<id>") returns it]
```

`<id>` is the blob id of the original string, written to `blobs/<id>.txt` before the request leaves. Every other field stays: keys, order, file paths, short arguments, numbers and booleans, so the input is still valid JSON of the same shape. The rule needs no tool list, so it works for every harness: Anthropic `tool_use.input`, OpenAI Chat and Responses function arguments, and the OpenCode 2 / Pi plugin's tool calls. A call counts as cleared when its result sits before the clear bound, even when the result itself is too short to replace (a Write's "File written"). The bound only moves forward, so later requests re-render the same bytes. The clearing estimate counts the shrunk inputs, so clearing alone settles more trims. The TTL sweep keeps these blobs: it keeps the blob id of every string of a stored call's input that a placeholder could name. `0` turns it off and restores the earlier bytes.

## Repeated results (`CONTEXT_DEDUP_RESULTS`)

An agent often reruns a command or re-reads a file and gets the same bytes back. With this setting on (the default), a new tool result is replaced, on the request that first carries it, when all of these hold:

- it comes after the request's last assistant message (the results the model has not seen yet);
- its text is at least 200 characters (`DEDUP_MIN_CHARS`; the pointer is about 120) and has no image or file part;
- an earlier result in the same view, the same request's batch included, has byte-identical text and is shown in full: not cleared, masked, evicted or dropped.

It becomes:

```text
[smarter-compact: output identical to an earlier tool result still shown above; smart_compact_get(id="<id>") returns it]
```

`<id>` is the [blob id](context-store.md#location) of the text (a compressed result names its compression's blob), written to `blobs/<id>.txt` before the request leaves; the stored row carries it as `blob_id`, so the TTL sweep keeps the blob. Only the newer copy changes, and only before it is ever sent, so no cached prefix moves. The decision goes into the trim state (`dedup`: the item's index and fingerprint, the call id, the earlier call id and the blob id), so every later request renders the same pointer, also after the earlier copy is cleared or its turn is dropped; the pointer then still resolves through `smart_compact_get`. A pointer is never masked, and [eviction](eviction.md) leaves the earlier copy alone. Formats: Anthropic `tool_result` blocks, OpenAI Chat `tool` messages, Responses `function_call_output` items, and the OpenCode 2 / Pi plugin's `tool-result` parts; a list content becomes one text block that keeps the last block's `cache_control`. Each request that adds pointers writes a `dedup` event. `0` turns it off and restores the earlier bytes.

In measured runs the saving is negligible: it fired once in 72 trials. That hit replaced one 392-character result in `make-doom-for-mips__Sgo2JKM` and saved 7,128 characters across that trial's later requests, about 2,900 tokens at 2.5 characters per token. The session databases agree. Of 2,322 tool results of at least 200 characters, 1 repeats an earlier one byte for byte. At any length, 72 repeat, averaging 96 characters, shorter than the pointer. Method: runs `tb21-h55-inputs` (24 trials) and `tb21-h55-rtk` (48 trials), Claude Code with Haiku 5.5, each trial's main transcript replayed offline through the proxy with the trial's arm settings, with dedup on and off.

## Ledger (`CONTEXT_LEDGER`)

Off by default until a benchmark arm measures it. The source is Turning Interaction History into Execution State: A Runtime Layer for Long-Horizon Coding Agents ([arXiv 2608.00808](https://arxiv.org/html/2608.00808)). Its state view holds "a fixed task anchor and two ledger-derived components. The _current focus_ lists the files most recently modified by the agent. The _observation index_ records which files or line ranges have already been observed and reports the current status of those observations" (§3.3). An observation is current "when neither the stored file counter nor the stored global counter differs from its current value". Each repeated action gets one of three verdicts: "Allow executes the command unchanged. Reuse suppresses the command and returns a reference to a still-visible result from an earlier equivalent interaction. Nudge allows the command to execute but augments the resulting observation with a short message indicating that the action may repeat prior work." It also states: "A partial read is reusable only when the proposed line range is fully covered by an earlier observation" and "A test command repeated without an intervening edit receives Nudge" (§3.4). On 500 SWE-bench Verified tasks with mini-swe-agent (Table 4), GPT-5 mini went from 281 to 321 resolved (56.2% to 64.2%) and from $23.60 to $16.78; MiniMax M2.5 resolved 379 at baseline, 385 with inform only, 402 with govern only and 405 with both, at $34.31 against $23.39. The paper renders the view "at the end of the current model input", regenerated each step. Here that would break the prompt cache, so each block is recorded and stays where it was first sent.

The ledger is rebuilt on every request from the tool calls the request carries:

| Call | Ledger entry |
| --- | --- |
| `Read` / `read` / `read_file` / `view` with a path (`file_path`, `filePath`, `path`, `notebook_path`, `file`) | read; `offset`/`limit` give the range; numbered output (`  12→`, `  12\t`) gives the lines actually shown |
| shell (`Bash`, `bash`, `shell`, `exec_command`, `local_shell`) running one plain `cat <path>`, `sed -n 'A,Bp' <path>`, `head [-n N] <path>` or `tail [-n N] <path>` | read (whole, `A`–`B`, `1`–`N`, last `N`) |
| `Edit` / `Write` / `MultiEdit` / `NotebookEdit` / `write_file` / `create_file`; `apply_patch` headers (`*** Update File:`); shell `>`/`>>` targets, `sed -i` and `tee` files | edit: that file's counter goes up |
| any other shell command that is not read-only (the [eviction](eviction.md) read-only rule) and not a test run | global counter up: every earlier read may be stale |
| a test run (`pytest`, `go test`, `cargo test`, `npm test`, `jest`, `python -m unittest`, …) | test: a rerun with no edit since is nudged |

A read covers a later one when its file and the global counters are unchanged, it was not an error, was not cut short (truncated by the harness, or head/tail compressed by the proxy) and shows every line the later one asks for. A read to the end of the file is covered only by an untruncated read to the end, or by the same whole read through the same tool.

For the newest tool results only (after the request's last assistant message), before they are first sent:

- a covered read whose earlier copy the view still shows in full becomes `[smarter-compact: this read repeats an earlier one still shown above (<path>, <range>; the file is unchanged since); smart_compact_get(id="<id>") returns this output]`, when its text is at least 200 characters. The decision joins the [repeated-result](#repeated-results-context_dedup_results) list (`dedup`, with `"kind": "reuse"` and the pointer text), so every later request renders the same bytes. The pointer is never masked, and its output is written to `blobs/<id>.txt` and kept by the TTL sweep like a dedup pointer's;
- a covered read whose earlier copy is gone (cleared, masked, dropped) stays as it is and gets a nudge line in the ledger block, so a pointer never sends the agent to content it cannot see;
- a stale read is allowed with no nudge: after an edit, a re-read is the expected action (§3.4 reuses only unchanged files);
- a test rerun with no edit since gets a nudge line.

The block is appended to the newest tool result, once per batch and only when its listing changed since the last recorded block (or it carries nudges), at most 1,500 characters (oldest paths dropped first):

```text
[smarter-compact ledger, from your tool calls: current = unchanged since you read it, stale = may have changed since]
Modified (latest last): src/a.py ×2, tests/test_a.py
Read (latest last):
- src/a.py: whole stale; lines 40-80 current
- README.md: whole current
Repeats in this batch:
- `pytest -q tests` already ran with no file change since; rerun tests only for new evidence.
```

The block is recorded in the trim state (`ledger`: the item's index and fingerprint, the text). Older blocks are already-sent bytes, so they stay until the next trim, which keeps only the newest recorded block (thread keys `ledger_from` / `ledger_from_fp`). Placement: Anthropic adds a text block after the tool results of the user message, so masking a result leaves its block. Chat, Responses (string output) and the OpenCode 2 / Pi plugin append it after the result text, so clearing that result clears its block too. Each request that adds reuse pointers writes a `reuse` event.

Measured offline on `tb21-h55-inputs` and `tb21-h55-reuse` (48 trials, Claude Code with Haiku 5.5, about 32 requests each), replaying each main transcript through the proxy (trim at 95,000, target 40,000, clearing 10): 607 blocks, 12.6 per trial, 316 characters on average (4,000 characters per trial), 48 of 48 trials. Billed input rose 0.19% and trims rose by 0.02 per trial. No reuse pointer and no nudge fired. Of 115 file reads, 47 re-read a path already read, 19 were covered by an earlier read, and 15 of those were still current. Every one of the 15 followed a whole-file read that head/tail compression had already shortened, which shows only a part of the file, so none counted as covered.

## Test and build logs (`CONTEXT_LOG_REDUCE_OVER_CHARS`)

Off by default (`0`). With `N` > 0, a new tool result (after the request's last assistant message, not yet sent) is reduced when its text is over N characters and one of its lines looks like test or build output: a pytest session header or summary, `Ran N tests`, jest `Tests:`, `--- FAIL` / `ok pkg 0.1s`, `test result:`, `Compiling x v1`, `error[E0308]`, `make: `, `file:1:2: error:`, `error TS2322:`, Maven `[INFO] BUILD`, Gradle `BUILD FAILED in`, `npm ERR!`. The reduced form keeps:

- every line matching LogDx-CI's failure pattern `error|failed|failure|traceback|exception|assert|panic|exit code|##\[error\]` (case-insensitive), with 3 lines before and 8 after, overlapping windows merged, a window identical to an earlier one skipped, at most 8,000 characters of windows;
- the last 40 lines;
- runs of one repeated line folded to `<line> ×N`, gaps marked `… N lines …`;

under `[smarter-compact: tool result compressed, test/build log of <lines> lines, <chars> chars, reduced to its failure lines and last 40 lines — smart_compact_get(id="<id>") returns the full text]`. It applies only when the reduced form is shorter. The full text goes to `blobs/<id>.txt` before the request leaves. The stored row keeps the full text with that `blob_id`, so the sweep keeps the blob, and a later mask or dedup pointer names the same blob. The decision is recorded in the trim state (`logs`: the item's index and fingerprint, the call id, the blob id), so later requests render the same bytes. A result already sent whole is never reduced. Reduction runs before head/tail [compression](context-proxy.md) (`CONTEXT_COMPRESS_OVER_TOKENS`), which skips the reduced form; results that are not logs still get head/tail. Each request that reduces logs writes a `log_reduce` event.

Source: LogDx-CI: Benchmarking Log Reduction Tools for LLM Root-Cause Diagnosis ([arXiv 2605.28876](https://arxiv.org/html/2605.28876v1)). Its grep reducer is "Regex-filtered failure-pattern lines + 3/8 context" (Table 2); the pattern above is `baseline_grep`'s default in its [repository](https://github.com/eyuansu62/LogDx) (`tools/run_baseline.py`). The best hybrid, `hybrid-grep-120k-rtk-tail` (grep when it fits in 120k tokens, else RTK, else the last 200 lines), scored 0.747 in the agent loop against 0.749 for a gpt-5-mini summary (Table 5), at about $0.031 against $0.18 per case. This rule keeps grep plus a 40-line tail, since a single tool result is far below 120k tokens.

Measured offline on `tb21-h55-inputs` and `tb21-h55-reuse` (48 trials) at `6000`: it fired once in 48 trials, on 1 of 1,633 tool results, and saved 21,528 characters on that result.

## Error results (`CONTEXT_KEEP_ERRORS`)

Off by default (`0`). With `K` > 0, each trim records the call ids of the K newest error tool results the view still shows whole (thread key `keep_errors`). Clearing, observation masking and input clearing leave those results and their calls' inputs whole until the next trim, which records a new set. A result already cleared stays cleared, so the set only moves forward and requests between trims render the same bytes. A result counts as an error when its block says so (Anthropic `is_error`, an OpenCode 2 `error` result) or a line of it starts with `Exit code N` (N ≠ 0), `Traceback (most recent call last)`, `<Name>Error: ` / `<Name>Exception: `, `FAILED`, `FAIL`, `ERROR` or `error: `, or contains `command not found` or `No such file or directory`. Results are cleared on the Anthropic route and in the OpenCode 2 / Pi plugin, so those are where this applies. A trim may still drop the turn that holds an error.

## Getting a result back

One call returns a cleared result: `smart_compact_get(id="<id>")` with the id from its placeholder, in 12,000-character pages. Addressable Recall Compaction ([ARC](https://arxiv.org/abs/2607.25066)) motivates this: recalling compacted tool outputs by id beat similarity search on needle-in-a-haystack, 99.40% against 88.12%.

The trim note says so, and keeps the older route as a fallback, since every result also stays in the session database: find the call with `smart_compact_search`, then read the next `tool_result` row of the same tool with `smart_compact_sql`:

```sql
SELECT text FROM messages
WHERE kind = 'tool_result' AND name = '<tool>' AND seq > <call seq>
ORDER BY seq LIMIT 1
```

## Evidence

Terminal-Bench 2.1, Claude Code with Haiku 5.5, eight long tasks, 23–24 trials per arm (run `tb21-h55-publish`):

| Arm | Passed | Cost (USD) |
| --- | --- | --- |
| Native compaction | 16/23 | 4.08 |
| Trim only | 13/24 | 1.68 |
| Trim + masking, no clearing | 14/23 | 2.00 |
| Trim + clearing 10, no masking | 14/23 | 1.46 |

To settle a trim, clearing alone had to land under the target, and at a 40,000-token target it almost never did. In run `tb21-h55-idevict` the clearing arm made 21 trims that dropped 574 messages, and 1 of 24 trials showed a placeholder. Clearing keeps every tool call whole. Right after such a trim, call inputs (Bash commands and written files) filled about 40% of the prompt, the system prompt and tools 27%, and tool results 25%.

Thinking is the larger share in Claude Code sessions on Haiku 5.5. In four `tb21-h55-clearfit` trials it billed 42,000 to 68,000 tokens (41% to 73% of the prompt) at the first trim, while the old estimate charged its signatures nothing, so a trim that freed 64,000 billed tokens recorded 99,228 → 99,015 estimated ([reasoning](context-store.md#signed-reasoning)). An offline replay of runs `tb21-h55-idevict` and `tb21-h55-rtk` compares the two settings on the 59 of 96 trajectories that pass 95,000. It used threshold 95,000, target 40,000, `N` = 10 and input clearing `2000`. Each request was billed as text at the trial's fitted ratio plus signature characters / 3.2, and fed back as usage so the proxy calibrates as it does live:

| `CONTEXT_STRIP_OLD_REASONING` | Trims | Settled without dropping a message | Trims per trial | Messages dropped per trial | Median billed after a trim | Median estimate error after a trim | Billed input per trial |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `0` | 76 | 0 | 1.29 | 48.5 | 27,300 | +23,000 | 1.98M |
| `1` | 94 | 36 | 1.59 | 31.3 | 41,000 | −500 | 2.09M (+6%) |

With signatures charged, a trim lands at the target instead of 13,000 tokens under it, so it drops a third fewer messages, but the fuller context bills 6% more input and trims 0.3 more times per trial (each a cache rewrite, not counted here). The replay re-sends what the agent did, so it measures requests, not pass rates.

No run has tested the shipped default, clearing and masking together, without the scratchpad. The research behind clearing, and how this masking differs from the papers' observation masking: [context research](context-research.md#clearing-versus-turn-deletion-2026-11-05). Anthropic's server-side alternative: [context proxy](context-proxy.md#server-side-tool-result-clearing).
