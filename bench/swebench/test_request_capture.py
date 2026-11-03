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

        body = json.dumps({
            "model": "claude-test",
            "messages": [{"role": "user", "content": "[smarter-compact: continuing session ses_test; 9 earlier messages are NOT in your context]"}],
            "tools": [{"name": "bash"}, {"name": "smart_compact_search"}, {"name": "smart_compact_sql"}],
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
        assert row["pointer"] is True, "trim pointer not detected"
        assert row["tools"] == ["bash", "smart_compact_search", "smart_compact_sql"], row

        bodies = [f for f in os.listdir(tmp) if f.endswith(".json.gz")]
        assert len(bodies) == 1, f"identical offerings should store one body, got {bodies}"
        import gzip
        stored = gzip.decompress(open(os.path.join(tmp, bodies[0]), "rb").read()).decode()
        assert "sk-ant-secret" not in json.dumps(rows), "headers leaked a key into index.jsonl"
        assert json.loads(stored)["x_api_key_leak"] == "sk-ant-secret", "body should be stored verbatim"
        print(f"PASS  forwarded={SEEN_UPSTREAM} rows={len(rows)} bodies={bodies} recall_tools={row['recall_tools']}")
        return 0
    finally:
        cap.kill()


if __name__ == "__main__":
    raise SystemExit(main())
