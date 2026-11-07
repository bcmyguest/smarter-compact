"""CONTEXT_SCRATCHPAD: the nudge near the threshold, the pad injected at trim, and the flag-off identity."""

import copy
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import SID, Base, anth_history, anth_request  # noqa: E402

from smarter_compact.core import (  # noqa: E402
    SCRATCHPAD_LABEL, SCRATCHPAD_MAX_CHARS, recall_tool_names, scratchpad_nudge,
)

NUDGE_TAG = "<compact-scratchpad-nudge>"
# Any prompt passes this fraction of BIG, so the nudge fires without a trim.
LOW = 1e-6


def ask(msgs, t):
    """msgs plus a new user question, so the request ends on a user item the nudge can go on."""
    return [*msgs, {"role": "user", "content": [{"type": "text", "text": f"next {t}"}]}]


def reply(msgs, t):
    """msgs plus an assistant tool call and its result: the agent's next step."""
    return [*msgs, {"role": "assistant", "content": [{"type": "tool_use", "id": f"x{t}", "name": "Bash",
                                                      "input": {"command": f"ls {t}"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"x{t}", "content": f"res {t}"}]}]


class ScratchpadTests(Base):
    def pad(self, text, sid=SID):
        (self.ctx / "scratchpad").mkdir(parents=True, exist_ok=True)
        (self.ctx / "scratchpad" / f"{sid}.md").write_text(text)

    def events(self, kind, sid=SID):
        conn = sqlite3.connect(self.ctx / "sessions" / f"{sid}.db")
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,))]
        finally:
            conn.close()

    def on(self, msgs, **kw):
        return self.run_req(anth_request(copy.deepcopy(msgs)), scratchpad=True, scratchpad_nudge_at=LOW, **kw)

    def nudged(self, out):
        return [j for j, m in enumerate(out["messages"]) if NUDGE_TAG in json.dumps(m)]

    def test_nudge_goes_on_the_newest_item_once_and_rerenders_identically(self):
        msgs = ask(anth_history(3), 0)
        out = self.on(msgs)
        self.assertEqual(self.nudged(out), [len(msgs) - 1])
        last = out["messages"][-1]
        self.assertEqual(last["content"][:-1], msgs[-1]["content"])
        self.assertEqual(last["content"][-1]["text"], scratchpad_nudge(SID, recall_tool_names(["Bash"])))
        later = reply(msgs, 1)
        again = self.on(later)
        self.assertEqual(self.nudged(again), [len(msgs) - 1])
        self.assertEqual(json.dumps(again["messages"][len(msgs) - 1]), json.dumps(last))
        self.assertEqual(again["messages"][len(msgs):], later[len(msgs):])
        self.assertEqual(len(self.events("nudge")), 1)

    def test_no_nudge_below_the_threshold_or_on_an_assistant_item(self):
        out = self.run_req(anth_request(ask(anth_history(3), 0)), scratchpad=True, scratchpad_nudge_at=0.75)
        self.assertEqual(self.nudged(out), [])
        out = self.on(anth_history(3))
        self.assertEqual(self.nudged(out), [])

    def test_pad_is_injected_capped_archived_and_the_nudge_rearms(self):
        msgs = ask(anth_history(4), 0)
        self.on(msgs)
        self.pad("goal: X\n" + "p" * (SCRATCHPAD_MAX_CHARS + 50))
        self.force()
        trimmed = reply(msgs, 1)
        out = self.on(trimmed)
        dumped = json.dumps(out["messages"])
        self.assertIn("<compact-scratchpad>\\ngoal: X\\n", dumped)
        self.assertIn("p" * (SCRATCHPAD_MAX_CHARS - len("goal: X\n")) + "\\n</compact-scratchpad>", dumped)
        self.assertNotIn("p" * (SCRATCHPAD_MAX_CHARS - len("goal: X\n") + 1), dumped)
        self.assertNotIn(NUDGE_TAG, dumped)
        self.assertTrue((self.ctx / "scratchpad" / f"{SID}.md").exists())
        self.assertTrue((self.ctx / "scratchpad" / f"{SID}.1.md").exists())
        self.assertEqual([e["scratchpad_chars"] for e in self.events("trim")], [SCRATCHPAD_MAX_CHARS])
        # The note persists with the pad; the nudge re-arms on the next request's newest item.
        nxt = self.on(ask(trimmed, 2))
        self.assertIn("<compact-scratchpad>", json.dumps(nxt["messages"]))
        self.assertEqual(self.nudged(nxt), [len(nxt["messages"]) - 1])
        self.assertEqual(len(self.events("nudge")), 2)
        # A second archived pad takes the next free number.
        self.pad("second")
        self.force()
        self.on(reply(ask(trimmed, 2), 3))
        self.assertTrue((self.ctx / "scratchpad" / f"{SID}.2.md").exists())

    def test_pad_persists_into_the_next_trim_without_a_rewrite(self):
        msgs = ask(anth_history(4), 0)
        self.on(msgs)
        self.pad("goal: keep me")
        self.force()
        trimmed = reply(msgs, 1)
        self.on(trimmed)
        self.force()
        out = self.on(reply(ask(trimmed, 2), 3))
        dumped = json.dumps(out["messages"])
        self.assertIn(json.dumps(SCRATCHPAD_LABEL)[1:-1] + "\\n<compact-scratchpad>\\ngoal: keep me\\n", dumped)
        self.assertEqual([e["scratchpad_chars"] for e in self.events("trim")], [13, 13])
        self.assertTrue((self.ctx / "scratchpad" / f"{SID}.2.md").exists())

    def test_empty_or_missing_pad_injects_nothing(self):
        msgs = ask(anth_history(4), 0)
        self.force()
        out = self.on(msgs)
        self.assertNotIn("<compact-scratchpad>", json.dumps(out["messages"]))
        self.pad("  \n")
        self.force()
        out = self.on(reply(ask(reply(ask(reply(msgs, 1), 2), 3), 4), 5))
        self.assertNotIn("<compact-scratchpad>", json.dumps(out["messages"]))
        self.assertTrue((self.ctx / "scratchpad" / f"{SID}.md").exists())
        self.assertEqual([e["scratchpad_chars"] for e in self.events("trim")], [0, 0])

    def test_flag_off_is_byte_identical_to_the_default(self):
        steps = [ask(anth_history(4), 0)]
        steps.append(reply(steps[0], 1))
        steps.append(ask(steps[1], 2))
        outs = {}
        for name, kw in (("default", {}), ("off", {"scratchpad": False, "scratchpad_nudge_at": LOW})):
            self.ctx = Path(self.tmp.name) / name
            if name == "off":
                self.pad("state")
            got = []
            for j, msgs in enumerate(steps):
                if j == 1:
                    self.force()
                got.append(json.dumps(self.run_req(anth_request(copy.deepcopy(msgs)), **kw), sort_keys=True))
            outs[name] = got
        self.assertEqual(outs["off"], outs["default"])
        self.assertNotIn("scratchpad", "".join(outs["off"]))
        self.assertTrue((self.ctx / "scratchpad" / f"{SID}.md").exists())
        self.assertIsNone(self.events("trim")[0]["scratchpad_chars"])
        self.assertEqual(self.events("nudge"), [])

    def test_tool_names_resolve_like_the_recall_tools(self):
        self.assertEqual(recall_tool_names(["Bash"])["scratchpad"], "smart_compact_scratchpad")
        offered = recall_tool_names([{"name": "mcp__smarter-compact__smart_compact_scratchpad"}])
        self.assertEqual((offered["scratchpad"], offered["scratchpad_load"]),
                         ("mcp__smarter-compact__smart_compact_scratchpad", ""))
        deferred = recall_tool_names([{"name": "ToolSearch"}])
        self.assertEqual(deferred["scratchpad_load"],
                         'ToolSearch with query "select:mcp__smarter-compact__smart_compact_scratchpad"')
        self.assertIn("load it first by calling ToolSearch", scratchpad_nudge(SID, deferred))
        dotted = recall_tool_names(["smarter-compact.smart_compact_scratchpad", "xsmart_compact_scratchpad"])
        self.assertEqual(dotted["scratchpad"], "smarter-compact.smart_compact_scratchpad")
        self.assertEqual(recall_tool_names(["xsmart_compact_scratchpad"])["scratchpad"], "smart_compact_scratchpad")


if __name__ == "__main__":
    unittest.main()
