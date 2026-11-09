"""Tool-result compression: a pure function of the content and the threshold."""

import hashlib
import re

from .tokens import text_tokens

HEAD_LINES = 40
TAIL_LINES = 20
HEAD_CHARS = 4000
TAIL_CHARS = 2000
MARKER_PREFIX = "[smarter-compact: tool result compressed,"
_ID_RE = re.compile(r'smart_compact_get\(id="([0-9a-f]{16})"\)')
# Start of every cleared or masked result's placeholder; detection matches on it (no quote: JSON keeps it as is).
MASK_PREFIX = "[smarter-compact: tool output cleared to save context;"
# Start of a repeated tool result's pointer (CONTEXT_DEDUP_RESULTS); never masked, so its bytes never change.
DEDUP_PREFIX = "[smarter-compact: output identical to an earlier tool result still shown above;"
# Start of a repeated read's pointer (CONTEXT_LEDGER); never masked either.
REUSE_PREFIX = "[smarter-compact: this read repeats an earlier one still shown above"
# Only a result at least this long becomes a dedup pointer (the pointer is about 120 characters).
DEDUP_MIN_CHARS = 200
# Start of every cleared tool-input string's placeholder (CONTEXT_CLEAR_INPUTS_OVER_CHARS); never re-masked.
INPUT_MASK_PREFIX = "[smarter-compact: tool input cleared to save context;"
# Characters of an input placeholder besides its count digits: prefix, " ", " chars; ", the get call, "]".
_INPUT_PLACEHOLDER_FIXED = (len(INPUT_MASK_PREFIX) + len(' chars; smart_compact_get(id="') + 16
                            + len('") returns it]') + 1)
# CONTEXT_LOG_REDUCE_OVER_CHARS: test/build output over the setting keeps its failure lines and its tail. The failure
# pattern and its 3/8 context are LogDx-CI's grep reducer (arXiv 2605.28876, Table 2; tools/run_baseline.py).
LOG_FAILURE_RE = re.compile(r"error|failed|failure|traceback|exception|assert|panic|exit code|##\[error\]", re.IGNORECASE)
LOG_BEFORE, LOG_AFTER = 3, 8
LOG_TAIL_LINES = 40
# Characters of failure windows kept before the rest are counted, not shown.
LOG_MATCH_MAX_CHARS = 8000
# Output a test runner, build tool or compiler prints: one line matching makes a result a log. Matched per line
# ("\n"-split), so the plugin's JavaScript port reads the same lines.
LOG_SIGNATURE_RE = re.compile(
    r"^=+ (?:test session starts|FAILURES|ERRORS|short test summary info) =+"
    r"|^(?:FAILED|PASSED|ERROR) [^ ]+::|[0-9]+ (?:passed|failed)(?:,| in )|^Ran [0-9]+ tests? in "
    r"|^(?:Tests|Test Suites): +[0-9]|^(?:---|===) (?:FAIL|RUN|PASS)|^(?:ok|FAIL)[ \t]+[^ \t]+[ \t]+[0-9.]+s"
    r"|^test result: (?:ok|FAILED)\.|^[ \t]*Compiling [^ ]+ v[0-9]|^error\[E[0-9]{4}\]|^make(?:\[[0-9]+\])?: "
    r"|^[^ \t:]+:[0-9]+:[0-9]+: (?:fatal )?(?:error|warning):|error TS[0-9]+:|^\[(?:INFO|ERROR)\] (?:BUILD|Tests run:)"
    r"|^BUILD (?:SUCCESSFUL|FAILED) in |^> Task :|^npm ERR!")


def blob_id(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def is_compressed(text):
    return text.startswith(MARKER_PREFIX)


def compressed_blob_id(text):
    """Blob id referenced by a compressed form, else None."""
    if not is_compressed(text):
        return None
    m = _ID_RE.search(text.split("\n", 1)[0])
    return m.group(1) if m else None


def result_blob_id(text):
    """Blob holding a tool result's full text: the compressed form's own blob, else blob_id(text)."""
    return compressed_blob_id(text) or blob_id(text)


def mask_placeholder(text):
    """A cleared result's text, naming its blob (ARC-style id recall); a function of text, so cache-stable."""
    return f'{MASK_PREFIX} smart_compact_get(id="{result_blob_id(text)}") returns it]'


def dedup_placeholder(text):
    """A repeated result's text, naming the blob of its full text; a function of text, so cache-stable."""
    return f'{DEDUP_PREFIX} smart_compact_get(id="{result_blob_id(text)}") returns it]'


def input_placeholder(text):
    """A cleared tool-input string, naming its length and blob; a function of text, so cache-stable."""
    return f'{INPUT_MASK_PREFIX} {len(text)} chars; smart_compact_get(id="{blob_id(text)}") returns it]'


def input_clearable(text, over=0):
    """Whether a tool-input string over `over` chars becomes its input_placeholder: longer than it, not one already."""
    n = len(text)
    return n > over and n > _INPUT_PLACEHOLDER_FIXED + len(str(n)) and not text.startswith(INPUT_MASK_PREFIX)


def masked_input(value, over):
    """value (a tool call's input) with every string, nested ones too, that input_clearable over `over` replaced by
    its input_placeholder; keys, order and other values kept; the same object when none changes."""
    if isinstance(value, str):
        return input_placeholder(value) if input_clearable(value, over) else value
    if isinstance(value, dict):
        out = {k: masked_input(v, over) for k, v in value.items()}
        return value if all(out[k] is v for k, v in value.items()) else out
    if isinstance(value, list):
        out = [masked_input(v, over) for v in value]
        return value if all(a is b for a, b in zip(out, value)) else out
    return value


def cleared_inputs(value, shown):
    """The strings of value that shown carries as their input_placeholder (walked in parallel)."""
    if isinstance(value, str):
        return [value] if isinstance(shown, str) and shown != value and shown == input_placeholder(value) else []
    if isinstance(value, dict) and isinstance(shown, dict):
        return [s for k, v in value.items() if k in shown for s in cleared_inputs(v, shown[k])]
    if isinstance(value, list) and isinstance(shown, list):
        return [s for v, w in zip(value, shown) for s in cleared_inputs(v, w)]
    return []


def clearable_strings(value):
    """Every string of value that input_clearable at any bar: the blobs a cleared input may name (TTL sweep)."""
    if isinstance(value, str):
        return [value] if input_clearable(value) else []
    if isinstance(value, dict):
        return [s for v in value.values() for s in clearable_strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in clearable_strings(v)]
    return []


def compress_text(text, over_tokens):
    """Return (compressed, blob_id) when text exceeds over_tokens, else None."""
    if over_tokens <= 0 or is_compressed(text):
        return None
    tokens = text_tokens(text)
    if tokens <= over_tokens:
        return None
    lines = text.split("\n")
    if len(lines) > HEAD_LINES + TAIL_LINES:
        head = "\n".join(lines[:HEAD_LINES])[:HEAD_CHARS]
        tail = "\n".join(lines[-TAIL_LINES:])[-TAIL_CHARS:]
    else:
        head = text[:HEAD_CHARS]
        tail = text[max(HEAD_CHARS, len(text) - TAIL_CHARS):]
    bid = blob_id(text)
    out = (
        f'{MARKER_PREFIX} {tokens} tokens — smart_compact_get(id="{bid}") returns the full text]\n'
        f"{head}\n…\n{tail}"
    )
    if len(out) >= len(text):
        return None
    return out, bid


def is_log(text):
    """Whether a line of text looks like test or build output (LOG_SIGNATURE_RE)."""
    return any(LOG_SIGNATURE_RE.search(line) for line in text.split("\n"))


def _folded(lines):
    """lines with each run of identical lines shown once, suffixed " ×N"."""
    out, k = [], 0
    while k < len(lines):
        n = 1
        while k + n < len(lines) and lines[k + n] == lines[k]:
            n += 1
        out.append(f"{lines[k]} ×{n}" if n > 1 else lines[k])
        k += n
    return out


def reduce_log(text, over_chars):
    """(reduced, blob_id) for test/build output over over_chars that shrinks, else None. The reduced form keeps every
    line matching LOG_FAILURE_RE with LOG_BEFORE/LOG_AFTER lines of context (overlapping windows merged, a window
    identical to an earlier one skipped, runs of one line folded "×N", at most LOG_MATCH_MAX_CHARS) and the last
    LOG_TAIL_LINES lines, under a compression marker naming the full text's blob."""
    if over_chars <= 0 or len(text) <= over_chars or is_compressed(text) or not is_log(text):
        return None
    lines = text.split("\n")
    tail = max(0, len(lines) - LOG_TAIL_LINES)
    windows = []
    for k, line in enumerate(lines[:tail]):
        if LOG_FAILURE_RE.search(line):
            lo, hi = max(0, k - LOG_BEFORE), min(tail, k + LOG_AFTER + 1)
            if windows and lo <= windows[-1][1]:
                windows[-1][1] = max(windows[-1][1], hi)
            else:
                windows.append([lo, hi])
    out, seen, used, shown_to, omitted = [], set(), 0, 0, 0
    for lo, hi in windows:
        body = "\n".join(_folded(lines[lo:hi]))
        if body in seen:
            continue
        if used + len(body) > LOG_MATCH_MAX_CHARS:
            omitted += 1
            continue
        if lo > shown_to:
            out.append(f"… {lo - shown_to} lines …")
        out.append(body)
        seen.add(body)
        used += len(body)
        shown_to = hi
    if omitted:
        out.append(f"… {omitted} more failure windows not shown …")
    if tail > shown_to:
        out.append(f"… {tail - shown_to} lines …")
    out.extend(_folded(lines[tail:]))
    bid = blob_id(text)
    reduced = (f"{MARKER_PREFIX} test/build log of {len(lines)} lines, {len(text)} chars, reduced to its failure lines "
               f'and last {LOG_TAIL_LINES} lines — smart_compact_get(id="{bid}") returns the full text]\n'
               + "\n".join(out))
    return (reduced, bid) if len(reduced) < len(text) else None
