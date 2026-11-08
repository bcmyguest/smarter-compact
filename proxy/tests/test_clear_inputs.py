"""CONTEXT_CLEAR_INPUTS_OVER_CHARS: a cleared call's long input strings become placeholders naming their blobs.
Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import re
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import SID, Base, anth_request  # noqa: E402
from test_trim_experiments import full_tokens  # noqa: E402

from smarter_compact import Settings, ttl  # noqa: E402
from smarter_compact.adapters import ChatAdapter, ResponsesAdapter  # noqa: E402
from smarter_compact.compress import (INPUT_MASK_PREFIX, MASK_PREFIX, blob_id, input_placeholder,  # noqa: E402
                                      masked_input)
from smarter_compact.settings import DEFAULT_CLEAR_INPUTS_OVER_CHARS  # noqa: E402

BAR = 2000
_ID_RE = re.compile(r'smart_compact_get\(id="([0-9a-f]{16})"\)')
# Clearing results and inputs alone lands under this fraction of the request, clearing results alone does not.
SETTLE = 0.5


def settle(msgs):
    """A target clearing alone reaches only with input clearing on."""
    return int(SETTLE * full_tokens(msgs))


def body(k, n=6000):
    return f"file {k}\n" + "w" * n


def write_turns(steps=5, n=6000):
    """The task, then steps of a Write whose input dominates (content n chars) and a short result, then a Bash."""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "the task"}]}]
    for k in range(steps):
        args = {"file_path": f"/app/f{k}.py", "content": body(k, n), "mode": 420, "dry": False,
                "meta": {"notes": ["short", "n" * (n // 2)], "tag": "t"}}
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": f"step {k}"},
                                                      {"type": "tool_use", "id": f"w{k}", "name": "Write", "input": args}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"w{k}", "content": "written"}]})
    msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": "wL", "name": "Bash",
                                                   "input": {"command": "ls"}}]})
    msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "wL", "content": "ok"}]})
    return msgs


def inputs_in(view):
    return {b["id"]: b["input"] for m in view if isinstance(m.get("content"), list) for b in m["content"]
            if b.get("type") == "tool_use"}


def strings(value):
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in strings(v)]
    return [s for v in value for s in strings(v)] if isinstance(value, list) else []


def later_step(msgs, k):
    return msgs + [{"role": "assistant", "content": [{"type": "tool_use", "id": f"n{k}", "name": "Write",
                                                      "input": {"file_path": f"/n{k}", "content": body(k)}}]},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"n{k}", "content": "written"}]}]


class MaskedInputTests(unittest.TestCase):
    def test_strictly_over_the_bar(self):
        for n, cleared in ((BAR - 1, False), (BAR, False), (BAR + 1, True)):
            s = "a" * n
            self.assertEqual(masked_input(s, BAR) != s, cleared, n)
        s = "é" * (BAR + 1)
        self.assertEqual(masked_input(s, BAR), input_placeholder(s))
        self.assertIn(f" {BAR + 1} chars; ", input_placeholder(s))
        self.assertIn(f'smart_compact_get(id="{blob_id(s)}")', input_placeholder(s))

    def test_nested_strings_and_other_fields(self):
        big = "b" * 3000
        value = {"path": "/x", "n": 5, "flag": True, "none": None, "list": ["s", big, {"deep": big}], "obj": {"k": big}}
        out = masked_input(value, BAR)
        self.assertEqual(list(out), list(value))
        self.assertEqual((out["path"], out["n"], out["flag"], out["none"]), ("/x", 5, True, None))
        self.assertEqual(out["list"], ["s", input_placeholder(big), {"deep": input_placeholder(big)}])
        self.assertEqual(out["obj"], {"k": input_placeholder(big)})
        self.assertEqual(json.loads(json.dumps(out)), out)
        self.assertIsNot(out, value)
        small = {"path": "/x", "list": ["s"]}
        self.assertIs(masked_input(small, BAR), small)

    def test_placeholder_never_re_masked(self):
        p = input_placeholder("c" * 5000)
        self.assertTrue(p.startswith(INPUT_MASK_PREFIX))
        self.assertIs(masked_input(p, 1), p)
        # A string no longer than its placeholder stays, whatever the bar.
        self.assertEqual(masked_input("d" * 50, 1), "d" * 50)

    def test_chat_and_responses_arguments(self):
        big = "c" * 3000
        args = json.dumps({"path": "/a", "content": big})
        item = {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
                                                      "function": {"name": "write", "arguments": args}}]}
        out = ChatAdapter().masked_inputs(item, {"c1"}, BAR)
        self.assertEqual(json.loads(out["tool_calls"][0]["function"]["arguments"]),
                         {"path": "/a", "content": input_placeholder(big)})
        self.assertIs(ChatAdapter().masked_inputs(item, {"other"}, BAR), item)
        call = {"type": "function_call", "call_id": "r1", "name": "write", "arguments": args}
        out = ResponsesAdapter().masked_inputs(call, {"r1"}, BAR)
        self.assertEqual(json.loads(out["arguments"])["content"], input_placeholder(big))
        custom = {"type": "custom_tool_call", "call_id": "r2", "name": "patch", "input": big}
        self.assertEqual(ResponsesAdapter().masked_inputs(custom, {"r2"}, BAR)["input"], input_placeholder(big))


class ClearInputsTests(Base):
    def send(self, msgs, sid=SID, **kw):
        kw.setdefault("chars_per_token", 1.0)
        kw.setdefault("clear_keep_results", 2)
        return self.run_req(anth_request(copy.deepcopy(msgs), sid=sid), **kw)["messages"]

    def trims(self, sid=SID):
        conn = sqlite3.connect(self.ctx / "sessions" / f"{sid}.db")
        try:
            return conn.execute("SELECT dropped FROM events WHERE kind = 'trim' ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_setting_from_env(self):
        self.assertEqual(DEFAULT_CLEAR_INPUTS_OVER_CHARS, 2000)
        self.assertEqual(Settings().clear_inputs_over_chars, 2000)
        self.assertEqual(Settings.from_env({}).clear_inputs_over_chars, 2000)
        self.assertEqual(Settings.from_env({"CONTEXT_CLEAR_INPUTS_OVER_CHARS": "500"}).clear_inputs_over_chars, 500)
        self.assertEqual(Settings.from_env({"CONTEXT_CLEAR_INPUTS_OVER_CHARS": "0"}).clear_inputs_over_chars, 0)
        self.assertEqual(Settings.from_env({"CONTEXT_CLEAR_INPUTS_OVER_CHARS": "-1"}).clear_inputs_over_chars, 0)
        self.assertEqual(Settings.from_env({"CONTEXT_CLEAR_INPUTS_OVER_CHARS": "x"}).clear_inputs_over_chars, 2000)

    def test_cleared_with_its_result_only(self):
        msgs = write_turns()
        view = self.send(msgs, trim_at_tokens=full_tokens(msgs), trim_to_tokens=settle(msgs))
        self.assertEqual([m["role"] for m in view], [m["role"] for m in msgs])
        got, orig = inputs_in(view), inputs_in(msgs)
        # The 2 newest results (w4, wL) stay with their inputs; every older call's long strings are placeholders.
        self.assertEqual((got["w4"], got["wL"]), (orig["w4"], orig["wL"]))
        for k in range(4):
            a, b = got[f"w{k}"], orig[f"w{k}"]
            self.assertEqual(list(a), list(b))
            self.assertEqual(a["content"], input_placeholder(b["content"]))
            self.assertEqual(a["meta"], {"notes": ["short", input_placeholder(b["meta"]["notes"][1])], "tag": "t"})
            self.assertEqual((a["file_path"], a["mode"], a["dry"]), (b["file_path"], 420, False))
        self.assertEqual(self.trims(), [(0,)])

    def test_clearing_inputs_settles_where_inputs_dominate(self):
        msgs = write_turns()
        full = full_tokens(msgs)
        on = self.send(msgs, trim_at_tokens=full, trim_to_tokens=settle(msgs))
        self.assertEqual(len(on), len(msgs))
        self.assertLess(full_tokens(on), SETTLE * full)
        self.assertNotIn("earlier messages are NOT", json.dumps(on))
        # Off, clearing results alone frees almost nothing, so the trim drops turns.
        off = self.send(msgs, "sid-off", trim_at_tokens=full, trim_to_tokens=settle(msgs),
                        clear_inputs_over_chars=0)
        self.assertLess(len(off), len(msgs))
        self.assertIn("earlier messages are NOT", json.dumps(off))

    def test_byte_identical_across_requests(self):
        msgs = write_turns()
        full = full_tokens(msgs)
        prev, more = self.send(msgs, trim_at_tokens=full, trim_to_tokens=settle(msgs)), msgs
        for k in range(2):
            more = later_step(more, k)
            view = self.send(more, trim_at_tokens=10 ** 9)
            self.assertEqual(json.dumps(view[:len(prev)]), json.dumps(prev))
            self.assertEqual(inputs_in(view)[f"n{k}"]["content"], body(k))
            prev = view
        self.assertEqual(json.dumps(self.send(more, trim_at_tokens=10 ** 9)), json.dumps(prev))

    def test_blob_written_and_kept_by_the_sweep(self):
        msgs = write_turns()
        view = self.send(msgs, trim_at_tokens=full_tokens(msgs), trim_to_tokens=settle(msgs))
        texts = [s for k in range(4) for s in (body(k), "n" * 3000)]
        placeholders = [s for v in inputs_in(view).values() for s in strings(v) if s.startswith(INPUT_MASK_PREFIX)]
        self.assertEqual(len(placeholders), 8)
        for p in placeholders:
            bid = _ID_RE.search(p).group(1)
            self.assertIn((self.ctx / "blobs" / f"{bid}.txt").read_text(encoding="utf-8"), texts)
        stray = self.ctx / "blobs" / "ffffffffffffffff.txt"
        stray.write_text("orphan")
        self.assertEqual(ttl.sweep_orphan_blobs(self.ctx), ["ffffffffffffffff"])
        self.assertEqual(len(list((self.ctx / "blobs").iterdir())), 5)  # 4 file bodies, 1 shared notes string

    def test_off_gives_the_old_bytes(self):
        msgs = write_turns()
        kw = dict(trim_at_tokens=full_tokens(msgs), trim_to_tokens=settle(msgs))
        off = self.send(msgs, "sid-a", clear_inputs_over_chars=0, **kw)
        huge = self.send(msgs, "sid-b", clear_inputs_over_chars=10 ** 9, **kw)
        self.assertNotIn(INPUT_MASK_PREFIX, json.dumps(off))
        self.assertEqual(json.dumps(off).replace("sid-a", "S"), json.dumps(huge).replace("sid-b", "S"))
        orig = inputs_in(msgs)
        self.assertEqual(inputs_in(off), {k: orig[k] for k in inputs_in(off)})

    def test_masked_observations_clear_inputs_too(self):
        msgs = write_turns()
        view = self.send(msgs, trim_at_tokens=full_tokens(msgs), clear_keep_results=0, mask_observations=True)
        got = inputs_in(view)
        masked = [k for k, v in got.items() if INPUT_MASK_PREFIX in json.dumps(v)]
        self.assertTrue(masked)
        for k in masked:
            # Masked only alongside its result.
            res = [b for m in view if isinstance(m.get("content"), list) for b in m["content"]
                   if b.get("type") == "tool_result" and b.get("tool_use_id") == k]
            self.assertTrue(all(r["content"] == "written" or str(r["content"]).startswith(MASK_PREFIX) for r in res))

    def test_eviction_clears_evicted_inputs(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "the task"}]}]
        steps = [("Write", {"file_path": "/app/a.py", "content": body(0)}, "written"),
                 *[("Bash", {"command": f"ls d{i}"}, f"result marker{i:04d}x\n" + "." * 300) for i in range(2)],
                 ("Write", {"file_path": "/app/a.py", "content": body(1)}, "written"),
                 *[("Bash", {"command": f"ls e{i}"}, f"result marker{i + 10:04d}x\n" + "." * 300) for i in range(2)]]
        for i, (name, args, out) in enumerate(steps):
            msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": name,
                                                           "input": args}]})
            msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": out}]})
        view = self.send(msgs, eviction=True, clear_keep_results=0)
        self.assertEqual(inputs_in(view)["t0"]["content"], input_placeholder(body(0)))
        self.assertEqual(inputs_in(view)["t3"]["content"], body(1))
        off = self.send(msgs, "sid-off", eviction=True, clear_keep_results=0, clear_inputs_over_chars=0)
        self.assertEqual(inputs_in(off), inputs_in(msgs))


if __name__ == "__main__":
    unittest.main()
