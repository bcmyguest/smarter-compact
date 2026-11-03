#!/usr/bin/env python3
"""Smoke test for request_capture.py: forwarding stays intact and the offering is recorded.

Run: python3 test_request_capture.py   (no pytest, no network: the upstream is a local stub)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
UP_PORT, CAP_PORT = 18998, 18999
SEEN_UPSTREAM: list[str] = []


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        SEEN_UPSTREAM.append(self.path)
        payload = json.dumps({"id": "x", "content": [{"type": "text", "text": "ok"}]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        assert json.loads(body)["model"] == "claude-test"


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="capture-test-")
    threading.Thread(target=ThreadingHTTPServer(("127.0.0.1", UP_PORT), Upstream).serve_forever,
                     daemon=True).start()
    cap = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "request_capture.py")],
        env={**os.environ, "CAPTURE_UPSTREAM": f"http://127.0.0.1:{UP_PORT}/v1",
             "CAPTURE_DIR": tmp, "CAPTURE_PORT": str(CAP_PORT)},
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(40):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{CAP_PORT}/health", timeout=1)
                break
            except Exception:
                time.sleep(0.25)
        else:
            return print("FAIL: forwarder never came up") or 1

        # The recall tool's real description quotes the pointer marker, so this body must read as
        # "not pointed": only a marker in the prompt counts. See facts() in request_capture.py.
        body = json.dumps({
            "model": "claude-test",
            "messages": [{"role": "user", "content": "what was the third api key?"}],
            "tools": [
                {"name": "bash"},
                {"name": "smart_compact_search", "description":
                    "Use it when an [smarter-compact: continuing session …] pointer says earlier messages were trimmed."},
                {"name": "smart_compact_sql"},
            ],
            "x_api_key_leak": "sk-ant-secret",
        }).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{CAP_PORT}/v1/messages", data=body,
                                     headers={"content-type": "application/json", "x-api-key": "sk-ant-secret"})
        reply = json.loads(urllib.request.urlopen(req, timeout=10).read())
        assert reply["id"] == "x", "response not forwarded"
        assert SEEN_UPSTREAM == ["/v1/messages"], f"wrong upstream path: {SEEN_UPSTREAM}"

        # a second, identical-offering request must not duplicate the stored body
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{CAP_PORT}/v1/messages", data=body,
                                                      headers={"content-type": "application/json"}), timeout=10)
        time.sleep(0.4)

        rows = [json.loads(l) for l in open(os.path.join(tmp, "index.jsonl"))]
        assert len(rows) == 2, f"expected 2 index rows, got {len(rows)}"
        row = rows[0]
        assert row["status"] == 200 and row["model"] == "claude-test", row
        assert row["recall_tools"] == ["smart_compact_search", "smart_compact_sql"], row
        assert row["pointer"] is False, "a marker quoted in a tool description is not a trim pointer"
        assert row["tools"] == ["bash", "smart_compact_search", "smart_compact_sql"], row

        bodies = [f for f in os.listdir(tmp) if f.endswith(".json.gz")]
        assert len(bodies) == 1, f"identical offerings should store one body, got {bodies}"

        # A pointer in the prompt itself is the case that matters: same tools, so only the
        # message content can distinguish it from the row above.
        pointed = json.dumps({
            "model": "claude-test",
            "messages": [{"role": "user", "content":
                "[smarter-compact: continuing session ses_test; 9 earlier messages are NOT in your context]"}],
            "tools": json.loads(body)["tools"],
        }).encode()
        urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{CAP_PORT}/v1/messages", data=pointed,
                                                      headers={"content-type": "application/json"}), timeout=10)
        time.sleep(0.4)
        rows = [json.loads(l) for l in open(os.path.join(tmp, "index.jsonl"))]
        assert len(rows) == 3, f"expected a third index row, got {len(rows)}"
        assert rows[-1]["pointer"] is True, "a pointer in the prompt must be detected"
        assert rows[-1]["tools"] == row["tools"], "same offering, so only the pointer changed"
        bodies = sorted(f for f in os.listdir(tmp) if f.endswith(".json.gz"))
        assert len(bodies) == 2, f"a pointed offering is a distinct signature, got {bodies}"
        assert any(f.endswith("-pointer.json.gz") for f in bodies), bodies
        import gzip
        stored = gzip.decompress(open(os.path.join(tmp, bodies[0]), "rb").read()).decode()
        assert "sk-ant-secret" not in json.dumps(rows), "headers leaked a key into index.jsonl"
        assert json.loads(stored)["x_api_key_leak"] == "sk-ant-secret", "body should be stored verbatim"
        # Tool names arrive in two wire shapes. Reading only Anthropic's flat form made every
        # OpenAI-compatible capture (cairn2) report tools=[] and recall_tools=[], which hides
        # exactly the "pointer names a tool that was never offered" case this recorder exists
        # to catch — it happened on cairn-probe-15k while the pointer was in fact correct.
        from request_capture import facts
        flat = facts({"tools": [{"name": "smart_compact_search"}], "messages": []})
        nested = facts({"tools": [{"type": "function",
                                   "function": {"name": "smarter-compact_smart_compact_search"}}],
                        "messages": []})
        assert flat["recall_tools"] == ["smart_compact_search"], flat
        assert nested["recall_tools"] == ["smarter-compact_smart_compact_search"], nested
        assert nested["tools"] == ["smarter-compact_smart_compact_search"], nested
        print(f"PASS  forwarded={SEEN_UPSTREAM} rows={len(rows)} bodies={bodies} recall_tools={row['recall_tools']}")
        return 0
    finally:
        cap.kill()


if __name__ == "__main__":
    raise SystemExit(main())
