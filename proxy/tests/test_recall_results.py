"""Results of our own recall tools reach the model whole on arrival: never compressed, log-reduced or pointered.
Reproduces trial path-tracing-reverse__ujsWcua: a 16 KB objdump result was compressed, the agent fetched it with
mcp__smarter-compact__smart_compact_get, and that get result was compressed again into a new blob.
Run: python3 -m unittest discover -s proxy/tests"""

import asyncio
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_live_shape import CC, _hook_module, total_tokens  # noqa: E402

from smarter_compact.compress import (  # noqa: E402
    DEDUP_PREFIX, MARKER_PREFIX, MASK_PREFIX, compressed_blob_id,
)

SID = "recall-sess"
GET = "mcp__smarter-compact__smart_compact_get"
# 470 lines of disassembly, about the 16,344 chars of the trial's /tmp/main.s.
OBJDUMP = "470 /tmp/main.s\n0000000000401740 <main>:\n" + "\n".join(
    f"  {0x401744 + 4 * k:x}:\tmovss  0x{0x68 + k:x}(%rsp),%xmm{k % 16}" for k in range(468))
# A stored pytest log over CONTEXT_LOG_REDUCE_OVER_CHARS: a Bash result like it is reduced to its failure lines.
LOG = "\n".join(["============================= test session starts =============================="]
                + [f"tests/test_mod.py::test_case_{k} PASSED" for k in range(300)]
                + ["FAILED tests/test_mod.py::test_last - AssertionError", "1 failed, 300 passed in 2.31s"])


def get_text(bid, text):
    """The MCP server's text for a whole smart_compact_get page (plugin/lib/mcp/context-store.mjs toolResultText)."""
    return f"[smart_compact_get id={bid}: characters 0-{len(text)} of {len(text)}, complete]\n{text}"


def call(k, name, args):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": f"toolu_{k}", "name": name, "input": args}]}


def result(k, text):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"toolu_{k}",
                                         "content": [{"type": "text", "text": text}]}]}


def body(msgs, call_id):
    return {"model": "claude-haiku-5-5",
            "system": [{"type": "text", "text": "You are a Claude agent.", "cache_control": CC}],
            "tools": [{"name": "Bash", "input_schema": {"type": "object"}},
                      {"name": "ToolSearch", "input_schema": {"type": "object"}}],
            "messages": msgs + [total_tokens(1000, True)], "litellm_call_id": call_id,
            "proxy_server_request": {"headers": {"X-Claude-Code-Session-Id": SID}}}


def result_text(msg):
    content = msg["content"][0]["content"]
    return content if isinstance(content, str) else content[0]["text"]


class RecallResultsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hook = _hook_module(self.tmp.name)
        self.env = {"SMARTER_COMPACT_STATE_DIR": self.tmp.name, "CONTEXT_TRIM_AT_TOKENS": "10000000",
                    "CONTEXT_STANDING_INSTRUCTION": "0"}
        self.blobs = Path(self.tmp.name) / "context" / "blobs"
        self.n = 0

    def send(self, msgs, **env):
        self.n += 1
        key = types.SimpleNamespace(request_route="/v1/messages")
        with mock.patch.dict(os.environ, {**self.env, **env}):
            out = asyncio.run(self.hook.handler.async_pre_call_hook(
                key, None, json.loads(json.dumps(body(msgs, f"c{self.n}"))), "anthropic_messages"))
        return out["messages"]

    def blob_ids(self):
        return sorted(p.stem for p in self.blobs.glob("*.txt")) if self.blobs.exists() else []

    def test_a_get_of_a_compressed_result_stays_whole_and_readable(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "Reverse /app/mystery"}]},
                call(0, "Bash", {"command": "objdump -d mystery > /tmp/main.s; cat /tmp/main.s"}), result(0, OBJDUMP)]
        first = self.send(msgs)
        compressed = result_text(first[2])
        self.assertTrue(compressed.startswith(MARKER_PREFIX), "the Bash result itself is still compressed")
        bid = compressed_blob_id(compressed)
        self.assertEqual(self.blob_ids(), [bid])
        self.assertEqual((self.blobs / f"{bid}.txt").read_text(), OBJDUMP)

        got = get_text(bid, (self.blobs / f"{bid}.txt").read_text())
        msgs += [call(1, GET, {"id": bid, "max_chars": 100000}), result(1, got)]
        second = self.send(msgs)
        self.assertEqual(result_text(second[5]), got, "the get result goes out whole")
        self.assertIn("\n  401744:\tmovss", result_text(second[5]))
        self.assertNotIn("\\n", result_text(second[5]))
        self.assertEqual(self.blob_ids(), [bid], "no new blob for the get result")

        # The same get again (identical, over DEDUP_MIN_CHARS) gets no identical-result pointer either.
        msgs += [call(2, GET, {"id": bid, "max_chars": 100000}), result(2, got)]
        third = self.send(msgs, CONTEXT_DEDUP_RESULTS="1", CONTEXT_LEDGER="1")
        self.assertEqual(result_text(third[7]), got)
        self.assertFalse(result_text(third[7]).startswith(DEDUP_PREFIX))
        self.assertEqual(self.blob_ids(), [bid])

    def test_a_get_of_a_log_is_not_log_reduced(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "Fix the test"}]},
                call(0, "Bash", {"command": "pytest"}), result(0, LOG)]
        reduced = result_text(self.send(msgs, CONTEXT_LOG_REDUCE_OVER_CHARS="6000")[2])
        self.assertTrue(reduced.startswith(MARKER_PREFIX), "the Bash log itself is still reduced")
        bid = compressed_blob_id(reduced)
        got = get_text(bid, (self.blobs / f"{bid}.txt").read_text())
        msgs += [call(1, "smarter-compact.smart_compact_get", {"id": bid}), result(1, got)]
        out = self.send(msgs, CONTEXT_LOG_REDUCE_OVER_CHARS="6000")
        self.assertEqual(result_text(out[4]), got)

    def test_a_trim_may_still_clear_a_get_result_and_its_blob_is_plain_text(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "Reverse /app/mystery"}]},
                call(0, "Bash", {"command": "cat /tmp/main.s"}), result(0, OBJDUMP)]
        bid = compressed_blob_id(result_text(self.send(msgs)[2]))
        got = get_text(bid, OBJDUMP)
        msgs += [call(1, GET, {"id": bid, "max_chars": 100000}), result(1, got)]
        for k in range(2, 5):
            msgs += [call(k, "Bash", {"command": f"echo {k}"}), result(k, f"out {k}")]
        sessions = Path(self.tmp.name) / "context" / "sessions"
        sessions.mkdir(parents=True, exist_ok=True)
        (sessions / f"{SID}.force").write_text("")
        out = self.send(msgs, CONTEXT_CLEAR_KEEP_RESULTS="1")
        cleared = [result_text(m) for m in out if m["role"] == "user" and isinstance(m["content"], list)
                   and m["content"] and m["content"][0].get("type") == "tool_result"
                   and m["content"][0]["tool_use_id"] == "toolu_1"]
        self.assertEqual(len(cleared), 1)
        self.assertTrue(cleared[0].startswith(MASK_PREFIX), "a trim clears a get result like any other")
        named = cleared[0].split('id="', 1)[1][:16]
        self.assertEqual((self.blobs / f"{named}.txt").read_text(), got, "its blob holds the plain get text")


if __name__ == "__main__":
    unittest.main()
