"""Per-request usage events: cache reads and writes, trim state, cache_control breakpoints.
Run: python3 -m unittest discover -s proxy/tests"""

import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openmem_context import core, process_usage  # noqa: E402
from openmem_context.adapters import AnthropicAdapter  # noqa: E402
from openmem_context.core import response_usage_counts, usage_counts  # noqa: E402
from openmem_context.tokens import fingerprint  # noqa: E402
from test_usage_calibration import SID, Base, history  # noqa: E402

COLUMNS = ("kind", "model", "thread_root", "trimmed", "input_tokens", "cache_read_tokens", "cache_creation_tokens",
           "output_tokens", "breakpoints", "breakpoint_at")


class UsageCountsTests(unittest.TestCase):
    def test_litellm_anthropic_usage(self):
        # Shape of LiteLLM v1.109.0-dev.1 AnthropicConfig.calculate_usage: prompt_tokens includes cache.
        usage = SimpleNamespace(prompt_tokens=93012, completion_tokens=50, cache_read_input_tokens=90000,
                                cache_creation_input_tokens=3000,
                                prompt_tokens_details=SimpleNamespace(cached_tokens=90000, cache_creation_tokens=3000))
        self.assertEqual(usage_counts(usage), {"input_tokens": 12, "cache_read_tokens": 90000,
                                               "cache_creation_tokens": 3000, "output_tokens": 50})

    def test_litellm_openai_usage_has_no_creation_count(self):
        usage = {"prompt_tokens": 100, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 60}}
        self.assertEqual(usage_counts(usage), {"input_tokens": 40, "cache_read_tokens": 60,
                                               "cache_creation_tokens": None, "output_tokens": 5})

    def test_cache_write_tokens_alias(self):
        # LiteLLM's PromptTokensDetailsWrapper mirrors cache_write_tokens (OpenAI naming) and cache_creation_tokens.
        usage = {"prompt_tokens": 100, "completion_tokens": 2,
                 "prompt_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 30}}
        self.assertEqual(usage_counts(usage), {"input_tokens": 10, "cache_read_tokens": 60,
                                               "cache_creation_tokens": 30, "output_tokens": 2})

    def test_uncached_backend_reports_only_totals(self):
        self.assertEqual(usage_counts({"prompt_tokens": 70, "completion_tokens": 3}),
                         {"input_tokens": 70, "cache_read_tokens": None, "cache_creation_tokens": None,
                          "output_tokens": 3})

    def test_anthropic_native_and_responses_usage(self):
        native = {"input_tokens": 12, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0, "output_tokens": 4}
        self.assertEqual(usage_counts(native), {"input_tokens": 12, "cache_read_tokens": 900,
                                                "cache_creation_tokens": 0, "output_tokens": 4})
        responses = {"input_tokens": 4000, "input_tokens_details": {"cached_tokens": 3000}, "output_tokens": 9}
        self.assertEqual(usage_counts(responses), {"input_tokens": 1000, "cache_read_tokens": 3000,
                                                   "cache_creation_tokens": None, "output_tokens": 9})

    def test_missing_usage_is_all_none(self):
        self.assertIsNone(usage_counts(None))
        self.assertIsNone(usage_counts({}))
        self.assertEqual(response_usage_counts(None, {}), dict.fromkeys(core.USAGE_FIELDS))
        self.assertEqual(response_usage_counts(SimpleNamespace(), {"standard_logging_object": {"prompt_tokens": 7}}),
                         {"input_tokens": 7, "cache_read_tokens": None, "cache_creation_tokens": None,
                          "output_tokens": None})


class BreakpointTests(unittest.TestCase):
    def test_positions_in_prefix_order(self):
        data = {
            "tools": [{"name": "a"}, {"name": "b", "cache_control": {"type": "ephemeral"}}],
            "system": [{"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "q", "cache_control": {"type": "ephemeral"}}]},
                {"role": "assistant", "content": "a"},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t",
                                              "content": [{"type": "text", "text": "r",
                                                           "cache_control": {"type": "ephemeral"}}]}]},
            ],
        }
        self.assertEqual(AnthropicAdapter().cache_breakpoints(data), (4, "tools,system,m-3,m-1"))

    def test_string_system_and_no_markers(self):
        self.assertEqual(AnthropicAdapter().cache_breakpoints({"system": "s", "messages": [{"role": "user", "content": "q"}]}),
                         (0, ""))


class UsageEventFlowTests(Base):
    def events(self):
        path = self.ctx / "sessions" / f"{SID}.db"
        conn = sqlite3.connect(path)
        try:
            return [dict(zip(COLUMNS, r)) for r in conn.execute(f"SELECT {', '.join(COLUMNS)} FROM events ORDER BY id")]
        finally:
            conn.close()

    def test_usage_event_per_reported_call(self):
        msgs = history(3)
        msgs[-1] = {"role": "assistant", "content": [{"type": "text", "text": "answer", "cache_control": {"type": "ephemeral"}}]}
        self.run_req(msgs, call_id="u1")
        usage = SimpleNamespace(prompt_tokens=1500, completion_tokens=20, cache_read_input_tokens=1000,
                                cache_creation_input_tokens=200)
        process_usage({"litellm_call_id": "u1"}, SimpleNamespace(usage=usage), self.settings())
        self.assertEqual(self.events(), [{
            "kind": "usage", "model": "claude", "thread_root": fingerprint(msgs[0]), "trimmed": core.TRIM_NONE,
            "input_tokens": 300, "cache_read_tokens": 1000, "cache_creation_tokens": 200, "output_tokens": 20,
            "breakpoints": 1, "breakpoint_at": "m-1"}])

    def test_unreported_usage_records_nulls(self):
        self.run_req(history(2), call_id="u2")
        process_usage({"litellm_call_id": "u2"}, None, self.settings())
        [event] = self.events()
        self.assertEqual([event[c] for c in core.USAGE_FIELDS], [None] * 4)
        self.assertEqual((event["breakpoints"], event["breakpoint_at"]), (0, ""))

    def test_trimmed_marks_the_new_cut_then_the_kept_cut(self):
        msgs = history(4)
        self.run_req(msgs, call_id="t1", trim_at_tokens=1)
        process_usage({"litellm_call_id": "t1"}, {"usage": {"prompt_tokens": 10}}, self.settings())
        self.run_req(msgs, call_id="t2")
        process_usage({"litellm_call_id": "t2"}, {"usage": {"prompt_tokens": 10}}, self.settings())
        rows = self.events()
        self.assertEqual([(r["kind"], r["trimmed"]) for r in rows],
                         [("trim", None), ("usage", core.TRIM_NEW), ("usage", core.TRIM_KEPT)])

    def test_chat_route_has_no_breakpoint_count(self):
        from openmem_context import process_request
        data = {"model": "gpt", "messages": history(2), "litellm_call_id": "c1",
                "proxy_server_request": {"headers": {"x-claude-code-session-id": SID}}}
        process_request(data, "acompletion", self.settings())
        process_usage({"litellm_call_id": "c1"}, {"usage": {"prompt_tokens": 9, "completion_tokens": 1}}, self.settings())
        [event] = self.events()
        self.assertEqual((event["breakpoints"], event["breakpoint_at"], event["model"]), (None, None, "gpt"))

    def test_disabled_proxy_records_no_event(self):
        self.run_req(history(2), call_id="d1")
        process_usage({"litellm_call_id": "d1"}, {"usage": {"prompt_tokens": 9}}, self.settings(enabled=False))
        self.assertFalse((self.ctx / "sessions" / f"{SID}.db").exists() and self.events())


if __name__ == "__main__":
    unittest.main()
