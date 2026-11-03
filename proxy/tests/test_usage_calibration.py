"""Trim estimate calibrated from reported usage. Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openmem_context import Settings, core, process_request, process_usage  # noqa: E402
from openmem_context.core import prompt_tokens_from_usage, response_prompt_tokens  # noqa: E402
from openmem_context.tokens import (  # noqa: E402
    DEFAULT_CHARS_PER_TOKEN, MAX_CHARS_PER_TOKEN, estimate_prompt_tokens, fingerprint, record_usage, request_chars,
)

SID = "sess-cal"


def history(turns, filler=400):
    msgs = []
    for t in range(turns):
        msgs.append({"role": "user", "content": f"question {t} " + "q" * filler})
        msgs.append({"role": "assistant", "content": f"answer {t} " + "a" * filler})
    return msgs


def request(msgs, call_id=None):
    data = {"model": "claude", "system": "SYSTEM", "messages": msgs,
            "proxy_server_request": {"headers": {"x-claude-code-session-id": SID}}}
    if call_id:
        data["litellm_call_id"] = call_id
    return data


def no_http(*args):
    raise AssertionError("no registration, so no recall")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Path(self.tmp.name) / "context"
        core._pending.clear()

    def settings(self, **kw):
        base = dict(trim_at_tokens=10 ** 9, compress_over_tokens=0, context_dir=self.ctx, standing_instruction=False)
        base.update(kw)
        return Settings(**base)

    def run_req(self, msgs, call_id=None, **kw):
        return process_request(request(copy.deepcopy(msgs), call_id), "anthropic_messages", self.settings(**kw),
                               http=no_http)

    def chars_of(self, msgs):
        return request_chars("SYSTEM", None, msgs)

    def trim_state(self):
        path = self.ctx / "sessions" / f"{SID}.trim.json"
        return json.loads(path.read_text()) if path.exists() else {}


class SettingsTests(unittest.TestCase):
    def test_default_and_env(self):
        self.assertEqual(Settings().chars_per_token, DEFAULT_CHARS_PER_TOKEN)
        self.assertEqual(DEFAULT_CHARS_PER_TOKEN, 2.5)
        self.assertEqual(Settings.from_env({"CONTEXT_CHARS_PER_TOKEN": "3.2"}).chars_per_token, 3.2)
        for bad in ("0", "-1", "x", "nan", "inf", ""):
            self.assertEqual(Settings.from_env({"CONTEXT_CHARS_PER_TOKEN": bad}).chars_per_token, 2.5, bad)


class EstimateTests(unittest.TestCase):
    def test_fallback_ratio_without_usage(self):
        self.assertEqual(estimate_prompt_tokens(1000, None, "r", 2.5), 400)
        self.assertEqual(estimate_prompt_tokens(1001, {}, "r", 2.5), 401)
        self.assertEqual(estimate_prompt_tokens(1000, None, "r", 4.0), 250)

    def test_thread_sample_plus_delta(self):
        usage = {"threads": {"r": {"chars": 2000, "tokens": 1000}}, "last": {"chars": 2000, "tokens": 1000}}
        # 2 chars per token observed: 1000 real + 600 new chars / 2.
        self.assertEqual(estimate_prompt_tokens(2600, usage, "r", 2.5), 1300)
        # A trim shrinks the request: the delta is negative, never below zero.
        self.assertEqual(estimate_prompt_tokens(1000, usage, "r", 2.5), 500)
        self.assertEqual(estimate_prompt_tokens(0, {"threads": {"r": {"chars": 9000, "tokens": 10}}}, "r", 2.5), 0)

    def test_other_thread_uses_session_ratio(self):
        usage = {"threads": {"r": {"chars": 3000, "tokens": 1000}}, "last": {"chars": 3000, "tokens": 1000}}
        self.assertEqual(estimate_prompt_tokens(900, usage, "other", 2.5), 300)

    def test_ratio_is_bounded_and_bad_samples_ignored(self):
        self.assertEqual(estimate_prompt_tokens(800, {"last": {"chars": 10 ** 6, "tokens": 1}}, "x", 2.5),
                         800 / MAX_CHARS_PER_TOKEN)
        for bad in ({"chars": 0, "tokens": 5}, {"chars": 5, "tokens": 0}, {"chars": "9", "tokens": 3},
                    {"chars": True, "tokens": 3}, [], None):
            self.assertEqual(estimate_prompt_tokens(1000, {"last": bad, "threads": {"x": bad}}, "x", 2.5), 400)

    def test_record_usage(self):
        state = {"version": 1, "threads": {}}
        self.assertFalse(record_usage(state, "r", 0, 10))
        self.assertNotIn("usage", state)
        self.assertTrue(record_usage(state, "r", 100, 40))
        self.assertTrue(record_usage(state, "s", 300, 100))
        self.assertEqual(state["usage"], {"threads": {"r": {"chars": 100, "tokens": 40}, "s": {"chars": 300, "tokens": 100}},
                                          "last": {"chars": 300, "tokens": 100}})
        for i in range(40):
            record_usage(state, f"t{i}", 10, 5)
        self.assertEqual(len(state["usage"]["threads"]), 32)
        self.assertNotIn("r", state["usage"]["threads"])

    def test_schema_property_named_type_is_counted(self):
        # Claude Code tool schemas have parameters named "type" whose value is a dict.
        tools = [{"input_schema": {"properties": {"type": {"type": "string"}}}}]
        self.assertEqual(request_chars(tools), len("input_schema" "properties" "type" "type" "string"))


class UsageShapeTests(unittest.TestCase):
    def test_litellm_usage_prompt_tokens(self):
        # LiteLLM's Usage for Anthropic: prompt_tokens already includes cache reads and writes.
        usage = SimpleNamespace(prompt_tokens=1500, cache_read_input_tokens=1000, cache_creation_input_tokens=200)
        self.assertEqual(prompt_tokens_from_usage(usage), 1500)

    def test_anthropic_native_usage_sums_cache(self):
        usage = {"input_tokens": 12, "cache_read_input_tokens": 90000, "cache_creation_input_tokens": 3000,
                 "output_tokens": 50}
        self.assertEqual(prompt_tokens_from_usage(usage), 93012)

    def test_responses_usage(self):
        usage = {"input_tokens": 4000, "input_tokens_details": {"cached_tokens": 3000}, "output_tokens": 9}
        self.assertEqual(prompt_tokens_from_usage(usage), 4000)
        completed = SimpleNamespace(type="response.completed", response=SimpleNamespace(usage=SimpleNamespace(
            input_tokens=700, output_tokens=3)))
        self.assertEqual(response_prompt_tokens(completed, {}), 700)

    def test_standard_logging_fallback_and_missing(self):
        self.assertEqual(response_prompt_tokens(SimpleNamespace(), {"standard_logging_object": {"prompt_tokens": 77}}), 77)
        self.assertIsNone(response_prompt_tokens(None, {}))
        self.assertIsNone(response_prompt_tokens({"usage": {"prompt_tokens": 0}}, {"standard_logging_object": {}}))
        self.assertIsNone(prompt_tokens_from_usage({"output_tokens": 5}))


class CalibrationFlowTests(Base):
    def test_usage_is_recorded_for_the_call_id(self):
        msgs = history(3)
        self.run_req(msgs, call_id="call-1")
        root = fingerprint(msgs[0])
        process_usage({"litellm_call_id": "call-1"}, {"usage": {"prompt_tokens": 1234}}, self.settings())
        sample = {"chars": self.chars_of(msgs), "tokens": 1234}
        self.assertEqual(self.trim_state()["usage"], {"threads": {root: sample}, "last": sample})
        self.assertEqual(core._pending, {})

    def test_unknown_or_unreported_calls_change_nothing(self):
        self.run_req(history(2), call_id="call-2")
        process_usage({"litellm_call_id": "other"}, {"usage": {"prompt_tokens": 9}}, self.settings())
        process_usage({"litellm_call_id": "call-2"}, {"usage": {}}, self.settings())
        process_usage({}, None, self.settings())
        process_usage(None, None, self.settings())
        self.assertEqual(self.trim_state(), {})
        self.run_req(history(2))  # no litellm_call_id: nothing pending
        self.assertEqual(core._pending, {})

    def test_fallback_ratio_trims_at_configured_tokens(self):
        msgs = history(4)
        chars = self.chars_of(msgs)
        at = -(-chars * 2 // 5)  # ceil(chars / 2.5)
        kept = self.run_req(msgs, trim_at_tokens=at + 1)["messages"]
        self.assertEqual(len(kept), len(msgs))
        trimmed = self.run_req(msgs, trim_at_tokens=at)["messages"]
        self.assertLess(len(trimmed), len(msgs))

    def test_reported_usage_moves_the_threshold(self):
        msgs = history(4)
        self.run_req(msgs[:6], call_id="c1")
        chars6 = self.chars_of(msgs[:6])
        # The provider counts twice the fallback estimate: real tokens = chars / 1.25.
        real6 = chars6 * 4 // 5
        process_usage({"litellm_call_id": "c1"}, {"usage": {"prompt_tokens": real6}}, self.settings())
        chars8 = self.chars_of(msgs)
        ratio = chars6 / real6
        expected = real6 + math.ceil((chars8 - chars6) / ratio)
        fallback = -(-chars8 * 2 // 5)
        self.assertGreater(expected, fallback)
        # Between the fallback estimate and the calibrated one: only calibration trims.
        out = self.run_req(msgs, trim_at_tokens=fallback + 1)["messages"]
        self.assertLess(len(out), len(msgs))
        self.assertTrue(out[-2]["content"][0]["text"].startswith("[openmem: continuing session"))

    def test_calibration_never_moves_an_existing_cut(self):
        msgs = history(4)
        first = self.run_req(msgs, call_id="c1", trim_at_tokens=1)["messages"]
        cut_before = copy.deepcopy(self.trim_state()["threads"])
        process_usage({"litellm_call_id": "c1"}, {"usage": {"prompt_tokens": 10 ** 6}}, self.settings())
        self.assertEqual(self.trim_state()["threads"], cut_before)
        # Same history below the threshold: the same rewrite, byte for byte.
        again = self.run_req(msgs, trim_at_tokens=10 ** 9)["messages"]
        self.assertEqual(again, first)
        self.assertEqual(self.trim_state()["threads"], cut_before)

    def test_disabled_proxy_records_nothing(self):
        self.run_req(history(2), call_id="c3")
        process_usage({"litellm_call_id": "c3"}, {"usage": {"prompt_tokens": 50}}, self.settings(enabled=False))
        self.assertEqual(self.trim_state(), {})

    def test_pending_is_bounded(self):
        for i in range(core.MAX_PENDING + 10):
            core._await_usage(f"id{i}", SID, "r", 10)
        self.assertEqual(len(core._pending), core.MAX_PENDING)
        self.assertNotIn("id0", core._pending)


if __name__ == "__main__":
    unittest.main()
