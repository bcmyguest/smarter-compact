"""Request size in characters (flat charge for binary attachments), the prompt-token estimate, and fingerprints.

text_tokens (characters / 4) sizes single tool results for compression. The trim threshold uses
estimate_prompt_tokens, calibrated per session from the provider's reported usage.
"""

import hashlib
import json
import math

# Block types whose payload is binary (base64) rather than text.
BINARY_BLOCK_TYPES = frozenset({"image", "document", "input_image", "input_file", "image_url", "file"})
# Flat charge per binary block, in characters (about 1600 tokens).
BINARY_BLOCK_CHARS = 6400
# Characters per prompt token before a session has reported usage. Claude Code requests measured about 2.3.
DEFAULT_CHARS_PER_TOKEN = 2.5
# Bounds on an observed ratio, so one odd usage report cannot make the estimate absurd.
MIN_CHARS_PER_TOKEN = 1.0
MAX_CHARS_PER_TOKEN = 8.0
# Usage samples kept per session (one per conversation thread).
MAX_USAGE_THREADS = 32


def text_tokens(text):
    return (len(text) + 3) // 4


def _chars(value):
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        if isinstance(value.get("type"), str) and value["type"] in BINARY_BLOCK_TYPES:
            return BINARY_BLOCK_CHARS
        return sum(len(str(k)) + _chars(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return sum(_chars(v) for v in value)
    if value is None:
        return 0
    return len(str(value))


def request_chars(*values):
    """Characters in the values, as the trim measures a request."""
    return sum(_chars(v) for v in values)


def _sample(value):
    """(chars, tokens) of a stored usage sample, or None when it is missing or malformed."""
    if not isinstance(value, dict):
        return None
    chars, tokens = value.get("chars"), value.get("tokens")
    if type(chars) is int and type(tokens) is int and chars > 0 and tokens > 0:
        return chars, tokens
    return None


def _ratio(sample, default):
    if sample is None:
        return default
    return min(MAX_CHARS_PER_TOKEN, max(MIN_CHARS_PER_TOKEN, sample[0] / sample[1]))


def _usage_threads(usage):
    threads = usage.get("threads") if isinstance(usage, dict) else None
    return threads if isinstance(threads, dict) else {}


def estimate_prompt_tokens(chars, usage, root, default_ratio):
    """Prompt tokens of a request of `chars` characters in conversation thread `root`.

    usage is the session's calibration (record_usage). With a sample for this thread: its reported tokens plus
    the characters added since, at that sample's chars-per-token ratio. Otherwise chars at the ratio of the
    session's latest sample, else default_ratio.
    """
    own = _sample(_usage_threads(usage).get(root))
    if own is not None:
        return max(0, own[1] + math.ceil((chars - own[0]) / _ratio(own, default_ratio)))
    last = _sample(usage.get("last")) if isinstance(usage, dict) else None
    return math.ceil(chars / _ratio(last, default_ratio))


def record_usage(state, root, chars, tokens):
    """Store a reported prompt size (tokens) for a request of `chars` characters in thread `root` into state["usage"].

    Returns False, leaving state alone, when either value is not a positive integer.
    """
    if _sample({"chars": chars, "tokens": tokens}) is None:
        return False
    usage = state.get("usage")
    if not isinstance(usage, dict):
        usage = state["usage"] = {}
    threads = _usage_threads(usage)
    sample = {"chars": chars, "tokens": tokens}
    threads.pop(root, None)
    threads[root] = sample
    while len(threads) > MAX_USAGE_THREADS:
        threads.pop(next(iter(threads)))
    usage["threads"] = threads
    usage["last"] = dict(sample)
    return True


def _normalize(value):
    if isinstance(value, dict):
        out = {k: _normalize(v) for k, v in value.items() if k != "cache_control"}
        content = out.get("content")
        if (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
                and content[0].get("type") == "text" and set(content[0]) == {"type", "text"}):
            out["content"] = content[0]["text"]
        return out
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def fingerprint(value):
    """Stable hash of a message, ignoring key order, cache_control markers and single-text-block wrapping.

    Claude Code marks the newest message with cache_control and sends its content as one text block; on later
    requests the marker is gone and the same content is a plain string. Both shapes must hash the same.
    """
    blob = json.dumps(_normalize(value), sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
