"""CONTEXT_REUSE_RULES: static don't-repeat-work rules end the standing instruction (Hu et al., arXiv 2609.30725).
Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import Base, anth_history, anth_request, chat_history, headers, responses_history  # noqa: E402

from smarter_compact import Settings  # noqa: E402
from smarter_compact.core import REUSE_RULES, STANDING_INSTRUCTION, standing_instruction  # noqa: E402

# The standing instruction before CONTEXT_REUSE_RULES, byte for byte (docs/context-store.md).
OLD = ("Context management: to fit your context window, older turns may be removed and replaced by an "
       "[smarter-compact: …] pointer. Removed content is saved, not forgotten. When you need earlier content you cannot see in your context — values, ids, keys, counts, decisions — call "
       "smart_compact_search (use `pattern` for exact ids/keys, `context` for neighbouring rows; `line_matches` gives "
       "exact counts) or smart_compact_sql for counts and aggregates over the session store; never guess "
       "from memory you cannot see. This trim never summarizes removed content — a remembered \"summary of earlier "
       "context\" is not its record; cite a search result instead.")


class ReuseRulesTests(Base):
    def on(self, data, call_type="anthropic_messages", **kw):
        return self.run_req(data, call_type, standing_instruction=True, **kw)

    def test_default_on_and_zero_off(self):
        self.assertTrue(Settings.from_env({}).reuse_rules)
        self.assertFalse(Settings.from_env({"CONTEXT_REUSE_RULES": "0"}).reuse_rules)

    def test_rules_are_short_static_and_harness_neutral(self):
        lines = REUSE_RULES.split("\n")
        self.assertLessEqual(len(lines), 3)
        self.assertTrue(all(len(line) <= 200 for line in lines))
        for name in ("Bash", "Read", "Grep", "Glob", "Edit", "Write", "Task", "mcp__", "Claude"):
            self.assertNotIn(name, REUSE_RULES, name)
        self.assertEqual(STANDING_INSTRUCTION, f"{OLD}\n{REUSE_RULES}")

    def test_zero_gives_the_old_bytes(self):
        self.assertEqual(standing_instruction(reuse=False), OLD)
        out = self.on(anth_request(anth_history(2)), reuse_rules=False)
        self.assertEqual(out["system"], [{"type": "text", "text": "SYSTEM"}, {"type": "text", "text": OLD}])
        out = self.on({"messages": chat_history(), **headers()}, "acompletion", reuse_rules=False)
        self.assertEqual(out["messages"][1], {"role": "system", "content": OLD})
        out = self.on({"input": responses_history(), "instructions": "I", **headers()}, "aresponses", reuse_rules=False)
        self.assertEqual(out["instructions"], f"I\n\n{OLD}")

    def test_on_ends_the_instruction_in_every_format(self):
        out = self.on(anth_request(anth_history(2)))
        self.assertEqual(out["system"][-1]["text"], f"{OLD}\n{REUSE_RULES}")
        out = self.on({"messages": chat_history(), **headers()}, "acompletion")
        self.assertEqual(out["messages"][1]["content"], f"{OLD}\n{REUSE_RULES}")
        out = self.on({"input": responses_history(), "instructions": "I", **headers()}, "aresponses")
        self.assertEqual(out["instructions"], f"I\n\n{OLD}\n{REUSE_RULES}")

    def test_byte_identical_across_requests_and_trims(self):
        msgs = anth_history(3)
        before = self.on(anth_request(copy.deepcopy(msgs)))["system"]
        trimmed = self.on(anth_request(copy.deepcopy(msgs)), trim_at_tokens=1)["system"]
        msgs += [{"role": "assistant", "content": [{"type": "text", "text": "more"}]}]
        after = self.on(anth_request(copy.deepcopy(msgs)))["system"]
        self.assertEqual(json.dumps(before), json.dumps(trimmed))
        self.assertEqual(json.dumps(before), json.dumps(after))

    def test_off_with_the_standing_instruction_off(self):
        out = self.run_req(anth_request(anth_history(2)))
        self.assertNotIn(REUSE_RULES, json.dumps(out["system"]))


if __name__ == "__main__":
    unittest.main()
