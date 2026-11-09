"""CONTEXT_LEDGER: files read and changed appended to new tool results; repeated reads pointed at or nudged.
Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import SID, Base, anth_request, headers  # noqa: E402

from smarter_compact import Settings, ttl  # noqa: E402
from smarter_compact import ledger  # noqa: E402
from smarter_compact.compress import DEDUP_PREFIX, MASK_PREFIX, blob_id, dedup_placeholder  # noqa: E402

FILE = "\n".join(f"line {k} of a.py with some text" for k in range(1, 41))  # cat output, 1,200+ chars
NUMBERED = "\n".join(f"{k:>6}\t" + f"line {k} of a.py with some text" for k in range(5, 16))


def anth_steps(steps, task="the task"):
    """The task, then per step one assistant tool call and its result: steps = [(name, args, result)]."""
    msgs = [{"role": "user", "content": [{"type": "text", "text": task}]}]
    for k, (name, args, out) in enumerate(steps):
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{k}", "name": name, "input": args}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{k}", "content": out}]})
    return msgs


def results(view):
    return {b["tool_use_id"]: b["content"] for m in view if isinstance(m.get("content"), list)
            for b in m["content"] if b.get("type") == "tool_result"}


def ledger_texts(view):
    return [b["text"] for m in view if isinstance(m.get("content"), list) for b in m["content"]
            if b.get("type") == "text" and b["text"].startswith(ledger.LEDGER_HEAD)]


CAT = ("Bash", {"command": "cat a.py"}, FILE)
READ_RANGE = ("Read", {"file_path": "a.py", "offset": 5, "limit": 11}, NUMBERED)
EDIT = ("Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"}, "The file a.py has been updated.")
TEST = ("Bash", {"command": "pytest -q tests"}, "1 passed in 0.1s")


class LedgerBase(Base):
    def settings(self, **kw):
        kw.setdefault("ledger", True)
        kw.setdefault("clear_keep_results", 0)
        kw.setdefault("strip_old_reasoning", False)
        return super().settings(**kw)

    def anth(self, msgs, **kw):
        return self.run_req(anth_request(copy.deepcopy(msgs)), **kw)["messages"]

    def grow(self, steps, **kw):
        """Send the history step by step, as an agent would; returns every view."""
        return [self.anth(anth_steps(steps[:n]), **kw) for n in range(1, len(steps) + 1)]


class WalkTests(unittest.TestCase):
    def steps(self, calls):
        return [(k, f"c{k}", name, args, out, False) for k, (name, args, out) in enumerate(calls)]

    def test_effects(self):
        self.assertEqual(ledger.effects("Read", {"file_path": "./a.py"}), [("read", "a.py", 1, None, "read")])
        self.assertEqual(ledger.effects("Bash", {"command": "sed -n '10,20p' a.py"}), [("read", "a.py", 10, 20, "sed")])
        self.assertEqual(ledger.effects("bash", {"command": "head -n 5 a.py"}), [("read", "a.py", 1, 5, "head")])
        self.assertEqual(ledger.effects("Bash", {"command": "tail -20 a.py"}), [("read", "a.py", None, 20, "tail")])
        self.assertEqual(ledger.effects("Bash", {"command": "echo x > b.py"}), [("edit", "b.py"), ("global",)])
        self.assertEqual(ledger.effects("Bash", {"command": "sed -i 's/a/b/' c.py"}), [("edit", "c.py"), ("global",)])
        self.assertEqual(ledger.effects("Bash", {"command": "pytest -q"}), [("test", "pytest -q")])
        self.assertEqual(ledger.effects("apply_patch", {"input": "*** Begin Patch\n*** Update File: d.py\n@@"}),
                         [("edit", "d.py")])
        self.assertEqual(ledger.effects("shell", {"command": ["bash", "-lc", "cat e.py"]}),
                         [("read", "e.py", 1, None, "cat")])
        self.assertEqual(ledger.effects("Bash", {"command": "cat a.py | grep x"}), [])

    def test_range_covered_only_when_current(self):
        _, repeats = ledger.walk(self.steps([CAT, READ_RANGE]), 1)
        self.assertEqual([(r["rid"], r["covers"]) for r in repeats], [("c1", [(0, "c0")])])
        _, repeats = ledger.walk(self.steps([CAT, EDIT, READ_RANGE]), 2)
        self.assertEqual(repeats, [])
        # Any shell command that may write invalidates every observation; a test run does not.
        _, repeats = ledger.walk(self.steps([CAT, ("Bash", {"command": "python gen.py"}, ""), READ_RANGE]), 2)
        self.assertEqual(repeats, [])
        _, repeats = ledger.walk(self.steps([CAT, TEST, READ_RANGE]), 2)
        self.assertEqual(len(repeats), 1)

    def test_partial_read_does_not_cover_more(self):
        wider = ("Bash", {"command": "sed -n '1,30p' a.py"}, FILE)
        _, repeats = ledger.walk(self.steps([READ_RANGE, wider]), 1)
        self.assertEqual(repeats, [])
        tail = ("Bash", {"command": "tail -n 5 a.py"}, FILE)
        self.assertEqual(ledger.walk(self.steps([CAT, tail]), 1)[1], [])
        truncated = ("Bash", {"command": "cat a.py"}, FILE + "\n[... 300 characters truncated ...]")
        self.assertEqual(ledger.walk(self.steps([truncated, READ_RANGE]), 1)[1], [])

    def test_test_rerun_without_edit(self):
        _, repeats = ledger.walk(self.steps([TEST, TEST]), 1)
        self.assertEqual(repeats, [{"rid": "c1", "j": 1, "test": "pytest -q tests"}])
        self.assertEqual(ledger.walk(self.steps([TEST, EDIT, TEST]), 2)[1], [])

    def test_block_lists_reads_and_edits_with_status(self):
        state, _ = ledger.walk(self.steps([CAT, EDIT, READ_RANGE]), 3)
        text = ledger.block(state)
        self.assertEqual(text.split("\n"), [ledger.LEDGER_HEAD, "Modified (latest last): a.py", "Read (latest last):",
                                            "- a.py: whole stale; lines 5-15 current"])

    def test_block_cap(self):
        calls = [("Read", {"file_path": f"src/module_{k:03}.py"}, "x") for k in range(200)]
        text = ledger.block(ledger.walk(self.steps(calls), 200)[0])
        self.assertLessEqual(len(text), ledger.LEDGER_MAX_CHARS)
        self.assertIn("earlier files not listed", text)
        self.assertTrue(text.endswith("src/module_199.py: whole current"))


class SettingTests(unittest.TestCase):
    def test_default_on(self):
        self.assertTrue(Settings.from_env({}).ledger)
        self.assertFalse(Settings.from_env({"CONTEXT_LEDGER": "0"}).ledger)


class AnthropicTests(LedgerBase):
    def test_off_gives_the_old_bytes(self):
        steps = [CAT, READ_RANGE]
        for view, msgs in zip(self.grow(steps, ledger=False), [anth_steps(steps[:1]), anth_steps(steps)]):
            self.assertEqual(json.dumps(view), json.dumps(msgs))
        # Recorded blocks and pointers are ignored with the setting off.
        self.ctx = self.ctx.with_name("ctx-2")
        self.grow(steps)
        self.assertEqual(json.dumps(self.anth(anth_steps(steps), ledger=False)), json.dumps(anth_steps(steps)))

    def test_block_on_the_newest_result_and_byte_identical_later(self):
        first, second = self.grow([CAT, EDIT])
        msgs = anth_steps([CAT])
        self.assertEqual(first[:2], msgs[:2])
        self.assertEqual(first[2]["content"][0], msgs[2]["content"][0])
        [block] = ledger_texts(first)
        self.assertIn("- a.py: whole current", block)
        self.assertEqual(json.dumps(second[:3]), json.dumps(first))
        self.assertIn("a.py: whole stale", ledger_texts(second)[-1])
        state = json.loads((self.ctx / "sessions" / f"{SID}.trim.json").read_text())
        self.assertEqual([d["at"] for d in state["ledger"]], [2, 4])

    def test_unchanged_block_is_not_repeated(self):
        views = self.grow([CAT, ("Bash", {"command": "ls"}, "a.py")])
        self.assertEqual(len(ledger_texts(views[-1])), 1)

    def test_covered_read_with_different_output_stays(self):
        # The range read is covered by the whole cat, but its output differs: no pointer, no nudge.
        views = self.grow([CAT, READ_RANGE])
        self.assertEqual(results(views[-1])["t1"], NUMBERED)
        self.assertEqual(results(views[-1])["t0"], FILE)
        self.assertNotIn("Repeats in this batch", ledger_texts(views[-1])[-1])
        later = self.anth(anth_steps([CAT, READ_RANGE, TEST]))
        self.assertEqual(json.dumps(later[:len(views[-1])]), json.dumps(views[-1]))

    def test_changed_reread_stays_whole(self):
        # Same command, no edit since, different output: the ledger never claims it unchanged.
        views = self.grow([CAT, ("Bash", {"command": "cat a.py"}, FILE + "\nline 41 appended")], dedup_results=False)
        self.assertEqual(results(views[-1])["t1"], FILE + "\nline 41 appended")
        self.assertNotIn("Repeats in this batch", ledger_texts(views[-1])[-1])

    def test_identical_reread_becomes_the_dedup_pointer(self):
        # With CONTEXT_DEDUP_RESULTS off the ledger makes the pointer; on, dedup does: the same bytes either way.
        views = self.grow([CAT, CAT], dedup_results=False)
        self.assertEqual(results(views[-1])["t1"], dedup_placeholder(FILE))
        self.assertTrue(dedup_placeholder(FILE).startswith(DEDUP_PREFIX))
        state = json.loads((self.ctx / "sessions" / f"{SID}.trim.json").read_text())
        self.assertEqual([(d["kind"], d["at"], d["id"], d["src"], d["bid"]) for d in state["dedup"]],
                         [("reuse", 4, "t1", "t0", blob_id(FILE))])
        self.assertEqual((self.ctx / "blobs" / f"{blob_id(FILE)}.txt").read_text(), FILE)
        ttl.sweep_orphan_blobs(self.ctx)
        self.assertTrue((self.ctx / "blobs" / f"{blob_id(FILE)}.txt").exists())
        later = self.anth(anth_steps([CAT, CAT, TEST]), dedup_results=False)
        self.assertEqual(json.dumps(later[:len(views[-1])]), json.dumps(views[-1]))
        self.ctx = self.ctx.with_name("ctx-dedup")
        on = self.grow([CAT, CAT])
        self.assertEqual(json.dumps(on), json.dumps(views))

    def test_stale_read_stays(self):
        views = self.grow([CAT, EDIT, READ_RANGE])
        self.assertEqual(results(views[-1])["t2"], NUMBERED)
        self.assertNotIn("Repeats in this batch", ledger_texts(views[-1])[-1])

    def test_read_of_a_cleared_copy_is_nudged(self):
        steps = [CAT, ("Bash", {"command": "ls"}, "b" * 400), ("Bash", {"command": "ls -la"}, "c" * 400), READ_RANGE]
        self.grow(steps[:3])
        view = self.anth(anth_steps(steps), trim_at_tokens=1100, clear_keep_results=2, trim_to_tokens=1100)
        self.assertTrue(results(view)["t0"].startswith(MASK_PREFIX))
        self.assertEqual(results(view)["t3"], NUMBERED)
        self.assertIn("a.py (lines 5-15) was read before and is unchanged since", ledger_texts(view)[-1])

    def test_test_rerun_nudged(self):
        view = self.grow([TEST, TEST])[-1]
        self.assertIn("`pytest -q tests` already ran with no file change since", ledger_texts(view)[-1])

    def test_trim_keeps_only_the_newest_block(self):
        steps = [CAT, EDIT, ("Read", {"file_path": "b.py"}, "b" * 300), ("Bash", {"command": "ls"}, "x" * 300)]
        self.grow(steps[:3])
        before = self.anth(anth_steps(steps[:3]))
        self.assertEqual(len(ledger_texts(before)), 3)
        trimmed = self.anth(anth_steps(steps), trim_at_tokens=1, clear_keep_results=1, trim_to_tokens=10_000_000)
        self.assertEqual(len(ledger_texts(trimmed)), 1)
        again = self.anth(anth_steps(steps), trim_at_tokens=10_000_000, clear_keep_results=1)
        self.assertEqual(json.dumps(again), json.dumps(trimmed))


class OtherFormatTests(LedgerBase):
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
            items.append({"type": "function_call", "call_id": f"r{k}", "name": name.lower(), "arguments": json.dumps(args)})
            items.append({"type": "function_call_output", "call_id": f"r{k}", "output": out})
        return items

    def test_chat(self):
        outs = [self.run_req({"messages": self.chat([CAT, READ_RANGE][:n]), **headers()}, "acompletion")["messages"]
                for n in (1, 2)]
        self.assertTrue(outs[0][-1]["content"].startswith(FILE + "\n\n" + ledger.LEDGER_HEAD))
        self.assertEqual(json.dumps(outs[1][:len(outs[0])]), json.dumps(outs[0]))
        self.assertTrue(outs[1][-1]["content"].startswith(NUMBERED + "\n\n" + ledger.LEDGER_HEAD))
        off = self.run_req({"messages": self.chat([CAT, READ_RANGE]), **headers()}, "acompletion", ledger=False)
        self.assertEqual(off["messages"], self.chat([CAT, READ_RANGE]))

    def test_responses(self):
        outs = [self.run_req({"input": self.responses([CAT, READ_RANGE][:n]), **headers()}, "aresponses")["input"]
                for n in (1, 2)]
        self.assertTrue(outs[0][-1]["output"].startswith(FILE + "\n\n" + ledger.LEDGER_HEAD))
        self.assertEqual(json.dumps(outs[1][:len(outs[0])]), json.dumps(outs[0]))
        self.assertTrue(outs[1][-1]["output"].startswith(NUMBERED + "\n\n" + ledger.LEDGER_HEAD))
        off = self.run_req({"input": self.responses([CAT, READ_RANGE]), **headers()}, "aresponses", ledger=False)
        self.assertEqual(off["input"], self.responses([CAT, READ_RANGE]))


if __name__ == "__main__":
    unittest.main()
