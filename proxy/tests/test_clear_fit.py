"""CONTEXT_CLEAR_FIT: clearing alone settles a trim under clear_fit x trim_at. Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import SID, Base, anth_request  # noqa: E402
from test_trim_experiments import HUGE, full_tokens, in_flight_turn, results_in  # noqa: E402

from smarter_compact import Settings  # noqa: E402
from smarter_compact.core import MASK_PREFIX  # noqa: E402
from smarter_compact.settings import DEFAULT_CLEAR_FIT  # noqa: E402

# Far below what clearing alone leaves, so only clear_fit can accept it.
LOW_TARGET = 1000


def later_step(msgs, k, text):
    return msgs + [{"role": "assistant", "content": [{"type": "tool_use", "id": f"n{k}", "name": "Bash",
                                                      "input": {"command": f"echo {k}"}}]},
                   {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"n{k}", "content": text}]}]


class ClearFitTests(Base):
    def send(self, msgs, sid=SID, **kw):
        kw.setdefault("chars_per_token", 1.0)
        kw.setdefault("clear_keep_results", 2)
        kw.setdefault("trim_to_tokens", LOW_TARGET)
        return self.run_req(anth_request(copy.deepcopy(msgs), sid=sid), **kw)["messages"]

    def trims(self):
        conn = sqlite3.connect(self.ctx / "sessions" / f"{SID}.db")
        try:
            return conn.execute("SELECT dropped FROM events WHERE kind = 'trim' ORDER BY id").fetchall()
        finally:
            conn.close()

    def test_setting_from_env(self):
        self.assertEqual(Settings().clear_fit, DEFAULT_CLEAR_FIT)
        self.assertEqual(Settings.from_env({}).clear_fit, DEFAULT_CLEAR_FIT)
        self.assertEqual(Settings.from_env({"CONTEXT_CLEAR_FIT": "0.5"}).clear_fit, 0.5)
        for bad in ("0", "1", "1.5", "-0.2", "x", "nan"):
            self.assertEqual(Settings.from_env({"CONTEXT_CLEAR_FIT": bad}).clear_fit, DEFAULT_CLEAR_FIT, bad)
        # The default leaves at least about 35k of headroom at a 95k threshold.
        self.assertLessEqual(DEFAULT_CLEAR_FIT * 95000, 60000)

    def test_clearing_alone_accepted_under_clear_fit(self):
        msgs = in_flight_turn(HUGE)
        full = full_tokens(msgs)
        view = self.send(msgs, trim_at_tokens=full, clear_fit=0.5)
        # Above the target, below 0.5 x trim_at: every message stays, old results are placeholders.
        self.assertGreater(full_tokens(view), LOW_TARGET)
        self.assertLess(full_tokens(view), 0.5 * full)
        self.assertEqual([m["role"] for m in view], [m["role"] for m in msgs])
        self.assertTrue(results_in(view)["s0"].startswith(MASK_PREFIX))
        self.assertNotIn("earlier messages are NOT", json.dumps(view))
        self.assertEqual(self.trims(), [(0,)])

    def test_drops_turns_when_clearing_lands_above_clear_fit(self):
        msgs = in_flight_turn(HUGE)
        full = full_tokens(msgs)
        accepted = self.send(msgs, trim_at_tokens=full, clear_fit=0.5)
        self.assertGreater(full_tokens(accepted), 0.1 * full + 2000)  # clearing alone lands above the 0.1 bar
        view = self.send(msgs, "sid-low", trim_at_tokens=full, clear_fit=0.1)
        self.assertLess(len(view), len(msgs))
        self.assertIn("earlier messages are NOT", json.dumps(view))

    def test_clear_fit_at_or_below_target_is_the_old_rule(self):
        msgs = in_flight_turn(HUGE)
        full = full_tokens(msgs)
        views = []
        for k, fit in enumerate((1e-6, LOW_TARGET / full)):
            view = self.send(msgs, f"sid-{k}", trim_at_tokens=full, clear_fit=fit)
            views.append(json.dumps(view).replace(f"sid-{k}", "S"))
            # Clearing alone does not get under the target, so turns drop.
            self.assertIn("earlier messages are NOT", views[-1])
        self.assertEqual(views[0], views[1])
        # A target above what clearing alone leaves still accepts it, whatever clear_fit.
        view = self.send(msgs, "sid-high", trim_at_tokens=full, clear_fit=1e-6, trim_to_tokens=int(0.5 * full))
        self.assertEqual(len(view), len(msgs))
        self.assertTrue(results_in(view)["s0"].startswith(MASK_PREFIX))

    def test_headroom_rerenders_byte_identical_until_the_next_trim(self):
        msgs = in_flight_turn(HUGE)
        full = full_tokens(msgs)
        first = self.send(msgs, trim_at_tokens=full, clear_fit=0.5)
        # The full history now exceeds trim_at, but the cleared view sits in the headroom: no new trim.
        more, prev = msgs, first
        for k in range(3):
            more = later_step(more, k, f"step {k} " + "y" * 2000)
            self.assertGreater(full_tokens(more), full)
            view = self.send(more, trim_at_tokens=full, clear_fit=0.5)
            self.assertEqual(json.dumps(view[:len(prev)]), json.dumps(prev))
            self.assertEqual(results_in(view)[f"n{k}"], f"step {k} " + "y" * 2000)
            prev = view
        self.assertEqual(self.trims(), [(0,)])
        # Past trim_at again: the next trim moves the bound forward.
        more = later_step(more, 9, "big" + HUGE * 5)
        view = self.send(more, trim_at_tokens=full, clear_fit=0.5)
        self.assertNotEqual(json.dumps(view[:len(prev)]), json.dumps(prev))
        self.assertEqual(len(self.trims()), 2)


if __name__ == "__main__":
    unittest.main()
