"""CONTEXT_LOG_REDUCE_OVER_CHARS: a new test/build tool result keeps its failure lines and its tail.
Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import SID, Base, anth_request, headers  # noqa: E402

from smarter_compact import Settings, ttl  # noqa: E402
from smarter_compact.compress import (  # noqa: E402
    LOG_TAIL_LINES,
    MARKER_PREFIX,
    blob_id,
    compressed_blob_id,
    is_log,
    reduce_log,
)

PASS = [f"tests/test_mod.py::test_case_{k} PASSED" for k in range(300)]
FAIL_BLOCK = ["", "    def test_boom():", ">       assert 1 == 2", "E       assert 1 == 2", "", "tests/test_x.py:12: AssertionError"]
LOG = "\n".join(["============================= test session starts ==============================", *PASS[:150],
                 "tests/test_x.py::test_boom FAILED", *PASS[150:], "=================================== FAILURES ===",
                 *FAIL_BLOCK, "=========================== short test summary info ============================",
                 "FAILED tests/test_x.py::test_boom - assert 1 == 2", "=== 1 failed, 300 passed in 2.10s ==="])
PROSE = "plain text with no log signature\n" * 400


def anth_turns(outs, command="pytest -q"):
    msgs = [{"role": "user", "content": [{"type": "text", "text": "the task"}]}]
    for k, out in enumerate(outs):
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{k}", "name": "Bash", "input": {"command": command}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{k}", "content": out}]})
    return msgs


def results(view):
    return {b["tool_use_id"]: b["content"] for m in view if isinstance(m.get("content"), list)
            for b in m["content"] if b.get("type") == "tool_result"}


class LogBase(Base):
    def settings(self, **kw):
        kw.setdefault("log_reduce_over_chars", 6000)
        kw.setdefault("clear_keep_results", 0)
        kw.setdefault("strip_old_reasoning", False)
        return super().settings(**kw)

    def anth(self, msgs, **kw):
        return self.run_req(anth_request(copy.deepcopy(msgs)), **kw)["messages"]


class ReducerTests(unittest.TestCase):
    def test_keeps_failure_windows_and_tail(self):
        reduced, bid = reduce_log(LOG, 6000)
        self.assertEqual(bid, blob_id(LOG))
        self.assertTrue(reduced.startswith(MARKER_PREFIX))
        self.assertEqual(compressed_blob_id(reduced), bid)
        self.assertIn("tests/test_x.py::test_boom FAILED", reduced)
        self.assertIn(">       assert 1 == 2", reduced)
        self.assertTrue(reduced.endswith("=== 1 failed, 300 passed in 2.10s ==="))
        self.assertLess(len(reduced), len(LOG) // 4)
        # 3 lines before and 8 after the failure line, the rest elided with a count.
        body = reduced.split("\n")
        at = body.index("tests/test_x.py::test_boom FAILED")
        self.assertEqual(body[at - 3:at], PASS[147:150])
        self.assertEqual(body[at + 1:at + 9], PASS[150:158])
        self.assertTrue(body[at - 4].startswith("… ") and body[at - 4].endswith(" lines …"))

    def test_tail_is_the_last_lines(self):
        reduced, _ = reduce_log(LOG, 6000)
        self.assertEqual(reduced.split("\n")[-LOG_TAIL_LINES:], LOG.split("\n")[-LOG_TAIL_LINES:])

    def test_identical_lines_fold(self):
        text = "\n".join(["make: Entering directory", *["warning: deprecated thing failed"] * 500, "done"])
        reduced, _ = reduce_log(text, 100)
        self.assertIn("warning: deprecated thing failed ×", reduced)
        self.assertLess(len(reduced), 2000)

    def test_not_a_log_or_small_or_off(self):
        self.assertFalse(is_log(PROSE))
        self.assertIsNone(reduce_log(PROSE, 100))
        self.assertIsNone(reduce_log(LOG, len(LOG)))
        self.assertIsNone(reduce_log(LOG, 0))
        self.assertIsNone(reduce_log(reduce_log(LOG, 100)[0], 100))

    def test_signatures(self):
        for text in ("Ran 12 tests in 0.3s", "--- FAIL: TestX (0.00s)", "test result: FAILED. 1 passed",
                     "src/a.c:3:4: error: x", "src/a.ts(1,2): error TS2322: y", "BUILD FAILED in 3s",
                     "make[1]: *** [all] Error 2", "Tests:       1 failed, 2 passed"):
            self.assertTrue(is_log(text), text)


class SettingTests(unittest.TestCase):
    def test_default_off(self):
        self.assertEqual(Settings.from_env({}).log_reduce_over_chars, 0)
        self.assertEqual(Settings.from_env({"CONTEXT_LOG_REDUCE_OVER_CHARS": "6000"}).log_reduce_over_chars, 6000)
        self.assertEqual(Settings.from_env({"CONTEXT_LOG_REDUCE_OVER_CHARS": "-1"}).log_reduce_over_chars, 0)


class AnthropicTests(LogBase):
    def test_off_gives_the_old_bytes(self):
        msgs = anth_turns([LOG])
        self.assertEqual(json.dumps(self.anth(msgs, log_reduce_over_chars=0)), json.dumps(msgs))
        self.anth(msgs)
        # A recorded decision is ignored with the setting off.
        self.assertEqual(json.dumps(self.anth(msgs, log_reduce_over_chars=0)), json.dumps(msgs))

    def test_new_result_reduced_and_blob_written(self):
        msgs = anth_turns([LOG])
        view = self.anth(msgs)
        self.assertEqual(view[:2], msgs[:2])
        reduced = results(view)["t0"]
        self.assertEqual(reduced, reduce_log(LOG, 6000)[0])
        self.assertEqual((self.ctx / "blobs" / f"{blob_id(LOG)}.txt").read_text(), LOG)
        state = json.loads((self.ctx / "sessions" / f"{SID}.trim.json").read_text())
        self.assertEqual([(d["at"], d["id"], d["bid"]) for d in state["logs"]], [(2, "t0", blob_id(LOG))])

    def test_byte_identical_on_later_requests(self):
        first = self.anth(anth_turns([LOG]))
        later = self.anth(anth_turns([LOG, "ok " * 10]))
        self.assertEqual(json.dumps(later[:len(first)]), json.dumps(first))

    def test_only_new_results_change(self):
        # A log already sent whole (the setting was off) stays whole.
        msgs = anth_turns([LOG, "ok " * 10])
        self.anth(msgs[:3], log_reduce_over_chars=0)
        self.assertEqual(results(self.anth(msgs))["t0"], LOG)

    def test_before_head_tail_compression(self):
        big = LOG + "\n" + "\n".join(PASS * 3)
        view = self.anth(anth_turns([big]), compress_over_tokens=4000)
        self.assertIn("test/build log of", results(view)["t0"])
        # A long result that is no log still gets head/tail compression.
        view = self.anth(anth_turns([PROSE * 3], command="cat notes.txt"), compress_over_tokens=4000)
        self.assertIn("tokens — smart_compact_get", results(view)["t0"])

    def test_stored_row_keeps_full_text_and_sweep_keeps_blob(self):
        self.anth(anth_turns([LOG]))
        rows = [r for r in self.db_rows() if r["kind"] == "tool_result"]
        self.assertEqual((rows[0]["text"], rows[0]["blob_id"]), (LOG, blob_id(LOG)))
        ttl.sweep_orphan_blobs(self.ctx)
        self.assertTrue((self.ctx / "blobs" / f"{blob_id(LOG)}.txt").exists())


class OtherFormatTests(LogBase):
    def test_chat(self):
        msgs = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c0", "type": "function", "function": {"name": "bash", "arguments": "{\"command\":\"pytest\"}"}}]},
                {"role": "tool", "tool_call_id": "c0", "content": LOG}]
        out = self.run_req({"messages": copy.deepcopy(msgs), **headers()}, "acompletion")["messages"]
        self.assertEqual(out[-1]["content"], reduce_log(LOG, 6000)[0])
        again = self.run_req({"messages": copy.deepcopy(msgs) + [{"role": "user", "content": "next"}], **headers()},
                             "acompletion")["messages"]
        self.assertEqual(again[:3], out)
        off = self.run_req({"messages": copy.deepcopy(msgs), **headers()}, "acompletion", log_reduce_over_chars=0)
        self.assertEqual(off["messages"], msgs)

    def test_responses(self):
        items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]},
                 {"type": "function_call", "call_id": "r0", "name": "shell", "arguments": "{\"cmd\":\"pytest\"}"},
                 {"type": "function_call_output", "call_id": "r0", "output": LOG}]
        out = self.run_req({"input": copy.deepcopy(items), **headers()}, "aresponses")["input"]
        self.assertEqual(out[-1]["output"], reduce_log(LOG, 6000)[0])
        off = self.run_req({"input": copy.deepcopy(items), **headers()}, "aresponses", log_reduce_over_chars=0)
        self.assertEqual(off["input"], items)


if __name__ == "__main__":
    unittest.main()
