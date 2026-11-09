"""CONTEXT_LEDGER through the LiteLLM pre-call hook on the request shape Claude Code 2.1.319 sends: every
request ends with a role "system" message (<total_tokens>), after the new tool results.
Run: python3 -m unittest discover -s proxy/tests"""

import asyncio
import copy
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

from smarter_compact import ledger  # noqa: E402

SID = "live-sess"
CC = {"type": "ephemeral", "ttl": "1h"}
REMINDER = "<system-reminder>\nThe date is 2026-11-09.\n</system-reminder>"
A_PY = "\n".join(f"{k:>6}\t" + f"line {k} of a.py with some text" for k in range(1, 41))
B_PY = "\n".join(f"{k:>6}\t" + f"line {k} of b.py with some text" for k in range(1, 41))


def _hook_module(state_dir):
    """smarter_compact_hook, imported with litellm stubbed when absent and the store under state_dir."""
    if "litellm.integrations.custom_logger" not in sys.modules:
        try:
            import litellm.integrations.custom_logger  # noqa: F401
        except ImportError:
            mods = {n: types.ModuleType(n) for n in ("litellm", "litellm.integrations",
                                                     "litellm.integrations.custom_logger")}
            mods["litellm.integrations.custom_logger"].CustomLogger = object
            sys.modules.update(mods)
    with mock.patch.dict(os.environ, {"SMARTER_COMPACT_STATE_DIR": state_dir}):
        import smarter_compact_hook
    return smarter_compact_hook


def total_tokens(n, cached):
    block = {"type": "text", "text": f"<total_tokens>{n} tokens left</total_tokens>"}
    return {"role": "system", "content": [{**block, "cache_control": CC} if cached else block]}


def step(k, tool, args, out, reminder=False):
    """One assistant step (signed thinking, tool_use) and its tool_result, with a system-reminder text block."""
    result = [{"type": "tool_result", "tool_use_id": f"toolu_{k}", "content": out}]
    return [{"role": "assistant", "content": [
                {"type": "thinking", "thinking": f"plan {k}", "signature": f"EuYBCkYICxgCKkB{k}=="},
                {"type": "tool_use", "id": f"toolu_{k}", "name": tool, "input": args}]},
            {"role": "user", "content": result + ([{"type": "text", "text": REMINDER}] if reminder else [])}]


def messages(n):
    """The first n steps as Claude Code sends them: only the newest <total_tokens> message carries cache_control."""
    steps = [("Read", {"file_path": "/app/a.py"}, A_PY), ("Read", {"file_path": "/app/b.py"}, B_PY)]
    msgs = [{"role": "user", "content": [{"type": "text", "text": REMINDER}, {"type": "text", "text": "Fix a.py"}]},
            {"role": "system", "content": [{"type": "text", "text": "# Environment\n - Primary working directory: /app"}]}]
    for k in range(n):
        msgs += step(k, *steps[k], reminder=k == 0)
        msgs.append(total_tokens(1000 - k, k == n - 1))
    return msgs


def body(n, call_id):
    return {"model": "claude-haiku-5-5",
            "system": [{"type": "text", "text": "You are a Claude agent.", "cache_control": CC},
                       {"type": "text", "text": "Harness rules.", "cache_control": CC}],
            "tools": [{"name": "Bash", "input_schema": {"type": "object"}},
                      {"name": "Read", "input_schema": {"type": "object"}}],
            "thinking": {"type": "adaptive"}, "context_management": {"edits": [{"type": "clear_thinking_20251015"}]},
            "messages": messages(n), "litellm_call_id": call_id,
            "proxy_server_request": {"headers": {"X-Claude-Code-Session-Id": SID}}}


def texts(msg):
    return [b["text"] for b in msg["content"] if isinstance(b, dict) and b.get("type") == "text"]


class HookLiveShapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hook = _hook_module(self.tmp.name)
        env = {"SMARTER_COMPACT_STATE_DIR": self.tmp.name, "CONTEXT_LEDGER": "1",
               "CONTEXT_TRIM_AT_TOKENS": "10000000", "CONTEXT_STANDING_INSTRUCTION": "0"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)

    def call(self, n):
        key = types.SimpleNamespace(request_route="/v1/messages")
        out = asyncio.run(self.hook.handler.async_pre_call_hook(key, None, body(n, f"c{n}"), "anthropic_messages"))
        return out["messages"]

    def test_ledger_on_the_tool_results_before_the_trailing_system_message(self):
        first = self.call(1)
        sent = messages(1)
        # The tool_result message stays as is; the block goes after the trailing system message's blocks, as two
        # system messages may not be adjacent.
        self.assertEqual(len(first), len(sent))
        self.assertEqual(json.dumps(first[:4]), json.dumps(sent[:4]))
        self.assertEqual(first[4]["content"][0], sent[4]["content"][0])
        added = texts(first[4])[1:]
        self.assertEqual(len(added), 1)
        self.assertTrue(added[0].startswith(ledger.LEDGER_HEAD))
        self.assertIn("/app/a.py", added[0])

        second = self.call(2)
        # The recorded block re-renders byte-identically (cache_control moved on); the new result gets its own.
        self.assertEqual(json.dumps(second[:4]), json.dumps(first[:4]))
        self.assertEqual(texts(second[4]), texts(first[4]))
        self.assertEqual(second[6], messages(2)[6])
        new = texts(second[7])[1:]
        self.assertEqual(len(new), 1)
        self.assertTrue(new[0].startswith(ledger.LEDGER_HEAD))
        self.assertIn("/app/b.py", new[0])

        state = json.loads((Path(self.tmp.name) / "context" / "sessions" / f"{SID}.trim.json").read_text())
        self.assertEqual([d["at"] for d in state["ledger"]], [3, 6])

    def test_a_request_ending_in_assistant_and_system_gets_nothing(self):
        msgs = messages(1)[:3] + [total_tokens(999, True)]
        key = types.SimpleNamespace(request_route="/v1/messages")
        data = {**body(1, "c-side"), "messages": copy.deepcopy(msgs)}
        out = asyncio.run(self.hook.handler.async_pre_call_hook(key, None, data, "anthropic_messages"))
        self.assertEqual(json.dumps(out["messages"]), json.dumps(msgs))


if __name__ == "__main__":
    unittest.main()
