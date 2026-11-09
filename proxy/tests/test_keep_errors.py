"""CONTEXT_KEEP_ERRORS: the K newest error tool results stay whole through trim clearing and masking.
Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import SID, Base, anth_request, headers  # noqa: E402

from smarter_compact import Settings, process_request  # noqa: E402
from smarter_compact.compress import INPUT_MASK_PREFIX, MASK_PREFIX  # noqa: E402
from smarter_compact.core import KEEP_ERRORS_MAX_CHARS, _is_error  # noqa: E402
from smarter_compact.tokens import request_chars  # noqa: E402

LONG = "y" * 3000


def anth_steps(outs):
    """outs = [(text, is_error)]: one Bash call per result, each with a long input string."""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "the task"}]}]
    for k, (out, err) in enumerate(outs):
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{k}", "name": "Bash", "input": {"command": f"run {k} {LONG}"}}]})
        block = {"type": "tool_result", "tool_use_id": f"t{k}", "content": out}
        if err:
            block["is_error"] = True
        msgs.append({"role": "user", "content": [block]})
    return msgs


def results(view):
    return {b["tool_use_id"]: b["content"] for m in view if isinstance(m.get("content"), list)
            for b in m["content"] if b.get("type") == "tool_result"}


def inputs(view):
    return {b["id"]: b["input"]["command"] for m in view if isinstance(m.get("content"), list)
            for b in m["content"] if b.get("type") == "tool_use"}


OK = ("ok " * 200, False)
ERR = ("Exit code 1\n" + "boom " * 200, True)
TRACE = ("Traceback (most recent call last):\n  File x\nValueError: bad " + "z" * 600, False)


class KeepBase(Base):
    def settings(self, **kw):
        kw.setdefault("keep_errors", 2)
        kw.setdefault("clear_keep_results", 1)
        kw.setdefault("strip_old_reasoning", False)
        kw.setdefault("dedup_results", False)
        return super().settings(**kw)

    def anth(self, outs, **kw):
        """The request's view, trimming at its own full size (clearing settles it) unless kw sets the threshold."""
        data = anth_request(copy.deepcopy(anth_steps(outs)))
        full = math.ceil(request_chars(data["system"], data["tools"], data["messages"]) / 2.5)
        kw.setdefault("trim_at_tokens", full)
        kw.setdefault("trim_to_tokens", full)
        return self.run_req(data, **kw)["messages"]


class UnitTests(unittest.TestCase):
    def test_error_detection(self):
        self.assertTrue(_is_error(True, "x"))
        self.assertTrue(_is_error(False, "Exit code 2\nfoo"))
        self.assertTrue(_is_error(False, TRACE[0]))
        self.assertTrue(_is_error(False, "bash: foo: command not found"))
        self.assertFalse(_is_error(False, "all good\nexit code 0"))

    def test_default_on(self):
        self.assertEqual(Settings.from_env({}).keep_errors, 3)
        self.assertEqual(Settings.from_env({"CONTEXT_KEEP_ERRORS": "0"}).keep_errors, 0)


class TrimTests(KeepBase):
    def test_off_gives_the_old_bytes(self):
        outs = [ERR, OK, OK, OK]
        off = self.anth(outs, keep_errors=0)
        self.assertTrue(results(off)["t0"].startswith(MASK_PREFIX))
        # Same bytes as the plain settings with keep_errors=0 (the default is now 3).
        self.ctx = self.ctx.with_name("ctx-2")
        data = anth_request(copy.deepcopy(anth_steps(outs)))
        full = math.ceil(request_chars(data["system"], data["tools"], data["messages"]) / 2.5)
        plain = Base.settings(self, clear_keep_results=1, strip_old_reasoning=False, dedup_results=False,
                              keep_errors=0, trim_at_tokens=full, trim_to_tokens=full)
        self.assertEqual(json.dumps(off), json.dumps(process_request(data, "anthropic_messages", plain)["messages"]))

    def test_errors_exempt_from_clearing_and_input_clearing(self):
        view = self.anth([ERR, OK, TRACE, OK, OK])
        r = results(view)
        self.assertEqual(r["t0"], ERR[0])
        self.assertEqual(r["t2"], TRACE[0])
        self.assertTrue(r["t1"].startswith(MASK_PREFIX))
        self.assertTrue(r["t3"].startswith(MASK_PREFIX))
        self.assertNotIn(INPUT_MASK_PREFIX, inputs(view)["t0"])
        self.assertIn(INPUT_MASK_PREFIX, inputs(view)["t1"])
        state = json.loads((self.ctx / "sessions" / f"{SID}.trim.json").read_text())
        self.assertEqual(next(iter(state["threads"].values()))["keep_errors"], ["t2", "t0"])

    def test_only_errors_up_to_the_cap_stay_whole(self):
        at_cap = "Exit code 1\n" + "b" * (KEEP_ERRORS_MAX_CHARS - 12)
        self.assertEqual(len(at_cap), KEEP_ERRORS_MAX_CHARS)
        view = self.anth([(at_cap, True), (at_cap + "b", True), OK, OK])
        r = results(view)
        self.assertEqual(r["t0"], at_cap)
        self.assertTrue(r["t1"].startswith(MASK_PREFIX))
        self.assertNotIn(INPUT_MASK_PREFIX, inputs(view)["t0"])
        self.assertIn(INPUT_MASK_PREFIX, inputs(view)["t1"])
        state = json.loads((self.ctx / "sessions" / f"{SID}.trim.json").read_text())
        self.assertEqual(next(iter(state["threads"].values()))["keep_errors"], ["t0"])

    def test_only_the_k_newest_and_byte_identical_between_trims(self):
        outs = [ERR, ERR, ERR, OK, OK]
        view = self.anth(outs)
        r = results(view)
        self.assertTrue(r["t0"].startswith(MASK_PREFIX))
        self.assertEqual((r["t1"], r["t2"]), (ERR[0], ERR[0]))
        again = self.anth(outs + [OK], trim_at_tokens=10_000_000)
        self.assertEqual(json.dumps(again[:len(view)]), json.dumps(view))

    def test_exemption_moves_forward_at_the_next_trim(self):
        first = self.anth([ERR, ERR, OK, OK])
        self.assertEqual(results(first)["t0"], ERR[0])
        outs = [ERR, ERR, OK, OK, ERR, OK, OK]
        data = anth_request(anth_steps(outs))
        full = math.ceil(request_chars(data["system"], data["tools"], data["messages"]) / 2.5)
        later = results(self.anth(outs, trim_at_tokens=int(full * 0.6), trim_to_tokens=full))
        self.assertTrue(later["t0"].startswith(MASK_PREFIX))
        self.assertEqual((later["t1"], later["t4"]), (ERR[0], ERR[0]))
        # A result cleared at an earlier trim is never brought back.
        state = json.loads((self.ctx / "sessions" / f"{SID}.trim.json").read_text())
        self.assertEqual(next(iter(state["threads"].values()))["keep_errors"], ["t4", "t1"])

    def test_chat_has_no_clearing_so_the_bytes_stay(self):
        msgs = [{"role": "user", "content": "go"}]
        for k, (out, _) in enumerate([ERR, OK, OK]):
            msgs += [{"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{k}", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": f"c{k}", "content": out}]
        views = []
        for k in (0, 2):
            self.ctx = self.ctx.with_name(f"chat-{k}")
            views.append(self.run_req({"messages": copy.deepcopy(msgs), **headers()}, "acompletion", keep_errors=k,
                                      trim_at_tokens=10_000_000)["messages"])
        self.assertEqual(views[0], views[1])

    def test_masked_observations_too(self):
        # A second user turn the trim cuts at: its earlier exchanges are masked (CONTEXT_MASK_OBSERVATIONS).
        big = ("ok " * 3000, False)
        msgs = anth_steps([OK, ERR, OK, big, big, big])
        msgs[1:1] = [{"role": "assistant", "content": [{"type": "text", "text": "ready " * 400}]},
                     {"role": "user", "content": [{"type": "text", "text": "go"}]}]
        data = anth_request(msgs)
        full = math.ceil(request_chars(data["system"], data["tools"], data["messages"]) / 2.5)
        views = []
        for k in (0, 2):
            self.ctx = self.ctx.with_name(f"ctx-{k}")
            views.append(self.run_req(copy.deepcopy(data), clear_keep_results=0, mask_observations=True,
                                      trim_at_tokens=full, trim_to_tokens=full, keep_errors=k)["messages"])
        self.assertTrue(results(views[0])["t1"].startswith(MASK_PREFIX))
        r = results(views[1])
        self.assertEqual(r["t1"], ERR[0])
        self.assertTrue(r["t2"].startswith(MASK_PREFIX))


if __name__ == "__main__":
    unittest.main()
