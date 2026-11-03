"""Session id resolution, in contract order."""

import hashlib
import json
import re

from .store import InvalidSessionId, sanitize_sid

_LEGACY_USER_ID_RE = re.compile(r"_session_([0-9A-Za-z-]{8,})$")
_FALLBACK_HEADERS = ("session_id", "x-session-id", "conversation_id")


def _headers(data):
    psr = data.get("proxy_server_request") or {}
    raw = psr.get("headers") or {}
    return {str(k).lower(): v for k, v in raw.items()} if isinstance(raw, dict) else {}


def _body(data):
    psr = data.get("proxy_server_request") or {}
    body = psr.get("body")
    return body if isinstance(body, dict) else {}


def _from_user_id(user_id):
    if not isinstance(user_id, str) or not user_id:
        return None
    try:
        parsed = json.loads(user_id)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("session_id"):
        return str(parsed["session_id"])
    m = _LEGACY_USER_ID_RE.search(user_id)
    return m.group(1) if m else None


def _first_user_text(adapter, items):
    for item in items or ():
        if isinstance(item, dict) and adapter.is_turn_start(item):
            return adapter.user_text(item)
    return ""


def _candidates(data, headers, body):
    """Session id sources in contract order (docs/context-store.md, "Session id")."""
    yield headers.get("x-openmem-session-id")
    yield headers.get("x-claude-code-session-id")
    for meta in (data.get("metadata"), body.get("metadata")):
        if isinstance(meta, dict):
            yield _from_user_id(meta.get("user_id"))
    yield data.get("prompt_cache_key")
    yield body.get("prompt_cache_key")
    for name in _FALLBACK_HEADERS:
        yield headers.get(name)


def resolve_session_id(data, adapter, items):
    """Sanitized session id from the first source that yields a non-empty one, else fp-<hash>."""
    headers = _headers(data)
    body = _body(data)
    for raw in _candidates(data, headers, body):
        if not raw:
            continue
        try:
            return sanitize_sid(raw)
        except InvalidSessionId:
            continue
    seed = adapter.system_text(data) + _first_user_text(adapter, items)
    return "fp-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
