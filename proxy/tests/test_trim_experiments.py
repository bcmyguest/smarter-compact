"""Opt-in trim experiments: CONTEXT_STRIP_PROSE and CONTEXT_PROGRESS_NOTE. Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import BIG, Base, anth_history, anth_request  # noqa: E402

from smarter_compact import Settings  # noqa: E402

# Step k of the in-flight turn: (tool name, input, result text, is_error).
STEPS = [
    ("Edit", {"file_path": "/src/f0.py", "old_string": "a", "new_string": "b"}, "edited", False),
    ("Bash", {"command": "pytest -q tests/"}, "Exit code 1\n1 failed", True),
    ("Write", {"file_path": "/src/f2.py", "content": "x"}, "written", False),
    ("Bash", {"command": "make build"}, "built", False),
    ("Read", {"file_path": "/src/f4.py"}, "contents", False),
]


def in_flight_turn():
    """History, then a running turn: user prompt, STEPS as text + tool_use / tool_result, and a latest run."""
    msgs = anth_history(2)
    msgs.append({"role": "user", "content": "now this"})
    for k, (name, args, result, error) in enumerate(STEPS):
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
    def test_env_flags_default_on(self):
        s = Settings.from_env({})
        self.assertEqual((s.strip_prose, s.progress_note), (True, True))
        self.assertEqual((Settings().strip_prose, Settings().progress_note), (True, True))
        s = Settings.from_env({"CONTEXT_STRIP_PROSE": "0", "CONTEXT_PROGRESS_NOTE": "off"})
        self.assertEqual((s.strip_prose, s.progress_note), (False, False))
        self.assertTrue(Settings.from_env({"CONTEXT_STANDING_INSTRUCTION": ""}).standing_instruction)


if __name__ == "__main__":
    unittest.main()
