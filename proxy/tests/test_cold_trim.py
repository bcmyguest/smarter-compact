"""CONTEXT_COLD_TRIM (a trim to the target after an idle gap longer than the prompt cache lives) and
CONTEXT_CACHE_TTL_SPLIT (message cache_control ttl "1h" sent as "5m" until the thread's Nth idle gap over 5 minutes).
Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import Base, anth_history, headers  # noqa: E402

from smarter_compact import Settings, core, process_request  # noqa: E402
from smarter_compact.adapters import AnthropicAdapter  # noqa: E402
from smarter_compact.core import process_usage  # noqa: E402

SID = "sess-1"
H1 = {"type": "ephemeral", "ttl": "1h"}
M5 = {"type": "ephemeral"}
T0 = 1_800_000_000


def anth(turns, ttl=H1, system_ttl=H1):
    """A Claude Code shaped request: 1h markers on tools and system, the newest user message marked."""
    msgs = anth_history(turns) + [{"role": "user", "content": [{"type": "text", "text": "next", "cache_control": ttl}]}]
    system = [{"type": "text", "text": "SYS", **({"cache_control": system_ttl} if system_ttl else {})}]
    return {"model": "claude-opus", "system": system, "messages": msgs,
            "tools": [{"name": "Bash", "input_schema": {}, **({"cache_control": system_ttl} if system_ttl else {})}],
            **headers(SID)}


def ttls(messages):
    """Every message-level and block-level cache_control ttl, in order ("5m" for a marker without one)."""
    out = []
    for m in messages if isinstance(messages, list) else ():
        if isinstance(m, dict):
            if isinstance(m.get("cache_control"), dict):
                out.append(m["cache_control"].get("ttl", "5m"))
            out += ttls(m.get("content"))
    return out


class ClockBase(Base):
    def settings(self, **kw):
        kw.setdefault("trim_at_tokens", 40_000)
        kw.setdefault("trim_to_tokens", 4_000)
        kw.setdefault("chars_per_token", 1.0)
        kw.setdefault("cold_trim_min_gain_tokens", 0)
        return super().settings(**kw)

    def at(self, seconds, data, call_type="anthropic_messages", **kw):
        with mock.patch.object(core, "_clock", return_value=T0 + seconds):
            return process_request(data, call_type, self.settings(**kw))

    def events(self, kind):
        conn = sqlite3.connect(self.ctx / "sessions" / f"{SID}.db")
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM events WHERE kind = ? ORDER BY id", (kind,))]
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    def state(self):
        path = self.ctx / "sessions" / f"{SID}.trim.json"
        return json.loads(path.read_text()) if path.exists() else {}


class SettingTests(unittest.TestCase):
    def test_defaults_and_parsing(self):
        s = Settings.from_env({})
        self.assertEqual((s.cold_trim, s.cold_trim_seconds, s.cold_trim_min_gain_tokens, s.cache_ttl_split),
                         (False, 3600, 5000, 0))
        s = Settings.from_env({"CONTEXT_COLD_TRIM": "1", "CONTEXT_COLD_TRIM_SECONDS": "600",
                               "CONTEXT_COLD_TRIM_MIN_GAIN_TOKENS": "100", "CONTEXT_CACHE_TTL_SPLIT": "2"})
        self.assertEqual((s.cold_trim, s.cold_trim_seconds, s.cold_trim_min_gain_tokens, s.cache_ttl_split),
                         (True, 600, 100, 2))
        s = Settings.from_env({"CONTEXT_COLD_TRIM_SECONDS": "0", "CONTEXT_CACHE_TTL_SPLIT": "-1"})
        self.assertEqual((s.cold_trim_seconds, s.cache_ttl_split), (3600, 0))


class AdapterTests(unittest.TestCase):
    def test_cache_ttl_seconds(self):
        a = AnthropicAdapter()
        self.assertEqual(a.cache_ttl_seconds(anth(1)), 3600)
        self.assertEqual(a.cache_ttl_seconds(anth(1, ttl=M5)), 300)
        self.assertEqual(a.cache_ttl_seconds({"messages": [{"role": "user", "content": "q"}]}), None)
        self.assertEqual(a.cache_ttl_seconds({"messages": [], "cache_control": H1}), 3600)

    def test_split_rewrites_message_markers_only(self):
        data = anth(1)
        data["messages"][0]["content"][0]["cache_control"] = dict(H1)
        data["messages"][2]["content"][0]["content"] = [{"type": "text", "text": "r", "cache_control": dict(H1)}]
        before = copy.deepcopy(data)
        out = AnthropicAdapter().split_cache_ttl(data)
        self.assertEqual(ttls(out["messages"]), ["5m", "5m", "5m"])
        self.assertEqual(out["system"], before["system"])
        self.assertEqual(out["tools"], before["tools"])
        self.assertEqual(out["messages"][-1]["content"][0]["cache_control"], {"type": "ephemeral", "ttl": "5m"})
        self.assertEqual(data, before)  # the input is never mutated

    def test_split_needs_a_stable_1h_marker(self):
        data = anth(1, system_ttl=None)
        self.assertIs(AnthropicAdapter().split_cache_ttl(data), data)
        data = anth(1, ttl=M5)
        self.assertIs(AnthropicAdapter().split_cache_ttl(data), data)


class ColdTrimTests(ClockBase):
    def test_off_gives_the_old_bytes(self):
        data = anth(20)
        expected = json.dumps(copy.deepcopy(data)["messages"])
        self.at(0, copy.deepcopy(data))
        out = self.at(100_000, copy.deepcopy(data))
        self.assertEqual(json.dumps(out["messages"]), expected)
        self.assertNotIn(core.LAST_REQUEST_KEY, self.state())
        self.assertEqual(self.events("trim"), [])

    def test_trims_only_after_the_1h_lifetime(self):
        self.at(0, anth(20), cold_trim=True)
        warm = self.at(3600, anth(20), cold_trim=True)
        self.assertEqual(json.dumps(warm["messages"]), json.dumps(anth(20)["messages"]))
        self.assertEqual(self.events("trim"), [])
        cold = self.at(3600 + 3601, anth(20), cold_trim=True)
        self.assertLess(len(cold["messages"]), len(anth(20)["messages"]))
        [event] = self.events("trim")
        self.assertEqual(event["location"], "cold")
        self.assertLess(event["tokens_after"], 4_000)
        self.assertGreater(event["tokens_before"], 4_000)
        # The cut is sticky: the next warm request renders the same view.
        again = self.at(3600 + 3611, anth(20), cold_trim=True)
        self.assertEqual(json.dumps(again["messages"]), json.dumps(cold["messages"]))
        self.assertEqual(len(self.events("trim")), 1)

    def test_5m_markers_expire_after_five_minutes(self):
        self.at(0, anth(20, ttl=M5, system_ttl=M5), cold_trim=True)
        self.at(299, anth(20, ttl=M5, system_ttl=M5), cold_trim=True)
        self.assertEqual(self.events("trim"), [])
        self.at(299 + 301, anth(20, ttl=M5, system_ttl=M5), cold_trim=True)
        self.assertEqual([e["location"] for e in self.events("trim")], ["cold"])

    def test_first_request_and_small_prompts_never_cold_trim(self):
        self.at(0, anth(20), cold_trim=True)
        self.assertEqual(self.events("trim"), [])
        self.at(100_000, anth(20), cold_trim=True, trim_to_tokens=39_000)
        self.assertEqual(self.events("trim"), [])

    def test_min_gain(self):
        self.at(0, anth(20), cold_trim=True)
        self.at(100_000, anth(20), cold_trim=True, cold_trim_min_gain_tokens=1_000_000)
        self.assertEqual(self.events("trim"), [])
        # CONTEXT_TRIM_MIN_GAIN_TOKENS applies too: the larger floor wins.
        self.at(200_000, anth(20), cold_trim=True, trim_min_gain_tokens=1_000_000)
        self.assertEqual(self.events("trim"), [])
        self.at(300_000, anth(20), cold_trim=True)
        self.assertEqual(len(self.events("trim")), 1)

    def test_threshold_trim_is_not_marked_cold(self):
        self.at(0, anth(20), cold_trim=True)
        self.at(100_000, anth(20), cold_trim=True, trim_at_tokens=5_000)
        [event] = self.events("trim")
        self.assertIsNone(event["location"])

    def test_chat_uses_the_configured_lifetime(self):
        msgs = [{"role": "user", "content": "task " + "x" * 300}]
        for k in range(20):
            msgs += [{"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{k}", "type": "function", "function": {"name": "bash", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": f"c{k}", "content": "out " + "o" * 400},
                {"role": "assistant", "content": f"done {k}"}, {"role": "user", "content": f"more {k}"}]

        def chat():
            return {"model": "gpt-5", "messages": copy.deepcopy(msgs), **headers(SID)}
        self.at(0, chat(), "acompletion", cold_trim=True, cold_trim_seconds=600)
        self.at(600, chat(), "acompletion", cold_trim=True, cold_trim_seconds=600)
        self.assertEqual(self.events("trim"), [])
        out = self.at(1201, chat(), "acompletion", cold_trim=True, cold_trim_seconds=600)
        self.assertLess(len(out["messages"]), len(msgs))
        self.assertEqual([e["location"] for e in self.events("trim")], ["cold"])

    def test_threads_keep_their_own_clock(self):
        other = anth(20)
        other["messages"][0]["content"][0]["text"] = "another thread " + "z" * 200
        self.at(0, anth(20), cold_trim=True)
        self.at(3000, copy.deepcopy(other), cold_trim=True)
        self.at(3700, anth(20), cold_trim=True)  # 3700 s after its own thread's last request
        self.at(3700 + 1000, copy.deepcopy(other), cold_trim=True)  # 1700 s: warm
        self.assertEqual(len(self.events("trim")), 1)


class TtlSplitTests(ClockBase):
    def test_off_gives_the_old_bytes(self):
        out = self.at(0, anth(3))
        self.assertEqual(json.dumps(out["messages"]), json.dumps(anth(3)["messages"]))

    def test_split_until_the_first_idle_gap(self):
        times = [0, 10, 200, 490, 900, 910]
        outs = [self.at(t, anth(3 + k), cache_ttl_split=1) for k, t in enumerate(times)]
        for out in outs[:4]:
            self.assertEqual(ttls(out["messages"]), ["5m"])
            self.assertEqual(out["system"][0]["cache_control"], H1)
            self.assertEqual(out["tools"][0]["cache_control"], H1)
        # 410 s idle: the tail expired; from this request on the client's 1h markers go up unchanged.
        for k in (4, 5):
            self.assertEqual(json.dumps(outs[k]["messages"]), json.dumps(anth(3 + k)["messages"]))
            self.assertEqual(ttls(outs[k]["messages"]), ["1h"])

    def test_n_idle_gaps(self):
        times = [0, 400, 410, 1000, 1010]
        outs = [self.at(t, anth(3), cache_ttl_split=2) for t in times]
        self.assertEqual([ttls(o["messages"]) for o in outs], [["5m"], ["5m"], ["5m"], ["1h"], ["1h"]])

    def test_byte_stable_between_requests(self):
        a = self.at(0, anth(3), cache_ttl_split=1)
        b = self.at(30, anth(3), cache_ttl_split=1)
        self.assertEqual(json.dumps(a), json.dumps(b))

    def test_other_formats_are_untouched(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "q", "cache_control": H1}]}]
        data = {"model": "claude", "messages": copy.deepcopy(msgs), **headers(SID)}
        out = self.at(0, data, "acompletion", cache_ttl_split=1)
        self.assertEqual(out["messages"], msgs)

    def test_split_makes_the_cold_lifetime_5m(self):
        # Split stays on through the first idle gap (N=2), so the 5m tail is cold after 301 s.
        self.at(0, anth(20), cache_ttl_split=2, cold_trim=True)
        out = self.at(301, anth(20), cache_ttl_split=2, cold_trim=True)
        self.assertEqual([e["location"] for e in self.events("trim")], ["cold"])
        self.assertEqual(ttls(out["messages"]), ["5m"])

    def test_usage_logs_writes_by_lifetime(self):
        data = anth(3)
        data["litellm_call_id"] = "u1"
        with mock.patch.object(core, "_clock", return_value=T0):
            process_request(data, "anthropic_messages", self.settings(cache_ttl_split=1))
        usage = {"input_tokens": 3, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 50,
                 "output_tokens": 7, "cache_creation": {"ephemeral_1h_input_tokens": 10, "ephemeral_5m_input_tokens": 40}}
        process_usage({"litellm_call_id": "u1"}, SimpleNamespace(usage=usage), self.settings())
        [event] = self.events("usage")
        self.assertEqual((event["cache_creation_1h_tokens"], event["cache_creation_5m_tokens"]), (10, 40))
        self.assertEqual(core.cache_write_split({"usage": {"input_tokens": 1}}), {})


class CacheWriteSplitShapeTests(unittest.TestCase):
    def test_litellm_model_response_prompt_tokens_details(self):
        # LiteLLM's ModelResponse for /v1/messages: Usage.prompt_tokens_details.cache_creation_token_details.
        details = SimpleNamespace(cache_creation_token_details=SimpleNamespace(
            ephemeral_5m_input_tokens=8112, ephemeral_1h_input_tokens=0))
        response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=8114, prompt_tokens_details=details))
        self.assertEqual(core.cache_write_split(response),
                         {"cache_creation_1h_tokens": 0, "cache_creation_5m_tokens": 8112})

    def test_no_details_is_empty(self):
        response = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=5, prompt_tokens_details=None))
        self.assertEqual(core.cache_write_split(response), {})


if __name__ == "__main__":
    unittest.main()
