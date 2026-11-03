"""Tool-result compression: a pure function of the content and the threshold."""

import hashlib
import re

from .tokens import text_tokens

HEAD_LINES = 40
TAIL_LINES = 20
HEAD_CHARS = 4000
TAIL_CHARS = 2000
MARKER_PREFIX = "[openmem: tool result compressed,"
_ID_RE = re.compile(r'smart_compact_get\(id="([0-9a-f]{16})"\)')


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
