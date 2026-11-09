"""Where proxy notes go: a system-role message when the client already sends mid-conversation system messages
(Claude Code), else inline wrapped in <system-reminder> tags; never as bare user text.
Run: python3 -m unittest discover -s proxy/tests"""

import asyncio
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_context_proxy import SID, Base, anth_history, anth_request, chat_history, headers  # noqa: E402
from test_live_shape import _hook_module  # noqa: E402

from smarter_compact.adapters import NOTES_KEY, AnthropicAdapter, wrap_note  # noqa: E402
from smarter_compact.compress import MASK_PREFIX  # noqa: E402
from smarter_compact.core import CLEARED_LEAD, SCRATCHPAD_LABEL, lift_notes, render  # noqa: E402
from smarter_compact.ledger import LEDGER_HEAD  # noqa: E402
from smarter_compact.tokens import request_chars  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
FILE = "\n".join(f"line {k} of a.py with some text" for k in range(1, 41))
OPEN = "<system-reminder>\n"


def tt(n):
    return {"role": "system", "content": [{"type": "text", "text": f"<total_tokens>{n} tokens left</total_tokens>"}]}


def cc_history(turns, filler=200):
    """anth_history as Claude Code sends it: a system message after the task and after every user message."""
    msgs = []
    for k, m in enumerate(anth_history(turns, filler)):
        msgs.append(m)
        if m["role"] == "user":
            msgs.append(tt(1000 - k))
    return msgs


def texts(m):
    c = m.get("content")
    return [c] if isinstance(c, str) else [b.get("text") for b in c or () if isinstance(b, dict) and "text" in b]


def user_texts(view):
    return [t for m in view if m.get("role") in ("user", "tool") for t in texts(m) if isinstance(t, str)]


def system_texts(view, role="system"):
    return [t for m in view if m.get("role") == role for t in texts(m) if isinstance(t, str)]


class SystemRoleTests(Base):
    """Claude Code shape: the request carries mid-conversation system messages, so notes become system text."""

    def test_trim_note_is_system_text_after_the_cut_turn(self):
        msgs = cc_history(5)
        view = self.run_req(anth_request(copy.deepcopy(msgs)), trim_at_tokens=1)["messages"]
        notes = [t for t in system_texts(view) if t.startswith("[smarter-compact: continuing session")]
        self.assertEqual(len(notes), 1)
        self.assertFalse(any("[smarter-compact" in t for t in user_texts(view)))
        self.assertNotIn(OPEN + "[smarter-compact", json.dumps(view))
        self.assertNotIn(NOTES_KEY, json.dumps(view))
        # The note joins the client's system message right after the cut turn: no two system messages side by side.
        at = next(j for j, m in enumerate(view) if notes[0] in texts(m))
        self.assertEqual(view[at - 1]["role"], "user")
        self.assertEqual(texts(view[at])[0], texts(msgs[msgs.index(view[at - 1]) + 1])[0])
        self.assertEqual(view[at + 1]["role"], "assistant")

    def test_trim_note_re_renders_byte_identically_until_the_next_trim(self):
        msgs = cc_history(5)
        first = self.run_req(anth_request(copy.deepcopy(msgs)), trim_at_tokens=1, trim_to_tokens=1)["messages"]
        later = msgs + [{"role": "assistant", "content": [{"type": "text", "text": "more"}]},
                        {"role": "user", "content": [{"type": "text", "text": "go on"}]}, tt(1)]
        second = self.run_req(anth_request(copy.deepcopy(later)))["messages"]
        self.assertEqual(json.dumps(second[:len(first)]), json.dumps(first))

    def test_a_dropped_anchor_drops_its_note_and_client_system_messages_stay(self):
        a = AnthropicAdapter()
        a.note_role = "system"
        msgs = cc_history(3)
        # A note on the first turn's tool results (index 3); the cut at turn 2 drops that turn.
        noted = [*msgs[:3], a.appended_note(msgs[3], "NOTE ON A DROPPED TURN"), *msgs[4:]]
        cut = next(j for j, m in enumerate(msgs) if m["role"] == "user" and "question 2" in texts(m)[0])
        view = lift_notes(a, render(a, noted, cut, "TRIM NOTE", first=0))
        dumped = json.dumps(view)
        self.assertNotIn("NOTE ON A DROPPED TURN", dumped)
        self.assertEqual(system_texts(view).count("TRIM NOTE"), 1)
        # Every client system message survives, the dropped turns' too (pinned).
        self.assertEqual([m for m in view if m["role"] == "system" and "TRIM NOTE" not in texts(m)],
                         [m for m in msgs if m["role"] == "system" and msgs.index(m) != cut + 1])
        self.assertNotIn(NOTES_KEY, dumped)

    def test_a_kept_anchor_keeps_its_note_in_the_next_system_message(self):
        a = AnthropicAdapter()
        a.note_role = "system"
        msgs = cc_history(2)
        noted = [*msgs[:3], a.appended_note(msgs[3], "KEPT NOTE"), *msgs[4:]]
        view = lift_notes(a, render(a, noted, None, None))
        self.assertEqual(len(view), len(msgs))
        self.assertEqual(view[3], msgs[3])
        self.assertEqual(texts(view[4]), [*texts(msgs[4]), "KEPT NOTE"])

    def test_a_note_without_a_following_system_message_gets_its_own(self):
        a = AnthropicAdapter()
        a.note_role = "system"
        msgs = anth_history(2)
        noted = [*msgs[:2], a.appended_note(msgs[2], "NOTE"), *msgs[3:]]
        view = lift_notes(a, noted)
        self.assertEqual(view[:3], msgs[:3])
        self.assertEqual(view[3], {"role": "system", "content": [{"type": "text", "text": "NOTE"}]})
        self.assertEqual(view[4:], msgs[3:])

    def test_ledger_block_is_system_text_and_stored_rows_hold_no_note(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "the task"}]}, tt(9),
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t0", "name": "Bash", "input": {"command": "cat a.py"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0", "content": FILE}]}, tt(8)]
        view = self.run_req(anth_request(copy.deepcopy(msgs)), ledger=True)["messages"]
        self.assertEqual(view[:4], msgs[:4])
        self.assertEqual(texts(view[4])[0], texts(msgs[4])[0])
        self.assertTrue(texts(view[4])[1].startswith(LEDGER_HEAD))
        self.assertFalse(any(r["text"].startswith((LEDGER_HEAD, OPEN)) for r in self.db_rows()))

    def test_chat_trim_note_is_a_system_message_after_the_cut_turn(self):
        msgs = chat_history()
        msgs.insert(4, {"role": "system", "content": "mid"})
        out = self.run_req({"messages": copy.deepcopy(msgs), **headers()}, "acompletion", trim_at_tokens=1)["messages"]
        notes = [m for m in out if m["role"] == "system" and m["content"].startswith("[smarter-compact: continuing")]
        self.assertEqual(len(notes), 1)
        self.assertFalse(any("[smarter-compact" in t for t in user_texts(out)))
        at = out.index(notes[0])
        self.assertEqual(out[at - 1]["content"], "second")

    def test_responses_note_is_a_developer_message(self):
        items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "the task"}]},
                 {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "ctx"}]},
                 {"type": "function_call", "call_id": "r0", "name": "shell",
                  "arguments": json.dumps({"command": "cat a.py"})},
                 {"type": "function_call_output", "call_id": "r0", "output": FILE}]
        out = self.run_req({"input": copy.deepcopy(items), **headers()}, "aresponses", ledger=True)["input"]
        self.assertEqual(out[:4], items)
        self.assertEqual(out[4]["role"], "developer")
        self.assertTrue(out[4]["content"][0]["text"].startswith(LEDGER_HEAD))


class InlineTests(Base):
    """No mid-conversation system message: each note kind is inline, wrapped; tool-output placeholders are not."""

    def test_clearing_only_note_is_wrapped_and_the_placeholder_is_not(self):
        msgs = anth_history(6, filler=2000)
        full = request_chars(msgs)
        # Clearing five of six results frees about 40%: enough for a target at 85%, so no turn is dropped.
        out = self.run_req(anth_request(copy.deepcopy(msgs)), chars_per_token=1.0, trim_at_tokens=int(0.9 * full),
                           trim_to_tokens=int(0.85 * full), clear_keep_results=1)["messages"]
        self.assertEqual(len(out), len(msgs))
        note = next(t for t in user_texts(out) if CLEARED_LEAD in t)
        self.assertTrue(note.startswith(OPEN + f"[smarter-compact: session {SID}] {CLEARED_LEAD}"))
        self.assertTrue(note.endswith("\n</system-reminder>"))
        cleared = [b["content"] for m in out for b in m["content"] if b.get("type") == "tool_result"
                   and str(b["content"]).startswith(MASK_PREFIX)]
        self.assertTrue(cleared)
        self.assertFalse(any("system-reminder" in str(c) for c in cleared))

    def test_scratchpad_block_is_inside_the_one_wrapped_trim_note(self):
        (self.ctx / "scratchpad").mkdir(parents=True, exist_ok=True)
        (self.ctx / "scratchpad" / f"{SID}.md").write_text("goal: keep me")
        data = {**anth_request(copy.deepcopy(anth_history(5))),
                "tools": [{"name": "Bash"}, {"name": "smart_compact_scratchpad"}]}
        out = self.run_req(data, trim_at_tokens=1, scratchpad=True)["messages"]
        note = next(t for t in user_texts(out) if SCRATCHPAD_LABEL in t)
        self.assertTrue(note.startswith(OPEN + SCRATCHPAD_LABEL + "\n<compact-scratchpad>\ngoal: keep me\n"))
        self.assertIn("\n[smarter-compact: continuing session", note)
        self.assertEqual(note.count("<system-reminder>"), 1)


class ParityTests(unittest.TestCase):
    """The proxy hook and the plugin's trimContext wrap the same note into the same bytes."""

    def setUp(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def node(self, script):
        out = subprocess.run(["node", "--input-type=module", "-e", script], cwd=REPO, capture_output=True,
                             text=True, check=True, timeout=60)
        return json.loads(out.stdout)

    def test_wrap_matches(self):
        cases = ["", "a", "[smarter-compact: x]\nline 2", "ünïcode — ✓", "ends\n"]
        js = self.node("import { wrapNote } from './plugin/lib/context-trim.mjs';"
                       f"console.log(JSON.stringify({json.dumps(cases)}.map(wrapNote)));")
        self.assertEqual(js, [wrap_note(c) for c in cases])

    def test_ledger_block_through_both_live_paths(self):
        state = Path(self.tmp.name) / "py"
        hook = _hook_module(str(state))
        msgs = [{"role": "user", "content": [{"type": "text", "text": "the task"}]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "t0", "name": "Bash", "input": {"command": "cat a.py"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0", "content": FILE}]}]
        data = {"model": "claude-haiku-5-5", "system": [{"type": "text", "text": "S"}],
                "tools": [{"name": "Bash", "input_schema": {"type": "object"}}], "messages": msgs,
                "proxy_server_request": {"headers": {"X-Claude-Code-Session-Id": SID}}}
        env = {"SMARTER_COMPACT_STATE_DIR": str(state), "CONTEXT_LEDGER": "1", "CONTEXT_TRIM_AT_TOKENS": "10000000",
               "CONTEXT_STANDING_INSTRUCTION": "0", "CONTEXT_COMPRESS_OVER_TOKENS": "0"}
        with mock.patch.dict(os.environ, env):
            out = asyncio.run(hook.handler.async_pre_call_hook(
                types.SimpleNamespace(request_route="/v1/messages"), None, data, "anthropic_messages"))
        py = out["messages"][2]["content"][-1]["text"]
        js_home = Path(self.tmp.name) / "js"
        js = self.node(
            "import { trimContext, trimSettings } from './plugin/lib/context-trim.mjs';"
            f"const FILE = {json.dumps(FILE)};"
            "const s = trimSettings({ CONTEXT_COMPRESS_OVER_TOKENS: '0', CONTEXT_STANDING_INSTRUCTION: '0',"
            " CONTEXT_TRIM_AT_TOKENS: '10000000', CONTEXT_LEDGER: '1',"
            f" SMARTER_COMPACT_STATE_DIR: {json.dumps(str(js_home))} }}, {json.dumps(str(js_home))});"
            "const messages = [{ role: 'user', content: [{ type: 'text', text: 'the task' }] },"
            " { role: 'assistant', content: [{ type: 'tool-call', id: 'p0', name: 'bash',"
            " input: { command: 'cat a.py' } }] },"
            " { role: 'tool', content: [{ type: 'tool-result', id: 'p0', name: 'bash',"
            " result: { type: 'text', value: FILE } }] }];"
            f"const view = await trimContext({{ sessionID: {json.dumps(SID)}, messages }}, s);"
            "console.log(JSON.stringify(view[2].content[0].result.value.slice(FILE.length + 2)));")
        self.assertTrue(py.startswith(OPEN + LEDGER_HEAD))
        self.assertEqual(js, py)


if __name__ == "__main__":
    unittest.main()
