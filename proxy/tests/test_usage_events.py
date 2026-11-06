"""Per-request usage events: cache reads and writes, trim state, cache_control breakpoints.
Run: python3 -m unittest discover -s proxy/tests"""

import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smarter_compact import core, process_usage  # noqa: E402
from smarter_compact.adapters import AnthropicAdapter  # noqa: E402
from smarter_compact.core import response_usage_counts, usage_counts  # noqa: E402
from smarter_compact.tokens import fingerprint  # noqa: E402
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
        from smarter_compact import process_request
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


def tool_history(result_text):
    return [{"role": "user", "content": "list files"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": result_text}]},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "next"}]


class ServerClearTests(UsageEventFlowTests):
    EDIT = {"type": "clear_tool_uses_20250919", "trigger": {"type": "input_tokens", "value": 100000},
            "keep": {"type": "tool_uses", "value": 3}, "clear_at_least": {"type": "input_tokens", "value": 5000}}

    def req(self, data_extra=None, model="claude-haiku-5-5", call_type="anthropic_messages", **kw):
        from smarter_compact import process_request
        data = {"model": model, "system": "SYSTEM", "messages": tool_history("x" * 50), "litellm_call_id": "s1",
                "proxy_server_request": {"headers": {"x-claude-code-session-id": SID}}, **(data_extra or {})}
        return process_request(data, call_type, self.settings(**kw), http=lambda *a: None)

    def test_env_defaults(self):
        from smarter_compact import Settings
        s = Settings.from_env({})
        self.assertEqual((s.server_clear, s.server_clear_trigger_tokens, s.server_clear_keep_tool_uses,
                          s.server_clear_at_least_tokens), (False, 100000, 3, 5000))
        s = Settings.from_env({"CONTEXT_SERVER_CLEAR": "1", "CONTEXT_SERVER_CLEAR_TRIGGER_TOKENS": "30000",
                               "CONTEXT_SERVER_CLEAR_KEEP_TOOL_USES": "5", "CONTEXT_SERVER_CLEAR_AT_LEAST_TOKENS": "0"})
        self.assertEqual((s.server_clear, s.server_clear_trigger_tokens, s.server_clear_keep_tool_uses,
                          s.server_clear_at_least_tokens), (True, 30000, 5, 0))

    def test_off_by_default(self):
        self.assertNotIn("context_management", self.req())

    def test_request_shape(self):
        self.assertEqual(self.req(server_clear=True)["context_management"], {"edits": [self.EDIT]})

    def test_zero_at_least_omits_the_gate(self):
        edit = self.req(server_clear=True, server_clear_at_least_tokens=0)["context_management"]["edits"][0]
        self.assertNotIn("clear_at_least", edit)

    def test_merges_after_claude_code_clear_thinking(self):
        # Claude Code 2.1.318 sends this on every Haiku 5.5 request with thinking (captured trial sessions).
        sent = {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
        data = self.req({"context_management": sent}, server_clear=True)
        self.assertEqual(data["context_management"]["edits"], [{"type": "clear_thinking_20251015", "keep": "all"}, self.EDIT])
        self.assertEqual(sent, {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]})

    def test_client_clear_tool_uses_wins(self):
        own = {"edits": [{"type": "clear_tool_uses_20250919", "keep": {"type": "tool_uses", "value": 9}}]}
        self.assertEqual(self.req({"context_management": own}, server_clear=True)["context_management"], own)

    def test_only_claude_on_the_anthropic_route(self):
        self.assertNotIn("context_management", self.req(model="gpt-5", server_clear=True))
        self.assertNotIn("context_management", self.req(call_type="acompletion", server_clear=True))

    def test_store_keeps_the_full_tool_result(self):
        self.req({"messages": tool_history("R" * 3000)}, server_clear=True)
        conn = sqlite3.connect(self.ctx / "sessions" / f"{SID}.db")
        try:
            [text] = [r[0] for r in conn.execute("SELECT text FROM messages WHERE kind = 'tool_result'")]
        finally:
            conn.close()
        self.assertEqual(text, "R" * 3000)

    def applied(self):
        conn = sqlite3.connect(self.ctx / "sessions" / f"{SID}.db")
        try:
            return [r[0] for r in conn.execute("SELECT applied_edits FROM events WHERE kind = 'usage'")]
        finally:
            conn.close()

    def test_applied_edits_from_a_response(self):
        self.req(server_clear=True)
        edits = [{"type": "clear_tool_uses_20250919", "cleared_tool_uses": 8, "cleared_input_tokens": 50000}]
        process_usage({"litellm_call_id": "s1"}, {"usage": {"input_tokens": 5}, "context_management": {"applied_edits": edits}},
                      self.settings())
        self.assertEqual(self.applied(), ['[{"cleared_input_tokens":50000,"cleared_tool_uses":8,"type":"clear_tool_uses_20250919"}]'])

    def test_applied_edits_from_a_stream_and_none_reported(self):
        self.req(server_clear=True)
        sse = ('event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
               '"usage":{"output_tokens":3},"context_management":{"applied_edits":[]}}\n\n'
               'event: message_stop\ndata: {"type":"message_stop"}\n\n')
        core.note_stream_edits("s1", sse.encode())
        process_usage({"litellm_call_id": "s1"}, {"usage": {"input_tokens": 5}}, self.settings())
        self.req({"litellm_call_id": "s2"}, server_clear=True)
        process_usage({"litellm_call_id": "s2"}, {"usage": {"input_tokens": 5}}, self.settings())
        self.assertEqual(self.applied(), ["[]", None])
        self.assertEqual(len(core._stream_edits), 0)

    def test_stream_parse_ignores_other_events(self):
        self.assertIsNone(core.stream_applied_edits(b'data: {"type":"message_start","context_management":1}\n'))
        self.assertIsNone(core.stream_applied_edits(b"data: not json context_management\n"))
