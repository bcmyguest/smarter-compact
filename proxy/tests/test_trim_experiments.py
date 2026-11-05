"""Trim experiments: CONTEXT_STRIP_PROSE, CONTEXT_MASK_OBSERVATIONS and CONTEXT_PROGRESS_NOTE. Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import BIG, Base, anth_history, anth_request  # noqa: E402

from smarter_compact import Settings  # noqa: E402
from smarter_compact.adapters import AnthropicAdapter  # noqa: E402
from smarter_compact.core import MASK_PLACEHOLDER  # noqa: E402

# Step k of the in-flight turn: (tool name, input, result text, is_error).
STEPS = [
    ("Edit", {"file_path": "/src/f0.py", "old_string": "a", "new_string": "b"}, "edited", False),
    ("Bash", {"command": "pytest -q tests/"}, "Exit code 1\n1 failed", True),
    ("Write", {"file_path": "/src/f2.py", "content": "x"}, "written", False),
    ("Bash", {"command": "make build"}, "built", False),
    ("Read", {"file_path": "/src/f4.py"}, "contents", False),
]


def in_flight_turn(pad=""):
    """History, then a running turn: user prompt, STEPS as text + tool_use / tool_result (result + pad), and a
    latest run."""
    msgs = anth_history(2)
    msgs.append({"role": "user", "content": "now this"})
    for k, (name, args, result, error) in enumerate(STEPS):
        result += pad
        msgs.append({"role": "assistant", "content": [
            {"type": "text", "text": f"step {k}"},
            {"type": "tool_use", "id": f"s{k}", "name": name, "input": args}]})
        res = {"type": "tool_result", "tool_use_id": f"s{k}", "content": result}
        msgs.append({"role": "user", "content": [{**res, "is_error": True} if error else res]})
    msgs.append({"role": "assistant", "content": [
        {"type": "thinking", "thinking": "go", "signature": "SIGL=="},
        {"type": "tool_use", "id": "sL", "name": "Bash", "input": {"command": "ls"}}]})
    msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "sL", "content": "ok"}]})
    return msgs


def blocks(m):
    c = m["content"]
    return [{"type": "text", "text": c}] if isinstance(c, str) else c


def absent(msgs, view):
    """Messages of msgs none of whose non-thinking blocks reach the view."""
    sent = [b for m in view for b in blocks(m)]
    return sum(not any(b in sent for b in blocks(m) if b.get("type") != "thinking") for m in msgs)


def pointer_count(view):
    return int(re.search(r"; (\d+) earlier messages are NOT", view[2]["content"][0]["text"]).group(1))


def ids_in(view, kind, key):
    return {b.get(key) for m in view if isinstance(m.get("content"), list) for b in m["content"]
            if b.get("type") == kind}


class ProseStripTests(Base):
    def test_off_keeps_tool_pairs(self):
        msgs = in_flight_turn()
        out = self.run_req(anth_request(copy.deepcopy(msgs)), trim_at_tokens=1)
        on = self.run_req(anth_request(copy.deepcopy(msgs), sid="other"), trim_at_tokens=1, strip_prose=True)
        self.assertIn("s3", ids_in(out["messages"], "tool_use", "id"))
        self.assertNotEqual(len(out["messages"]), len(on["messages"]))

    def test_strips_pairs_before_bound_keeps_text_and_latest_run(self):
        msgs = in_flight_turn()
        view = self.run_req(anth_request(copy.deepcopy(msgs)), trim_at_tokens=1, strip_prose=True)["messages"]
        # The pair s3 sits wholly before the latest run's preceding message: gone from both sides.
        self.assertNotIn("s3", ids_in(view, "tool_use", "id"))
        self.assertNotIn("s3", ids_in(view, "tool_result", "tool_use_id"))
        # s4's result is the message right before the latest run: that pair stays whole.
        self.assertIn("s4", ids_in(view, "tool_use", "id"))
        self.assertIn("s4", ids_in(view, "tool_result", "tool_use_id"))
        self.assertEqual(ids_in(view, "tool_use", "id"), ids_in(view, "tool_result", "tool_use_id"))
        texts = [b.get("text") for m in view if isinstance(m.get("content"), list) for b in m["content"]]
        self.assertIn("step 3", texts)
        # The latest run and its result go out byte-identical.
        self.assertEqual(json.dumps(view[-2:]), json.dumps(msgs[-2:]))
        # Step 3's text merged into step 4's message, so roles still alternate.
        roles = [m["role"] for m in view]
        self.assertEqual(roles[0], "user")
        self.assertTrue(all(a != b for a, b in zip(roles, roles[1:])), roles)
        self.assertEqual([b.get("type") for b in view[-4]["content"]], ["text", "text", "tool_use"])

    def test_message_before_system_keeps_its_results(self):
        # TB2.1 probe: the API rejects a system message after an assistant one, so step 3's result must stay.
        msgs = in_flight_turn()
        at = next(i for i, m in enumerate(msgs) if m["role"] == "user" and "s3" in str(m["content"])) + 1
        msgs.insert(at, {"role": "system", "content": "reminder"})
        view = self.run_req(anth_request(copy.deepcopy(msgs)), trim_at_tokens=1, strip_prose=True)["messages"]
        self.assertIn("system", [m["role"] for m in view])
        self.assertIn("s3", ids_in(view, "tool_result", "tool_use_id"))
        for a, b in zip(view, view[1:]):
            if b["role"] == "system":
                self.assertEqual(a["role"], "user")

    def test_cut_note_untouched(self):
        msgs = in_flight_turn()
        off = self.run_req(anth_request(copy.deepcopy(msgs)), trim_at_tokens=1)["messages"]
        on = self.run_req(anth_request(copy.deepcopy(msgs), sid="other"), trim_at_tokens=1,
                          strip_prose=True)["messages"]
        self.assertEqual(off[2]["content"][1:], on[2]["content"][1:])
        self.assertEqual(on[2]["content"][-1], {"type": "text", "text": "now this"})

    def test_prefix_stable_on_next_request(self):
        msgs = in_flight_turn()
        first = self.run_req(anth_request(copy.deepcopy(msgs)), trim_at_tokens=1, strip_prose=True)["messages"]
        more = msgs + [{"role": "assistant", "content": [{"type": "tool_use", "id": "sM", "name": "Bash",
                                                          "input": {"command": "pwd"}}]},
                       {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "sM", "content": "/"}]}]
        second = self.run_req(anth_request(copy.deepcopy(more)), trim_at_tokens=BIG, strip_prose=True)["messages"]
        self.assertEqual(json.dumps(second[:len(first)]), json.dumps(first))


    def view_rows(self, msgs, view):
        """(kind, text) of every non-thinking row the view carries, the note left out."""
        a = AnthropicAdapter()
        names = a.tool_names(msgs)
        return sorted((r[1], r[3]) for m in view for r in a.rows(m, names)
                      if r[1] != "thinking" and not r[3].startswith("[smarter-compact:"))

    def stored_in_context(self):
        return sorted((r["kind"], r["text"]) for r in self.db_rows(where="in_context = 1 AND kind != 'thinking'"))

    def test_in_context_matches_view_after_strip(self):
        msgs = in_flight_turn()
        view = self.run_req(anth_request(copy.deepcopy(msgs)), trim_at_tokens=1, strip_prose=True)["messages"]
        self.assertEqual(self.stored_in_context(), self.view_rows(msgs, view))
        trimmed = [r["text"] for r in self.db_rows(where="in_context = 0 AND kind = 'tool_use'")]
        self.assertIn("make build", " ".join(trimmed))
        # A later untrimmed request keeps the same flags for the same rows.
        more = msgs + [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
        view = self.run_req(anth_request(copy.deepcopy(more)), trim_at_tokens=BIG, strip_prose=True)["messages"]
        self.assertEqual(self.stored_in_context(), self.view_rows(more, view))

    def test_pointer_counts_messages_absent_from_view(self):
        msgs = in_flight_turn()
        view = self.run_req(anth_request(copy.deepcopy(msgs)), trim_at_tokens=1, strip_prose=True)["messages"]
        self.assertEqual(pointer_count(view), absent(msgs, view))
        off = self.run_req(anth_request(copy.deepcopy(msgs), sid="other"), trim_at_tokens=1)["messages"]
        self.assertGreater(pointer_count(view), pointer_count(off))
        # The note's bytes stay put on the next request.
        more = msgs + [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
        again = self.run_req(anth_request(copy.deepcopy(more)), trim_at_tokens=BIG, strip_prose=True)["messages"]
        self.assertEqual(json.dumps(again[2]), json.dumps(view[2]))


LONG = "\n" + "x" * 300


def results_in(view):
    """{tool_use_id: content} of every tool_result block in the view."""
    return {b["tool_use_id"]: b["content"] for m in view if isinstance(m.get("content"), list) for b in m["content"]
            if b.get("type") == "tool_result"}


class MaskObservationsTests(Base):
    def mask(self, msgs, sid="sid-1", **kw):
        return self.run_req(anth_request(copy.deepcopy(msgs), sid=sid), mask_observations=True, **kw)["messages"]

    def test_off_by_default(self):
        view = self.run_req(anth_request(in_flight_turn(LONG)), trim_at_tokens=1)["messages"]
        self.assertNotIn(MASK_PLACEHOLDER, json.dumps(view))

    def test_masks_results_keeps_calls_text_and_latest_run(self):
        msgs = in_flight_turn(LONG)
        off = self.run_req(anth_request(copy.deepcopy(msgs), sid="other"), trim_at_tokens=1)["messages"]
        view = self.mask(msgs, trim_at_tokens=1)
        # Every call the off view carries stays, paired with a result.
        self.assertEqual(ids_in(view, "tool_use", "id"), ids_in(off, "tool_use", "id"))
        self.assertEqual(ids_in(view, "tool_use", "id"), ids_in(view, "tool_result", "tool_use_id"))
        results = results_in(view)
        self.assertEqual(results["s3"], MASK_PLACEHOLDER)
        # s4's result is the message right before the latest run: kept, as prose strip keeps it.
        self.assertEqual(results["s4"], "contents" + LONG)
        self.assertEqual(json.dumps(view[-2:]), json.dumps(msgs[-2:]))
        # Same messages and roles as without masking; only result contents differ.
        self.assertEqual([m["role"] for m in view], [m["role"] for m in off])
        self.assertEqual(results_in(off)["s3"], "built" + LONG)

    def test_keeps_is_error_and_short_results(self):
        # The gap keeps the newest 3 steps, so s3 is the one result in the masked region.
        msgs = in_flight_turn(LONG)
        at = next(i for i, m in enumerate(msgs) if m["role"] == "user" and "'s3'" in str(m["content"]))
        msgs[at]["content"][0]["is_error"] = True
        masked = next(b for m in self.mask(msgs, trim_at_tokens=1) if isinstance(m["content"], list)
                      for b in m["content"] if b.get("type") == "tool_result" and b.get("tool_use_id") == "s3")
        self.assertEqual(masked, {"type": "tool_result", "tool_use_id": "s3", "content": MASK_PLACEHOLDER,
                                  "is_error": True})
        msgs[at] = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "s3", "content": "short"}]}
        self.assertEqual(results_in(self.mask(msgs, sid="other", trim_at_tokens=1))["s3"], "short")

    def test_wins_over_prose_strip(self):
        msgs = in_flight_turn(LONG)
        both = self.mask(msgs, trim_at_tokens=1, strip_prose=True)
        alone = self.mask(msgs, sid="other", trim_at_tokens=1)
        self.assertEqual(json.dumps(both[3:]), json.dumps(alone[3:]))
        self.assertIn("s3", ids_in(both, "tool_use", "id"))

    def test_prefix_stable_on_next_request(self):
        msgs = in_flight_turn(LONG)
        first = self.mask(msgs, trim_at_tokens=1)
        more = msgs + [{"role": "assistant", "content": [{"type": "tool_use", "id": "sM", "name": "Bash",
                                                          "input": {"command": "pwd"}}]},
                       {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "sM", "content": "/"}]}]
        second = self.mask(more, trim_at_tokens=BIG)
        self.assertEqual(json.dumps(second[:len(first)]), json.dumps(first))
        # The latest run at trim time stays unmasked until the next trim.
        self.assertEqual(results_in(second)["sL"], "ok")

    def view_rows(self, msgs, view):
        """(kind, text) of every non-thinking row the view carries, the note and placeholders left out."""
        a = AnthropicAdapter()
        names = a.tool_names(msgs)
        return sorted((r[1], r[3]) for m in view for r in a.rows(m, names)
                      if r[1] != "thinking" and not r[3].startswith("[smarter-compact:"))

    def stored_in_context(self, sid):
        return sorted((r["kind"], r["text"]) for r in self.db_rows(sid, "in_context = 1 AND kind != 'thinking'"))

    def test_in_context_matches_view(self):
        msgs = in_flight_turn(LONG)
        view = self.mask(msgs, trim_at_tokens=1)
        self.assertEqual(self.stored_in_context("sid-1"), self.view_rows(msgs, view))
        # The call stays in context; its masked result's original text does not.
        kept = [r["text"] for r in self.db_rows("sid-1", "in_context = 1 AND kind = 'tool_use'")]
        self.assertIn("make build", " ".join(kept))
        out = [r["text"] for r in self.db_rows("sid-1", "in_context = 0 AND kind = 'tool_result'")]
        self.assertIn("built" + LONG, out)
        self.assertNotIn("contents" + LONG, out)
        more = msgs + [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
        view = self.mask(more, trim_at_tokens=BIG)
        self.assertEqual(self.stored_in_context("sid-1"), self.view_rows(more, view))

    def test_pointer_counts_and_names_recovery(self):
        msgs = in_flight_turn(LONG)
        view = self.mask(msgs, trim_at_tokens=1)
        off = self.run_req(anth_request(copy.deepcopy(msgs), sid="other"), trim_at_tokens=1)["messages"]
        prose = self.run_req(anth_request(copy.deepcopy(msgs), sid="third"), trim_at_tokens=1,
                             strip_prose=True)["messages"]
        # Masking empties no message, so the count is the cut's alone.
        self.assertEqual(pointer_count(view), pointer_count(off))
        self.assertLess(pointer_count(view), pointer_count(prose))
        note = view[2]["content"][0]["text"]
        self.assertIn('session_id="sid-1"', note.split("Older tool outputs")[1])
        self.assertNotIn("Older tool outputs", off[2]["content"][0]["text"])
        more = msgs + [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
        again = self.mask(more, trim_at_tokens=BIG)
        self.assertEqual(json.dumps(again[2]), json.dumps(view[2]))

    def test_short_results_masked_nowhere_and_no_recovery_line(self):
        view = self.mask(in_flight_turn(), trim_at_tokens=1)
        self.assertNotIn(MASK_PLACEHOLDER, json.dumps(view))
        self.assertNotIn("Older tool outputs", view[2]["content"][0]["text"])


class ProgressNoteTests(Base):
    def note(self, view):
        return view[2]["content"][0]["text"]

    def test_off_no_progress(self):
        view = self.run_req(anth_request(in_flight_turn()), trim_at_tokens=1)["messages"]
        self.assertNotIn("Progress before this trim", self.note(view))

    def test_lists_edited_files_and_build_commands(self):
        view = self.run_req(anth_request(in_flight_turn()), trim_at_tokens=1, progress_note=True)["messages"]
        note = self.note(view)
        self.assertIn("Files written or edited (latest last): /src/f0.py, /src/f2.py", note)
        self.assertIn("- `pytest -q tests/` -> exit 1", note)
        # Step 3 is kept whole without prose stripping, so its command is not in the note.
        self.assertNotIn("make build", note)

    def test_with_prose_includes_stripped_steps(self):
        view = self.run_req(anth_request(in_flight_turn()), trim_at_tokens=1, progress_note=True,
                            strip_prose=True)["messages"]
        self.assertIn("- `make build` -> ok", self.note(view))

    def test_note_bytes_stable_across_requests(self):
        msgs = in_flight_turn()
        first = self.run_req(anth_request(copy.deepcopy(msgs)), trim_at_tokens=1, progress_note=True)["messages"]
        more = msgs + [{"role": "assistant", "content": [{"type": "tool_use", "id": "sM", "name": "Write",
                                                          "input": {"file_path": "/src/new.py"}}]},
                       {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "sM", "content": "ok"}]}]
        second = self.run_req(anth_request(copy.deepcopy(more)), trim_at_tokens=BIG, progress_note=True)["messages"]
        self.assertEqual(json.dumps(second[2]), json.dumps(first[2]))


class ExperimentSettingsTests(unittest.TestCase):
    def test_env_flag_defaults(self):
        s = Settings.from_env({})
        self.assertEqual((s.strip_prose, s.progress_note), (False, True))
        self.assertEqual((Settings().strip_prose, Settings().progress_note), (False, True))
        s = Settings.from_env({"CONTEXT_STRIP_PROSE": "1", "CONTEXT_PROGRESS_NOTE": "off"})
        self.assertEqual((s.strip_prose, s.progress_note), (True, False))
        self.assertFalse(Settings.from_env({}).mask_observations)
        self.assertFalse(Settings().mask_observations)
        self.assertTrue(Settings.from_env({"CONTEXT_MASK_OBSERVATIONS": "1"}).mask_observations)
        self.assertFalse(Settings.from_env({"CONTEXT_MASK_OBSERVATIONS": ""}).mask_observations)
        self.assertTrue(Settings.from_env({"CONTEXT_STANDING_INSTRUCTION": ""}).standing_instruction)


if __name__ == "__main__":
    unittest.main()
