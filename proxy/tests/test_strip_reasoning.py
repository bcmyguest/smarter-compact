"""CONTEXT_STRIP_OLD_REASONING: a trim strips reasoning from every assistant message but the latest, never dropping a
message. Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smarter_compact import Settings, process_request  # noqa: E402
from smarter_compact.adapters import wrap_note  # noqa: E402
from smarter_compact.core import reasoning_text  # noqa: E402
from smarter_compact.tokens import request_chars, signed_chars  # noqa: E402

SID = "sess-strip"
SIG = 4000  # signature characters per thinking block, billed like text
THINKING_TYPES = ("thinking", "redacted_thinking")


def headers(sid=SID):
    return {"proxy_server_request": {"headers": {"X-Claude-Code-Session-Id": sid}}}


def step(k, sig=SIG):
    """One Claude Code step: thinking (empty text, large signature) and a tool_use, then its tool_result."""
    return [{"role": "assistant", "content": [{"type": "thinking", "thinking": "", "signature": f"{k}" * sig},
                                              {"type": "text", "text": f"step {k}"},
                                              {"type": "tool_use", "id": f"t{k}", "name": "Bash",
                                               "input": {"command": f"echo {k}"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{k}", "content": f"out {k}"}]}]


def loop(steps, sig=SIG):
    """One user prompt, then a tool loop: the whole task is a single assistant turn."""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "the task"}]}]
    for k in range(steps):
        msgs += step(k, sig)
    return msgs


def request(msgs, sid=SID):
    return {"model": "claude", "system": [{"type": "text", "text": "SYS"}], "tools": [{"name": "Bash"}],
            "messages": msgs, **headers(sid)}


def full_tokens(data):
    """The estimate at 1 char per token with signatures charged (the on rule)."""
    parts = [data["system"], data["tools"], data["messages"]]
    return request_chars(*parts) + sum(signed_chars(p) for p in parts)


def kinds(m):
    return [b.get("type") for b in m["content"]] if isinstance(m.get("content"), list) else ["str"]


class StripOldReasoningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Path(self.tmp.name) / "context"

    def settings(self, **kw):
        base = dict(compress_over_tokens=0, context_dir=self.ctx, standing_instruction=False, strip_prose=False,
                    progress_note=False, mask_observations=False, scratchpad=False, chars_per_token=1.0,
                    clear_keep_results=0, trim_at_tokens=10_000_000)
        base.update(kw)
        base.setdefault("trim_to_tokens", math.ceil(0.5 * base["trim_at_tokens"]))
        return Settings(**base)

    def send(self, data, call_type="anthropic_messages", **kw):
        out = process_request(copy.deepcopy(data), call_type, self.settings(**kw))
        return out["input"] if call_type == "aresponses" else out["messages"]

    def trim(self, msgs, sid=SID, **kw):
        data = request(msgs, sid)
        return self.send(data, trim_at_tokens=full_tokens(data) - 1, **kw)

    def test_single_turn_loop_strips_all_but_the_latest_message(self):
        msgs = loop(5)
        view = self.trim(msgs)
        # Every message stays; the note rides on the task.
        self.assertEqual([m["role"] for m in view], [m["role"] for m in msgs])
        self.assertEqual(view[0]["content"],
                         [{"type": "text", "text": wrap_note(reasoning_text(SID))}, *msgs[0]["content"]])
        for j in range(1, 9, 2):
            self.assertEqual(view[j]["content"], msgs[j]["content"][1:])
            self.assertEqual(view[j + 1], msgs[j + 1])
        self.assertEqual(json.dumps(view[9:]), json.dumps(msgs[9:]))

    def test_message_holding_only_reasoning_keeps_it(self):
        msgs = loop(2)
        msgs[3:3] = [{"role": "assistant", "content": [{"type": "redacted_thinking", "data": "R" * 100}]},
                     {"role": "user", "content": [{"type": "text", "text": "go on"}]}]
        msgs += step(9)
        view = self.trim(msgs)
        self.assertEqual(len(view), len(msgs))
        self.assertEqual(view[3], msgs[3])
        self.assertEqual(kinds(view[1]), ["text", "tool_use"])
        self.assertEqual(json.dumps(view[-2:]), json.dumps(msgs[-2:]))

    def test_rerender_is_byte_identical_and_later_reasoning_stays(self):
        msgs = loop(5)
        first = self.trim(msgs)
        more = msgs + step(5) + step(6)
        data = request(more)
        later = self.send(data, trim_at_tokens=10 * full_tokens(data))
        self.assertEqual(json.dumps(later[:len(first)]), json.dumps(first))
        # Reasoning produced after the trim goes out whole until the next trim.
        self.assertEqual(json.dumps(later[len(first):]), json.dumps(more[len(first):]))

    def test_off_drops_in_turn_exchanges_as_before(self):
        msgs = loop(5)
        data = request(msgs)
        at = request_chars(data["system"], data["tools"], msgs) - 1
        # Off, the old rule: signatures uncharged, no strip-only trim, so the in-turn gap drops exchanges.
        off = self.send(data, trim_at_tokens=at, strip_old_reasoning=False)
        self.assertLess(len(off), len(msgs))
        self.assertIn("earlier messages are NOT in your context", off[0]["content"][0]["text"])
        self.assertEqual(json.dumps(off[-2:]), json.dumps(msgs[-2:]))

    def test_off_still_drops_a_message_left_empty(self):
        msgs = [{"role": "user", "content": "task"}, *step(0)[:1],
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t0", "content": "r0"}]},
                {"role": "assistant", "content": [{"type": "redacted_thinking", "data": "R1"}]},
                {"role": "user", "content": "now this"}, *step(1), *step(2)]
        off = self.send(request(msgs), trim_at_tokens=1, strip_old_reasoning=False)
        self.assertNotIn(msgs[3], off)
        on = self.send(request(msgs, "sid-on"), trim_at_tokens=1)
        self.assertIn(msgs[3], on)

    def test_estimate_charges_signatures_only_when_on(self):
        msgs = loop(5)
        data = request(msgs)
        bare = request_chars(data["system"], data["tools"], msgs)
        # Between the size without and with signatures: only the on rule reaches the threshold.
        at = bare + signed_chars(msgs) // 2
        self.assertEqual(json.dumps(self.send(data, trim_at_tokens=at, strip_old_reasoning=False)), json.dumps(msgs))
        self.assertNotEqual(json.dumps(self.send(request(msgs, "sid-on"), trim_at_tokens=at)), json.dumps(msgs))

    def test_settles_without_dropping_turns_across_user_turns(self):
        done = [{"type": "thinking", "thinking": "", "signature": "z" * SIG}, {"type": "text", "text": "done"}]
        msgs = loop(3) + [{"role": "assistant", "content": done},
                          {"role": "user", "content": [{"type": "text", "text": "next task"}]}] + step(7)[:1]
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t7", "content": "r"}]})
        view = self.trim(msgs)
        self.assertEqual(len(view), len(msgs))
        for j, m in enumerate(view[:-2]):
            if m["role"] == "assistant":
                self.assertFalse(set(kinds(m)) & set(THINKING_TYPES), j)
        self.assertEqual(json.dumps(view[-2:]), json.dumps(msgs[-2:]))

    def test_chat_thinking_blocks_stripped_and_unsigned_reasoning_kept(self):
        msgs = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "task"}]
        for k in range(4):
            msgs += [{"role": "assistant", "content": None, "reasoning_content": f"why {k}",
                      "thinking_blocks": [{"type": "thinking", "thinking": f"why {k}", "signature": f"{k}" * SIG}],
                      "tool_calls": [{"id": f"c{k}", "type": "function",
                                      "function": {"name": "ls", "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": f"c{k}", "content": f"files {k}"}]
        # A reasoning-only message (no content, no call) keeps its thinking; DeepSeek-style unsigned reasoning stays.
        msgs.insert(4, {"role": "assistant", "content": "", "thinking_blocks": [
            {"type": "thinking", "thinking": "only", "signature": "O" * SIG}]})
        msgs.insert(5, {"role": "user", "content": "go on"})
        msgs.insert(6, {"role": "assistant", "content": "plain", "reasoning_content": "unsigned"})
        msgs.insert(7, {"role": "user", "content": "more"})
        data = {"messages": msgs, **headers()}
        at = request_chars(msgs) + signed_chars(msgs) - 1
        view = self.send(data, "acompletion", trim_at_tokens=at)
        self.assertEqual(len(view), len(msgs))
        self.assertEqual(view[4], msgs[4])
        self.assertEqual(view[6], msgs[6])
        for j in (2, 8, 10):
            self.assertNotIn("thinking_blocks", view[j])
            self.assertEqual(view[j]["tool_calls"], msgs[j]["tool_calls"])
        self.assertEqual(json.dumps(view[-2:]), json.dumps(msgs[-2:]))

    def test_responses_litellm_reasoning_dropped_before_the_latest_step(self):
        def enc(k):
            if k == 1:
                return "gAAAA-openai"
            return json.dumps([{"type": "thinking", "thinking": "", "signature": f"{k}" * SIG}], separators=(",", ":"))
        items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "task"}]}]
        for k in range(4):
            items += [{"type": "reasoning", "id": f"r{k}", "summary": [], "encrypted_content": enc(k)},
                      {"type": "function_call", "call_id": f"f{k}", "name": "shell", "arguments": "{}"},
                      {"type": "function_call_output", "call_id": f"f{k}", "output": f"out {k}"}]
        data = {"input": items, **headers()}
        view = self.send(data, "aresponses", trim_at_tokens=request_chars(items) - 1)
        # A reasoning item is reasoning, not a message: LiteLLM-encoded ones before the latest step go, every call
        # stays; OpenAI's own reasoning (step 1) and the latest step (3) go out as sent.
        self.assertEqual([x.get("id") for x in view if x["type"] == "reasoning"], ["r1", "r3"])
        self.assertEqual([x["call_id"] for x in view if x["type"] == "function_call"], ["f0", "f1", "f2", "f3"])
        self.assertEqual(json.dumps(view[-3:]), json.dumps(items[-3:]))


if __name__ == "__main__":
    unittest.main()
