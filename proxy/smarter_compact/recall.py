"""Hindsight recall and ingest-status calls (stdlib urllib)."""

import json
import urllib.parse
import urllib.request

OPS_TIMEOUT_S = 2.0


def urllib_http(method, url, body, timeout):
    """Default transport: returns (status, parsed JSON or None)."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw.decode("utf-8")) if raw else None)


def _bank_url(base, bank):
    return f"{base}/v1/default/banks/{urllib.parse.quote(bank, safe='')}"


def recall_text(http, base, bank, query, max_chars, timeout):
    """Recalled memory text, at most max_chars; '' when nothing came back."""
    body = {"query": query, "budget": "low", "max_tokens": max(256, max_chars // 4)}
    status, value = http("POST", f"{_bank_url(base, bank)}/memories/recall", body, timeout)
    if status != 200 or not isinstance(value, dict):
        return ""
    hits = value.get("results") or []
    texts = []
    for hit in hits:
        if isinstance(hit, dict):
            text = hit.get("text") or hit.get("memory") or hit.get("content")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    return "\n\n".join(texts)[:max_chars]


def active_operations(http, base, bank):
    """Pending + processing ingest operations for the bank."""
    total = 0
    for status in ("pending", "processing"):
        code, value = http("GET", f"{_bank_url(base, bank)}/operations?status={status}&limit=1", None, OPS_TIMEOUT_S)
        if code == 200 and isinstance(value, dict):
            count = value.get("total")
            if count is None:
                count = len(value.get("operations") or [])
            total += int(count)
    return total
