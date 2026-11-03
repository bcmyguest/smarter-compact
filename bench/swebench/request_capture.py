#!/usr/bin/env python3
"""Log what the model was actually offered, per request, into the trial dir.

A smart trial that trims but never recalls is only evidence if we know the recall tools were
in the request. Without this, `recall_available` is unverifiable and "the model ignored the
tool" is indistinguishable from "the tool was never offered" — which cost a whole sweep.

Runs INSIDE the eval container (so: stdlib only, no litellm), as a forwarding proxy between
opencode and the real endpoint. Cheap to keep on for every trial: `index.jsonl` gets one
summary line per request always, and the full body is written only when the offering
signature (tool names + pointer presence) changes, gzipped.

Env: CAPTURE_UPSTREAM (e.g. https://api.anthropic.com/v1), CAPTURE_DIR, CAPTURE_PORT.
"""
from __future__ import annotations

import gzip
import http.client
import itertools
import json
import os
import re
import ssl
import sys
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM = urllib.parse.urlparse(os.environ.get("CAPTURE_UPSTREAM", "https://api.anthropic.com/v1"))
CAPTURE_DIR = os.environ.get("CAPTURE_DIR", "/opt/trial/requests")
PORT = int(os.environ.get("CAPTURE_PORT", "18899"))
# Current spelling first; the pre-rename one is still counted, or a sweep would read the rename
# itself as "no pointer was ever sent".
POINTERS = ("[smarter-compact: continuing session", "[openmem: continuing session")
POINTER = POINTERS[0]
STRIP = {"host", "connection", "content-length", "proxy-connection", "keep-alive", "transfer-encoding"}
SECRET = {"x-api-key", "authorization", "api-key"}
SEQ = itertools.count()
SEEN: set[tuple] = set()


def facts(body: dict) -> dict:
    """What this request offered: tools, message count, and whether the trim pointer is in it."""
    raw = json.dumps(body)
    tools = body.get("tools") or body.get("tool_definitions") or []
    # Two wire shapes, both seen here: Anthropic sends flat {"name": ...}, an
    # OpenAI-compatible endpoint (cairn2, @ai-sdk/openai-compatible) nests it as
    # {"type":"function","function":{"name":...}}. Reading only the flat key made
    # every cairn2 capture report tools=[] and recall_tools=[], so "the pointer
    # names a tool that was never offered" was undetectable on that provider --
    # measured on cairn-probe-15k, where the pointer was in fact correct.
    names = sorted({
        n for t in tools if isinstance(t, dict)
        for n in [t.get("name") or (t.get("function") or {}).get("name")]
        if isinstance(n, str)
    })
    # Only the prompt, never the tool list: our own search tool's description quotes the pointer
    # ("Use it when an [smarter-compact: continuing session …] pointer says…"), so a whole-body
    # scan marks every request that merely *offers* the recall tools as pointed. Measured on the
    # hard45-haiku-60k-cap capture, that read 254/528 requests as pointed and let a run where no
    # pointer ever reached the model pass the "trimmed but no pointer" check.
    prompt = json.dumps(body.get("messages") or []) + json.dumps(body.get("system") or "")
    # Tool calls the model had already made, visible as history in THIS request. Bodies are
    # stored once per offering signature (the earliest request of each), so a call made mid-trial
    # lands in no stored body at all -- cairn-probe-15k kept 2 of 25 requests and looked like a
    # trial that never called anything. The index has a row per request, so recording calls here
    # is what makes "did the model actually use recall?" answerable from the capture. Take the
    # MAX of `recall_calls` across a trial's rows, never the sum: a call persists in every later
    # request's history, so summing counts one call dozens of times.
    calls = []
    for m in body.get("messages") or []:
        for tc in (m.get("tool_calls") or []):  # OpenAI-compatible wire shape
            n = (tc.get("function") or {}).get("name") or tc.get("name")
            if isinstance(n, str):
                calls.append(n)
        content = m.get("content")
        if isinstance(content, list):  # Anthropic content blocks
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "tool_use" and isinstance(blk.get("name"), str):
                    calls.append(blk["name"])
    return {
        "calls": sorted(set(calls)),
        "recall_calls": sum(1 for n in calls if "smart_compact" in n or n == "context_status"),
        "model": body.get("model"),
        "messages": len(body.get("messages") or []),
        "tools": names,
        "recall_tools": [n for n in names if "smart_compact" in n or n == "context_status"],
        "pointer": any(p in prompt for p in POINTERS),
        "chars": len(raw),
    }


def record(body, status: int, path: str, headers: dict | None = None) -> None:
    try:
        os.makedirs(CAPTURE_DIR, exist_ok=True)
        row = {"seq": next(SEQ), "ts": round(time.time(), 3), "path": path, "status": status}
        if headers:  # credentials never land in the trial dir
            row["headers"] = {k: ("[redacted]" if k.lower() in SECRET else v) for k, v in headers.items()}
        try:
            parsed = json.loads(body)
            row.update(facts(parsed) if isinstance(parsed, dict) else {"non_dict": True})
        except Exception:  # noqa: BLE001 - a non-JSON body still counts as a request
            row["bytes"] = len(body)
            parsed = None
        with open(os.path.join(CAPTURE_DIR, "index.jsonl"), "a") as fh:
            fh.write(json.dumps(row) + "\n")
        if isinstance(parsed, dict):
            # Store each offering once, plus the first request in which the model had actually
            # called recall: that body carries the call and its arguments, which is the proof a
            # "did it use search?" question needs. Bounded at a few bodies per trial.
            key = (tuple(row.get("tools") or []), bool(row.get("pointer")), bool(row.get("recall_calls")))
            if key not in SEEN:  # the offering is stable across a trial; store each variant once
                SEEN.add(key)
                name = f"{row['seq']:04d}-tools{len(row.get('tools') or [])}" \
                       + ("-pointer" if row.get("pointer") else "") \
                       + ("-recall" if row.get("recall_calls") else "") + ".json.gz"
                with gzip.open(os.path.join(CAPTURE_DIR, name), "wb") as fh:
                    fh.write(json.dumps(parsed).encode())
    except Exception as e:  # noqa: BLE001 - never break the agent's own request
        print(f"capture record failed: {type(e).__name__}: {e}", file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # noqa: N802 - silence stdlib access logging into the agent stream
        pass

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/health", ""):
            return self._respond(200, b'{"ok":true}', "application/json")
        return self._proxy()

    def do_POST(self):  # noqa: N802
        return self._proxy()

    def _respond(self, status, payload, ctype):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _forward_path(self) -> str:
        # opencode posts <baseURL>/messages; with baseURL pointed here the path already
        # carries the upstream prefix, so don't double it.
        return self.path if self.path.startswith(UPSTREAM.path or "/") and UPSTREAM.path not in ("", "/") \
            else (UPSTREAM.path.rstrip("/") + self.path)

    def _proxy(self):
        n = next(SEQ)
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        headers = {k: v for k, v in self.headers.items() if k.lower() not in STRIP}
        conn = None
        try:
            if UPSTREAM.scheme == "https":
                conn = http.client.HTTPSConnection(UPSTREAM.hostname, UPSTREAM.port or 443,
                                                   timeout=1800, context=ssl.create_default_context())
            else:
                conn = http.client.HTTPConnection(UPSTREAM.hostname, UPSTREAM.port or 80, timeout=1800)
            conn.request(self.command, self._forward_path(), body=body, headers=headers)
            up = conn.getresponse()
            self.close_connection = True  # stream to EOF; no content-length on SSE
            # send_response (not send_response_only): keeps a valid status line, and http.client
            # has already decoded any chunked framing, so transfer-encoding must not be forwarded.
            self.send_response(up.status, up.reason)
            for k, v in up.getheaders():
                if k.lower() not in STRIP:
                    self.send_header(k, v)
            self.send_header("connection", "close")
            self.end_headers()
            while True:
                chunk = up.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
            self.wfile.flush()
            record(body, up.status, self.path, headers)
        except Exception as e:  # noqa: BLE001 - surface the failure to the agent, and to the log
            print(f"capture seq={n} forward failed: {type(e).__name__}: {e}", file=sys.stderr)
            try:
                self._respond(502, json.dumps({"error": f"capture proxy: {e}"}).encode(), "application/json")
                record(body, 502, self.path, headers)
            except Exception:  # noqa: BLE001
                pass
        finally:
            if conn:
                conn.close()


if __name__ == "__main__":
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    print(f"capture proxy on :{PORT} -> {UPSTREAM.geturl()} dir={CAPTURE_DIR}", file=sys.stderr, flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
