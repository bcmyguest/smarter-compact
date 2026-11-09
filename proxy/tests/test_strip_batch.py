"""CONTEXT_STRIP_REASONING_EVERY: every N assistant steps, signed reasoning before the latest assistant message goes,
gated by CONTEXT_STRIP_REASONING_MIN_TOKENS. Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import Base, headers  # noqa: E402
from test_strip_reasoning import SID, kinds, loop, request  # noqa: E402

from smarter_compact import Settings  # noqa: E402


def thinking_at(view):
    return [j for j, m in enumerate(view) if "thinking" in kinds(m)]


def chat_loop(steps, sig=4000):
    msgs = [{"role": "user", "content": "the task"}]
    for k in range(steps):
        msgs.append({"role": "assistant", "content": f"step {k}", "thinking_blocks": [
            {"type": "thinking", "thinking": "", "signature": f"{k}" * sig}], "reasoning_content": "r",
            "tool_calls": [{"id": f"c{k}", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{k}", "content": f"out {k}"})
    return msgs


class BatchBase(Base):
    def settings(self, **kw):
        kw.setdefault("strip_reasoning_every", 4)
        kw.setdefault("strip_reasoning_min_tokens", 0)
        kw.setdefault("clear_keep_results", 0)
        kw.setdefault("chars_per_token", 1.0)
        return super().settings(**kw)

    def anth(self, steps, **kw):
        return self.run_req(request(copy.deepcopy(loop(steps))), **kw)["messages"]

    def events(self, kind):
        conn = sqlite3.connect(self.ctx / "sessions" / f"{SID}.db")
        try:
            return conn.execute("SELECT tokens_before, tokens_after FROM events WHERE kind = ?", (kind,)).fetchall()
        finally:
            conn.close()


class SettingTests(unittest.TestCase):
    def test_defaults(self):
        s = Settings.from_env({})
        self.assertEqual((s.strip_reasoning_every, s.strip_reasoning_min_tokens), (0, 8000))
        s = Settings.from_env({"CONTEXT_STRIP_REASONING_EVERY": "12", "CONTEXT_STRIP_REASONING_MIN_TOKENS": "4000"})
        self.assertEqual((s.strip_reasoning_every, s.strip_reasoning_min_tokens), (12, 4000))


class AnthropicTests(BatchBase):
    def test_off_gives_the_old_bytes(self):
        for n in range(1, 10):
            self.assertEqual(json.dumps(self.anth(n, strip_reasoning_every=0)), json.dumps(loop(n)))

    def test_bound_moves_only_at_batch_boundaries(self):
        views = {n: self.anth(n) for n in range(1, 10)}
        for n in (1, 2, 3):
            self.assertEqual(json.dumps(views[n]), json.dumps(loop(n)))
        # Step 4 opens batch 1: reasoning before the latest assistant message goes, every message stays.
        self.assertEqual(len(views[4]), len(loop(4)))
        self.assertEqual(thinking_at(views[4]), [7])
        # Within the batch every request re-renders the same prefix; new steps keep their reasoning.
        for n in (5, 6, 7):
            self.assertEqual(json.dumps(views[n][:len(views[n - 1])]), json.dumps(views[n - 1]))
        self.assertEqual(thinking_at(views[7]), [7, 9, 11, 13])
        self.assertEqual(thinking_at(views[8]), [15])
        self.assertEqual(len(self.events("strip_batch")), 2)
        before, after = self.events("strip_batch")[0]
        self.assertGreater(before - after, 3 * 4000)

    def test_message_never_emptied(self):
        msgs = loop(4)
        for m in msgs:
            if m["role"] == "assistant":
                m["content"] = [m["content"][0]]
        msgs = [msgs[0]] + [m for m in msgs[1:] if m["role"] == "assistant"]
        msgs = [x for m in msgs for x in ([m, {"role": "user", "content": [{"type": "text", "text": "go"}]}]
                                          if m["role"] == "assistant" else [m])]
        view = self.run_req(request(copy.deepcopy(msgs)))["messages"]
        self.assertEqual(len(view), len(msgs))
        self.assertEqual(json.dumps(view), json.dumps(msgs))

    def test_gain_gate_waits_for_the_next_boundary(self):
        # 3 blocks of 4000 before the latest at step 4 (12,000) miss a 20,000 floor; at step 8, 7 blocks clear it.
        views = {n: self.anth(n, strip_reasoning_min_tokens=20_000) for n in range(1, 10)}
        self.assertEqual(json.dumps(views[7]), json.dumps(loop(7)))
        self.assertEqual(thinking_at(views[8]), [15])
        self.assertEqual(json.dumps(views[9][:len(views[8])]), json.dumps(views[8]))

    def test_estimate_counts_the_strip(self):
        # Full size is about 9 x 4,000 signature characters; stripped, it stays under a 20,000 threshold.
        for n in range(1, 10):
            view = self.anth(n, trim_at_tokens=20_000, strip_reasoning_every=2)
        self.assertEqual(len(view), len(loop(9)))
        self.assertEqual(self.events("trim"), [])

    def test_survives_a_trim(self):
        for n in range(1, 6):
            self.anth(n)
        trimmed = self.anth(6, trim_at_tokens=1, trim_to_tokens=1)
        state = json.loads((self.ctx / "sessions" / f"{SID}.trim.json").read_text())
        [entry] = state["threads"].values()
        self.assertEqual((entry["reason"], entry["reason_batch"]), (7, 1))
        self.assertIn("cut", entry)
        self.assertEqual(json.dumps(self.anth(6)), json.dumps(trimmed))


class ChatTests(BatchBase):
    def test_chat_thinking_blocks(self):
        views = [self.run_req({"messages": chat_loop(n), **headers()}, "acompletion")["messages"] for n in (3, 4, 5)]
        self.assertEqual(views[0], chat_loop(3))
        stripped = [j for j, m in enumerate(views[1]) if "thinking_blocks" in m]
        self.assertEqual(stripped, [7])
        self.assertEqual(json.dumps(views[2][:len(views[1])]), json.dumps(views[1]))
        off = self.run_req({"messages": chat_loop(5), **headers()}, "acompletion", strip_reasoning_every=0)
        self.assertEqual(off["messages"], chat_loop(5))

    def test_responses_reasoning_items_stay(self):
        # Dropping a reasoning item would shift the recorded indices, so the batched strip leaves Responses alone.
        signed = json.dumps([{"type": "thinking", "thinking": "", "signature": "s" * 4000}])
        items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}]
        for k in range(5):
            items += [{"type": "reasoning", "summary": [], "encrypted_content": signed},
                      {"type": "function_call", "call_id": f"r{k}", "name": "shell", "arguments": "{}"},
                      {"type": "function_call_output", "call_id": f"r{k}", "output": f"out {k}"}]
        out = self.run_req({"input": copy.deepcopy(items), **headers()}, "aresponses", strip_reasoning_every=2)
        self.assertEqual(out["input"], items)


if __name__ == "__main__":
    unittest.main()
