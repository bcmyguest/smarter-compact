"""CONTEXT_EVICTION: each rule, the newest batch, stability between batch boundaries, blobs, events, flag off."""

import copy
import json
import re
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import SID, Base, anth_request  # noqa: E402

from smarter_compact import Settings  # noqa: E402
from smarter_compact.compress import MASK_PREFIX, mask_placeholder  # noqa: E402
from smarter_compact.evict import cat_paths  # noqa: E402

# No tokens in it, long enough that a result outgrows its placeholder.
FILLER = "." * 300
_ID_RE = re.compile(r'smart_compact_get\(id="([0-9a-f]{16})"\)')


def bash(command, out):
    return ("Bash", {"command": command}, out)


def conv(steps):
    """Anthropic history: the task, then per step one assistant tool call and its result (message 2i+2)."""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "the task"}]}]
    for i, (name, args, out) in enumerate(steps):
        msgs.append({"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": args}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                                  "content": f"{out}\n{FILLER}"}]})
    return msgs


def distinct(n, start=0):
    """n steps whose results share no token with anything else."""
    return [bash(f"ls d{i}", f"result marker{i:04d}x") for i in range(start, start + n)]


class EvictionTests(Base):
    def send(self, steps, sid=SID, **kw):
        kw.setdefault("eviction", True)
        return self.run_req(anth_request(copy.deepcopy(conv(steps)), sid), **kw)

    @staticmethod
    def evicted(out):
        """Step indices whose result went out as its placeholder."""
        return [(j - 2) // 2 for j, m in enumerate(out["messages"])
                if m["role"] == "user" and j > 0 and str(m["content"][0].get("content", "")).startswith(MASK_PREFIX)]

    def events(self, kind):
        conn = sqlite3.connect(self.ctx / "sessions" / f"{SID}.db")
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,))]
        finally:
            conn.close()

    def test_superseded_file_state_goes_at_the_boundary(self):
        steps = [("Read", {"file_path": "/app/a.py"}, "def alpha(): pass"), *distinct(2),
                 ("Edit", {"file_path": "/app/a.py", "old_string": "x", "new_string": "y"}, "edited"), *distinct(2, 2)]
        self.assertEqual(self.evicted(self.send(steps)), [0])

    def test_a_cat_reads_the_file_and_a_partial_read_does_not_supersede(self):
        steps = [("Read", {"file_path": "/app/b.py"}, "whole b"), *distinct(2),
                 ("Read", {"file_path": "/app/b.py", "offset": 10, "limit": 5}, "part of b"),
                 bash("cat /app/c.py", "c body"), bash("cat /app/c.py", "c body again")]
        # Step 4's cat is in the newest batch (3-5), step 0's whole read has no full successor yet.
        self.assertEqual(self.evicted(self.send(steps, eviction_keep=100)), [])
        steps += [*distinct(2, 10), ("Read", {"file_path": "/app/c.py"}, "c via read")]
        self.assertEqual(self.evicted(self.send(steps, eviction_keep=100)), [4, 5])
        self.assertEqual(cat_paths("cat /app/c.py"), ["/app/c.py"])
        self.assertEqual(cat_paths("cat /app/c.py | head"), [])

    def test_superseded_command_goes(self):
        steps = [bash("make test", "FAILED 3"), *distinct(3), bash("make  test", "passed"), *distinct(1, 3)]
        self.assertEqual(self.evicted(self.send(steps, eviction_keep=100)), [0])

    def test_unreferenced_old_result_goes_and_a_referenced_one_stays(self):
        steps = [bash("ls a", "alpha_token_one"), bash("ls b", "bravo_token_two"), *distinct(3),
                 bash("grep bravo_token_two src", "found")]
        # keep 2: steps 0 and 1 have 4+ later results; step 1's token is in step 5's input.
        self.assertEqual(self.evicted(self.send(steps, eviction_keep=2)), [0, 2])
        # Fewer than keep later results: nothing goes by this rule.
        self.assertEqual(self.evicted(self.send(steps, "other", eviction_keep=20)), [])

    def test_nothing_in_the_newest_batch_goes(self):
        steps = [*distinct(3), bash("make", "one"), bash("make", "two"), bash("make", "three")]
        self.assertEqual(self.evicted(self.send(steps, eviction_keep=1)), [0, 1, 2])
        self.assertEqual(self.evicted(self.send(steps, "other", eviction_batch=4, eviction_keep=1)), [0, 1])

    def test_the_set_is_fixed_between_boundaries_and_only_grows(self):
        steps = [bash("make", "first"), *distinct(3), bash("make", "second")]
        # Five steps: batch 1 was computed at three, when step 0 was not yet superseded.
        self.assertEqual(self.evicted(self.send(steps[:3], eviction_keep=100)), [])
        mid = self.send(steps, eviction_keep=100)
        self.assertEqual(self.evicted(mid), [])
        at = self.send([*steps, *distinct(1, 9)], eviction_keep=100)
        self.assertEqual(self.evicted(at), [0])
        for n in (1, 2):
            later = self.send([*steps, *distinct(1 + n, 9)], eviction_keep=100)
            self.assertEqual(json.dumps(later["messages"][:len(at["messages"])]), json.dumps(at["messages"]))

    def test_an_evicted_result_stays_evicted_once_referenced(self):
        steps = [bash("ls a", "alpha_token_one"), *distinct(5)]
        self.assertIn(0, self.evicted(self.send(steps, eviction_keep=2)))
        steps += [bash("grep alpha_token_one src", "found"), *distinct(2, 10)]
        self.assertIn(0, self.evicted(self.send(steps, eviction_keep=2)))
        # A session that never evicted it keeps it: the rule alone no longer picks it.
        self.assertNotIn(0, self.evicted(self.send(steps, "other", eviction_keep=2)))

    def test_evicted_ids_round_trip_to_blobs_and_rows_leave_context(self):
        steps = [bash("make", "the full make output 0123"), *distinct(3), bash("make", "again"), *distinct(1, 3)]
        out = self.send(steps, eviction_keep=100)
        placeholder = out["messages"][2]["content"][0]["content"]
        original = f"the full make output 0123\n{FILLER}"
        self.assertEqual(placeholder, mask_placeholder(original))
        bid = _ID_RE.search(placeholder).group(1)
        self.assertEqual((self.ctx / "blobs" / f"{bid}.txt").read_text(), original)
        row = self.db_rows(where="kind = 'tool_result' AND text LIKE 'the full make output%'")
        self.assertEqual([r["in_context"] for r in row], [0])

    def test_one_event_per_batch_that_evicts(self):
        steps = [bash("make", "first"), *distinct(3), bash("make", "second"), *distinct(1, 3)]
        self.send(steps, eviction_keep=100)
        self.send([*steps, *distinct(1, 9)], eviction_keep=100)
        events = self.events("evict")
        self.assertEqual([e["dropped"] for e in events], [1])
        self.assertLess(events[0]["tokens_after"], events[0]["tokens_before"])

    def test_the_threshold_trim_does_not_run_while_evicting(self):
        steps = distinct(6)
        out = self.send(steps, trim_at_tokens=10, eviction_keep=100)
        self.assertEqual(json.dumps(out["messages"]), json.dumps(conv(steps)))
        self.assertFalse((self.ctx / "sessions" / f"{SID}.db").exists() and self.events("trim"))
        trimmed = self.send(steps, trim_at_tokens=10, eviction=False)
        self.assertNotEqual(json.dumps(trimmed["messages"]), json.dumps(conv(steps)))

    def test_flag_off_is_byte_identical(self):
        steps = [bash("make", "first"), *distinct(3), bash("make", "second"), *distinct(4, 3)]
        out = self.send(steps, eviction=False)
        self.assertEqual(json.dumps(out["messages"]), json.dumps(conv(steps)))
        state = json.loads((self.ctx / "sessions" / f"{SID}.trim.json").read_text()) \
            if (self.ctx / "sessions" / f"{SID}.trim.json").exists() else {}
        self.assertNotIn("evict", json.dumps(state))

    def test_settings_from_env(self):
        off = Settings.from_env({})
        self.assertEqual((off.eviction, off.eviction_batch, off.eviction_keep), (False, 3, 12))
        on = Settings.from_env({"CONTEXT_EVICTION": "1", "CONTEXT_EVICTION_BATCH": "5", "CONTEXT_EVICTION_KEEP": "0"})
        self.assertEqual((on.eviction, on.eviction_batch, on.eviction_keep), (True, 5, 12))


if __name__ == "__main__":
    unittest.main()
