"""Codex through LiteLLM's ChatGPT-subscription pass-through. Run: python3 -m unittest discover -s proxy/tests"""

import copy
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openmem_context import Settings, core, process_request, process_usage  # noqa: E402
from openmem_context.adapters import (  # noqa: E402
    CODEX_RESPONSES_ROUTE, ResponsesAdapter, UnsupportedCallType, adapter_for,
)
from openmem_context.core import STANDING_INSTRUCTION  # noqa: E402

SID = "codex-thread-1"
BIG = "tool output line\n" * 400


def codex_body(sid=SID):
    """The body Codex posts to /backend-api/codex/responses, as LiteLLM hands it to the pre-call hook."""
    return {
        "model": "gpt-6-sol", "instructions": "You are Codex.", "stream": True, "store": False,
        "prompt_cache_key": sid,
        "input": [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "sandbox rules"}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "list the files"}]},
            {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": "{\"command\":[\"ls\"]}"},
            {"type": "function_call_output", "call_id": "c1", "output": BIG},
        ],
        "litellm_logging_obj": object(),
    }


def no_http(*args):
    raise AssertionError("no registration, so no recall")


class AdapterSelectionTests(unittest.TestCase):
    def test_codex_route_uses_the_responses_adapter(self):
        self.assertIsInstance(adapter_for("pass_through_endpoint", CODEX_RESPONSES_ROUTE), ResponsesAdapter)
        self.assertIsInstance(adapter_for("pass_through_endpoint", CODEX_RESPONSES_ROUTE + "/"), ResponsesAdapter)

    def test_other_pass_through_routes_are_opaque(self):
        for route in (None, "", "/backend-api/codex/models", "/anthropic/v1/messages", "/openai/v1/responses"):
            with self.subTest(route=route), self.assertRaises(UnsupportedCallType):
                adapter_for("pass_through_endpoint", route)

    def test_route_does_not_change_regular_call_types(self):
        self.assertIsInstance(adapter_for("aresponses", "/v1/responses"), ResponsesAdapter)


class PassThroughRequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Path(self.tmp.name) / "context"
        core._pending.clear()
        self.addCleanup(core._pending.clear)

    def settings(self, **kw):
        base = dict(trim_at_tokens=10 ** 9, compress_over_tokens=50, context_dir=self.ctx, standing_instruction=True)
        base.update(kw)
        return Settings(**base)

    def run_codex(self, data, route=CODEX_RESPONSES_ROUTE, call_id="call-codex", **kw):
        return process_request(data, "pass_through_endpoint", self.settings(**kw), http=no_http, route=route,
                               call_id=call_id)

    def test_codex_body_is_rewritten_in_place(self):
        data = codex_body()
        out = self.run_codex(data)
        self.assertIs(out, data)
        self.assertTrue(out["instructions"].startswith("You are Codex."))
        self.assertIn(STANDING_INSTRUCTION, out["instructions"])
        output = out["input"][3]["output"]
        self.assertTrue(output.startswith("[openmem: tool result compressed"), output[:80])
        self.assertEqual(out["input"][:3], codex_body()["input"][:3])

    def test_call_id_stays_out_of_the_forwarded_body(self):
        data = self.run_codex(codex_body())
        self.assertNotIn("litellm_call_id", data)
        self.assertIn("call-codex", core._pending)

    def test_session_is_the_prompt_cache_key_and_messages_are_stored(self):
        self.run_codex(codex_body())
        db = self.ctx / "sessions" / f"{SID}.db"
        self.assertTrue(db.exists())
        con = sqlite3.connect(db)
        try:
            rows = con.execute("SELECT role, kind, name FROM messages ORDER BY seq").fetchall()
        finally:
            con.close()
        self.assertEqual(rows, [("user", "text", None), ("assistant", "tool_use", "shell"),
                                ("tool", "tool_result", "shell")])

    def test_usage_is_calibrated_from_the_logging_call_id(self):
        self.run_codex(codex_body(), call_id="call-usage")
        process_usage({"litellm_call_id": "call-usage"}, {"usage": {"input_tokens": 777}}, self.settings())
        state = core.Store(self.ctx).load_trim(SID)
        self.assertEqual(state["usage"]["last"]["tokens"], 777)

    def test_other_pass_through_route_is_left_alone(self):
        data = codex_body()
        before = {k: copy.deepcopy(v) for k, v in data.items() if k != "litellm_logging_obj"}
        out = self.run_codex(data, route="/openai/v1/responses")
        self.assertEqual({k: v for k, v in out.items() if k != "litellm_logging_obj"}, before)
        self.assertFalse((self.ctx / "sessions").exists())
        self.assertEqual(core._pending, {})


if __name__ == "__main__":
    unittest.main()
