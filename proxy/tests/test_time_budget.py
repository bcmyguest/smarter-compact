"""CONTEXT_TIME_BUDGET_SECONDS: a "[TIME REMAINING: ...]" line on each request's new tool results, recorded and
re-rendered byte-identically. Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import SID, Base, anth_request, headers  # noqa: E402
from test_ledger import anth_steps  # noqa: E402

from smarter_compact import Settings, core, ledger  # noqa: E402

LS = ("Bash", {"command": "ls"}, "a.py\nb.py")
CAT = ("Bash", {"command": "cat a.py"}, "print(1)")
TEST = ("Bash", {"command": "pytest -q"}, "1 passed in 0.1s")
T0 = 1_000_000.0


def clock_lines(view):
    return [b["text"] for m in view if isinstance(m.get("content"), list) for b in m["content"]
            if isinstance(b, dict) and b.get("type") == "text" and b["text"].startswith("[TIME REMAINING:")]


class ClockBase(Base):
    def settings(self, **kw):
        kw.setdefault("time_budget_seconds", 1800)
        kw.setdefault("clear_keep_results", 0)
        kw.setdefault("strip_old_reasoning", False)
        return super().settings(**kw)

    def at(self, t, fn, *args, **kw):
        with mock.patch.object(core, "_clock", return_value=t):
            return fn(*args, **kw)

    def anth(self, msgs, t=T0, **kw):
        return self.at(t, self.run_req, anth_request(copy.deepcopy(msgs)), **kw)["messages"]

    def state(self):
        return json.loads((self.ctx / "sessions" / f"{SID}.trim.json").read_text())


class TextTests(unittest.TestCase):
    def test_format_follows_the_paper(self):
        self.assertEqual(core.clock_text(900, 146), "[TIME REMAINING: 12m34s of 15m budget]")
        self.assertEqual(core.clock_text(1800, 0), "[TIME REMAINING: 30m00s of 30m budget]")
        self.assertEqual(core.clock_text(930, 0.5), "[TIME REMAINING: 15m29s of 15m30s budget]")

    def test_floored_at_zero(self):
        self.assertEqual(core.clock_text(900, 5000), "[TIME REMAINING: 0m00s of 15m budget]")

    def test_setting(self):
        self.assertEqual(Settings.from_env({}).time_budget_seconds, 0)
        self.assertEqual(Settings.from_env({"CONTEXT_TIME_BUDGET_SECONDS": "1800"}).time_budget_seconds, 1800)
        self.assertEqual(Settings.from_env({"CONTEXT_TIME_BUDGET_SECONDS": "auto"}).time_budget_seconds, 0)
        self.assertEqual(Settings.from_env({"CONTEXT_TIME_BUDGET_SECONDS": "-5"}).time_budget_seconds, 0)


class AnthropicTests(ClockBase):
    def test_off_gives_the_old_bytes(self):
        for n in (1, 2):
            msgs = anth_steps([LS, CAT][:n])
            self.assertEqual(json.dumps(self.anth(msgs, time_budget_seconds=0)), json.dumps(msgs))
        path = self.ctx / "sessions" / f"{SID}.trim.json"
        self.assertTrue(not path.exists() or "clock_start" not in self.state())

    def test_recorded_lines_ignored_when_off(self):
        self.anth(anth_steps([LS]))
        msgs = anth_steps([LS, CAT])
        self.assertEqual(json.dumps(self.anth(msgs, time_budget_seconds=0)), json.dumps(msgs))

    def test_values_from_the_clock_and_start_at_first_request(self):
        # The start is the first request's whole second; 0.7 s in, 1799.3 s are left, shown floored.
        first = self.anth(anth_steps([LS]), t=T0 + 0.7)
        self.assertEqual(clock_lines(first), ["[TIME REMAINING: 29m59s of 30m budget]"])
        self.assertEqual(self.state()["clock_start"], int(T0))
        second = self.anth(anth_steps([LS, CAT]), t=T0 + 754.2)
        self.assertEqual(clock_lines(second), ["[TIME REMAINING: 29m59s of 30m budget]",
                                               "[TIME REMAINING: 17m25s of 30m budget]"])
        third = self.anth(anth_steps([LS, CAT, TEST]), t=T0 + 4000)
        self.assertEqual(clock_lines(third)[-1], "[TIME REMAINING: 0m00s of 30m budget]")

    def test_start_survives_a_restart(self):
        # The start lives in the session's trim state, not in process memory.
        self.anth(anth_steps([LS]), t=T0)
        self.anth(anth_steps([LS, CAT]), t=T0 + 600)
        self.assertEqual(self.state()["clock_start"], int(T0))

    def test_line_on_the_newest_result_and_byte_identical_later(self):
        first = self.anth(anth_steps([LS]), t=T0)
        msgs = anth_steps([LS])
        self.assertEqual(first[:2], msgs[:2])
        self.assertEqual(first[2]["content"][:-1], msgs[2]["content"])
        self.assertEqual(first[2]["content"][-1], {"type": "text", "text": "[TIME REMAINING: 30m00s of 30m budget]"})
        second = self.anth(anth_steps([LS, CAT]), t=T0 + 60)
        self.assertEqual(json.dumps(second[:3]), json.dumps(first))
        # A retry of the same request re-renders the recorded line, not a fresh value.
        again = self.anth(anth_steps([LS, CAT]), t=T0 + 300)
        self.assertEqual(json.dumps(again), json.dumps(second))
        self.assertEqual([d["at"] for d in self.state()["clock"]], [2, 4])

    def test_only_when_there_is_a_new_tool_result(self):
        msgs = anth_steps([LS])
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "done"}]})
        msgs.append({"role": "user", "content": [{"type": "text", "text": "thanks"}]})
        view = self.anth(msgs, t=T0)
        self.assertEqual(clock_lines(view), [])
        self.assertEqual(json.dumps(view), json.dumps(msgs))
        self.assertEqual(clock_lines(self.anth([{"role": "user", "content": "hi"}])), [])

    def test_never_in_the_system_prompt(self):
        data = self.at(T0, self.run_req, anth_request(anth_steps([LS])))
        self.assertEqual(data["system"], [{"type": "text", "text": "SYSTEM"}])

    def test_line_survives_a_trim_and_stays_out_of_its_note(self):
        steps = [LS, ("Bash", {"command": "ls -la"}, "x" * 400), CAT]
        for n in (1, 2):
            self.anth(anth_steps(steps[:n]), t=T0 + n)
        trimmed = self.anth(anth_steps(steps), t=T0 + 30, trim_at_tokens=1, clear_keep_results=1,
                            trim_to_tokens=10_000_000)
        self.assertTrue(trimmed[0]["content"][0]["text"].startswith("[smarter-compact: session"))
        lines = clock_lines(trimmed)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[-1], "[TIME REMAINING: 29m31s of 30m budget]")  # started at T0 + 1
        self.assertTrue(all(not b["text"].startswith("[smarter-compact") or "TIME REMAINING" not in b["text"]
                            for m in trimmed if isinstance(m.get("content"), list) for b in m["content"]
                            if b.get("type") == "text"))
        again = self.anth(anth_steps(steps), t=T0 + 90, trim_at_tokens=10_000_000, clear_keep_results=1)
        self.assertEqual(json.dumps(again), json.dumps(trimmed))

    def test_with_the_ledger_both_render_in_order(self):
        first = self.anth(anth_steps([CAT]), ledger=True)
        texts = [b["text"] for b in first[-1]["content"] if b.get("type") == "text"]
        self.assertTrue(texts[0].startswith(ledger.LEDGER_HEAD))
        self.assertTrue(texts[-1].startswith("[TIME REMAINING:"))
        second = self.anth(anth_steps([CAT, LS]), t=T0 + 10, ledger=True)
        self.assertEqual(json.dumps(second[:3]), json.dumps(first))


class OtherFormatTests(ClockBase):
    def chat(self, steps):
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "the task"}]
        for k, (name, args, out) in enumerate(steps):
            msgs.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{k}", "type": "function", "function": {"name": name.lower(), "arguments": json.dumps(args)}}]})
            msgs.append({"role": "tool", "tool_call_id": f"c{k}", "content": out})
        return msgs

    def responses(self, steps):
        items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "the task"}]}]
        for k, (name, args, out) in enumerate(steps):
            items.append({"type": "function_call", "call_id": f"r{k}", "name": name.lower(),
                          "arguments": json.dumps(args)})
            items.append({"type": "function_call_output", "call_id": f"r{k}", "output": out})
        return items

    def req(self, key, items, call_type, t, **kw):
        return self.at(t, self.run_req, {key: copy.deepcopy(items), **headers()}, call_type, **kw)[key]

    def test_chat(self):
        outs = [self.req("messages", self.chat([LS, CAT][:n]), "acompletion", T0 + 61 * n) for n in (1, 2)]
        self.assertEqual(outs[0][-1]["content"], "a.py\nb.py\n\n[TIME REMAINING: 30m00s of 30m budget]")
        self.assertEqual(json.dumps(outs[1][:len(outs[0])]), json.dumps(outs[0]))
        self.assertEqual(outs[1][-1]["content"], "print(1)\n\n[TIME REMAINING: 28m59s of 30m budget]")
        self.ctx = self.ctx.with_name("ctx-off")
        off = self.req("messages", self.chat([LS, CAT]), "acompletion", T0, time_budget_seconds=0)
        self.assertEqual(off, self.chat([LS, CAT]))

    def test_responses(self):
        outs = [self.req("input", self.responses([LS, CAT][:n]), "aresponses", T0 + 61 * n) for n in (1, 2)]
        self.assertEqual(outs[0][-1]["output"], "a.py\nb.py\n\n[TIME REMAINING: 30m00s of 30m budget]")
        self.assertEqual(json.dumps(outs[1][:len(outs[0])]), json.dumps(outs[0]))
        self.assertEqual(outs[1][-1]["output"], "print(1)\n\n[TIME REMAINING: 28m59s of 30m budget]")
        self.ctx = self.ctx.with_name("ctx-off")
        off = self.req("input", self.responses([LS, CAT]), "aresponses", T0, time_budget_seconds=0)
        self.assertEqual(off, self.responses([LS, CAT]))


if __name__ == "__main__":
    unittest.main()
