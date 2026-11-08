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
# Only a result at least this long becomes a dedup pointer (the pointer is about 120 characters).
DEDUP_MIN_CHARS = 200
# Start of every cleared tool-input string's placeholder (CONTEXT_CLEAR_INPUTS_OVER_CHARS); never re-masked.
INPUT_MASK_PREFIX = "[smarter-compact: tool input cleared to save context;"
# Characters of an input placeholder besides its count digits: prefix, " ", " chars; ", the get call, "]".
_INPUT_PLACEHOLDER_FIXED = (len(INPUT_MASK_PREFIX) + len(' chars; smart_compact_get(id="') + 16
                            + len('") returns it]') + 1)


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
