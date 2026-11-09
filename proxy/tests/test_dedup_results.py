"""CONTEXT_DEDUP_RESULTS: a new tool result identical to one still shown in full becomes a pointer to it.
Run: python3 -m unittest discover -s proxy/tests"""

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_context_proxy import SID, Base, anth_request, headers  # noqa: E402

from smarter_compact import Settings, ttl  # noqa: E402
from smarter_compact.compress import DEDUP_MIN_CHARS, DEDUP_PREFIX, MASK_PREFIX, blob_id, dedup_placeholder  # noqa: E402
from smarter_compact.adapters import wrap_note  # noqa: E402
from smarter_compact.ledger import LEDGER_HEAD  # noqa: E402

OUT = "line of output\n" * 40  # 600 chars
POINTER = dedup_placeholder(OUT)
PROXY_POINTER = ('[smarter-compact: output identical to an earlier tool result still shown above; '
                 'smart_compact_get(id="646c9063dd68a784") returns it]')
# The CONTEXT_LEDGER block (on by default) on a new batch: every call reads one file, whole, unchanged since.
LEDGER = f"{LEDGER_HEAD}\nRead (latest last):\n- log.txt: whole current"
LEDGER_X = f"{LEDGER_HEAD}\nRead (latest last):\n- x: whole current"


def anth_turns(outs):
    """The task, then one Bash call and its result per text in outs."""
    msgs = [{"role": "user", "content": [{"type": "text", "text": "the task"}]}]
    for k, out in enumerate(outs):
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{k}", "name": "Bash", "input": {"command": "cat log.txt"}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{k}", "content": out}]})
    return msgs


def ledgered(msg, text=LEDGER):
    """msg with the ledger block appended as a text block (Anthropic)."""
    return {**msg, "content": [*msg["content"], {"type": "text", "text": wrap_note(text)}]}


def anth_results(view):
    return {b["tool_use_id"]: b["content"] for m in view if isinstance(m.get("content"), list)
            for b in m["content"] if b.get("type") == "tool_result"}


def chat_turns(outs):
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "the task"}]
    for k, out in enumerate(outs):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{k}", "type": "function", "function": {"name": "bash", "arguments": "{\"command\":\"cat x\"}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{k}", "content": out})
    return msgs


def responses_turns(outs):
    items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "the task"}]}]
    for k, out in enumerate(outs):
        items.append({"type": "function_call", "call_id": f"r{k}", "name": "shell", "arguments": "{\"cmd\":\"cat x\"}"})
        items.append({"type": "function_call_output", "call_id": f"r{k}", "output": out})
    return items


class DedupBase(Base):
    def settings(self, **kw):
        kw.setdefault("dedup_results", True)
        kw.setdefault("clear_keep_results", 0)
        kw.setdefault("strip_old_reasoning", False)
        return super().settings(**kw)

    def anth(self, msgs, **kw):
        return self.run_req(anth_request(copy.deepcopy(msgs)), **kw)["messages"]

    def blob(self, text):
        return (self.ctx / "blobs" / f"{blob_id(text)}.txt").read_text()


class SettingTests(DedupBase):
    def test_default_on_and_zero_off(self):
        self.assertTrue(Settings.from_env({}).dedup_results)
        self.assertFalse(Settings.from_env({"CONTEXT_DEDUP_RESULTS": "0"}).dedup_results)

    def test_placeholder_is_shorter_than_the_minimum(self):
        self.assertTrue(POINTER.startswith(DEDUP_PREFIX))
        self.assertLess(len(POINTER), DEDUP_MIN_CHARS)
        self.assertIn(f'smart_compact_get(id="{blob_id(OUT)}")', POINTER)


class AnthropicTests(DedupBase):
    def test_only_the_newer_copy_becomes_the_pointer(self):
        msgs = anth_turns([OUT])
        self.assertEqual(self.anth(msgs), [*msgs[:2], ledgered(msgs[2])])
        msgs = anth_turns([OUT, OUT])
        view = self.anth(msgs)
        self.assertEqual(view[:4], [*msgs[:2], ledgered(msgs[2]), msgs[3]])
        self.assertEqual(anth_results(view), {"t0": OUT, "t1": POINTER})
        self.assertEqual(self.blob(OUT), OUT)

    def test_off_gives_the_old_bytes(self):
        msgs = anth_turns([OUT, OUT])
        # ledger=False too: on by default, it would turn the repeated cat into its own reuse pointer.
        self.assertEqual(json.dumps(self.anth(msgs, dedup_results=False, ledger=False)), json.dumps(msgs))
        # A recorded decision is ignored when the setting is off.
        self.anth(msgs)
        self.assertEqual(json.dumps(self.anth(msgs, dedup_results=False, ledger=False)), json.dumps(msgs))

    def test_byte_identical_on_every_later_request(self):
        msgs = anth_turns([OUT, OUT])
        first = self.anth(msgs)
        msgs += [{"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                 {"role": "user", "content": [{"type": "text", "text": "next"}]}]
        again = self.anth(msgs)
        self.assertEqual(json.dumps(again[:len(first)]), json.dumps(first))

    def test_short_different_and_already_sent_results_stay(self):
        short = "x" * (DEDUP_MIN_CHARS - 1)
        shorts = anth_turns([short, short])
        self.assertEqual(self.anth(shorts), [*shorts[:-1], ledgered(shorts[-1])])
        self.ctx = self.ctx.with_name("ctx-2")
        # A changed re-read stays whole: the ledger points only at a byte-identical copy.
        changed = anth_turns([OUT, OUT + "!"])
        self.assertEqual(self.anth(changed), [*changed[:-1], ledgered(changed[-1])])
        # Duplicates that are no longer the newest result were sent whole already: never edited.
        self.ctx = self.ctx.with_name("ctx-3")
        msgs = anth_turns([OUT, OUT]) + [{"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
        self.assertEqual(self.anth(msgs), msgs)

    def test_earlier_copy_not_shown_in_full_means_no_pointer(self):
        msgs = anth_turns([OUT, OUT])
        view = self.anth(msgs, trim_at_tokens=1, clear_keep_results=1)
        results = anth_results(view)
        self.assertTrue(results["t0"].startswith(MASK_PREFIX))
        self.assertEqual(results["t1"], OUT)

    def test_pointer_survives_when_the_earlier_copy_is_cleared_or_dropped(self):
        self.anth(anth_turns([OUT, OUT]))
        msgs = anth_turns([OUT, OUT, "other " + "y" * 300, "more " + "z" * 300])
        cleared = self.anth(msgs, trim_at_tokens=600, clear_keep_results=1, trim_to_tokens=590)
        results = anth_results(cleared)
        self.assertTrue(results["t0"].startswith(MASK_PREFIX))
        self.assertEqual(results["t1"], POINTER)
        later = self.anth(msgs, trim_at_tokens=600, clear_keep_results=1, trim_to_tokens=590)
        self.assertEqual(json.dumps(later), json.dumps(cleared))
        # A trim that drops the earlier copy's turn still renders the same pointer.
        self.ctx = self.ctx.with_name("ctx-drop")
        self.anth(anth_turns([OUT, OUT]))
        dropped = self.anth(msgs, trim_at_tokens=500, clear_keep_results=1, trim_to_tokens=400)
        self.assertNotIn("t0", anth_results(dropped))
        self.assertEqual(anth_results(dropped)["t1"], POINTER)
        self.assertEqual(self.blob(OUT), OUT)

    def test_sweep_keeps_the_blob(self):
        self.anth(anth_turns([OUT, OUT]))
        ttl.sweep_orphan_blobs(self.ctx)
        self.assertEqual(self.blob(OUT), OUT)

    def test_parallel_identical_results_in_one_message(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "the task"}]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "cat x"}},
                    {"type": "tool_use", "id": "b", "name": "Bash", "input": {"command": "cat x"}}]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": [{"type": "text", "text": OUT}]},
                    {"type": "tool_result", "tool_use_id": "b", "is_error": False, "content": [
                        {"type": "text", "text": OUT, "cache_control": {"type": "ephemeral"}}]}]}]
        view = self.anth(msgs)
        self.assertEqual(view[2]["content"][0], msgs[2]["content"][0])
        self.assertEqual(view[2]["content"][1], {"type": "tool_result", "tool_use_id": "b", "is_error": False, "content": [
            {"type": "text", "text": POINTER, "cache_control": {"type": "ephemeral"}}]})

    def test_ledger_on_gives_the_plugin_pointer_and_dedup_decision(self):
        # Same literal as the plugin's test (tests/context-trim.test.mjs): both sides render these bytes.
        self.anth(anth_turns([OUT]), ledger=True)
        view = self.anth(anth_turns([OUT, OUT]), ledger=True)
        self.assertEqual(anth_results(view)["t1"], PROXY_POINTER)
        state = json.loads((self.ctx / "sessions" / f"{SID}.trim.json").read_text())
        self.assertEqual([(d.get("kind"), d["at"], d["id"], d["src"], d["bid"]) for d in state["dedup"]],
                         [(None, 4, "t1", "t0", blob_id(OUT))])

    def test_state_records_the_decision_by_fingerprint(self):
        self.anth(anth_turns([OUT, OUT]))
        state = json.loads((self.ctx / "sessions" / f"{SID}.trim.json").read_text())
        [d] = state["dedup"]
        self.assertEqual((d["at"], d["id"], d["src"], d["bid"]), (4, "t1", "t0", blob_id(OUT)))
        self.assertEqual(len(d["fp"]), 32)


class OtherFormatTests(DedupBase):
    def test_chat_tool_messages(self):
        msgs = chat_turns([OUT, OUT])
        out = self.run_req({"messages": copy.deepcopy(msgs), **headers()}, "acompletion")["messages"]
        self.assertEqual(out[:-1], msgs[:-1])
        self.assertEqual(out[-1], {**msgs[-1], "content": f"{POINTER}\n\n{wrap_note(LEDGER_X)}"})
        again = self.run_req({"messages": copy.deepcopy(msgs) + [{"role": "user", "content": "go"}], **headers()},
                             "acompletion")["messages"]
        self.assertEqual(again[:len(out)], out)
        off = self.run_req({"messages": copy.deepcopy(msgs), **headers()}, "acompletion", dedup_results=False,
                           ledger=False)
        self.assertEqual(off["messages"], msgs)

    def test_responses_function_call_output(self):
        items = responses_turns([OUT, OUT])
        out = self.run_req({"input": copy.deepcopy(items), **headers()}, "aresponses")["input"]
        self.assertEqual(out[:-1], items[:-1])
        self.assertEqual(out[-1], {**items[-1], "output": f"{POINTER}\n\n{wrap_note(LEDGER_X)}"})
        again = self.run_req({"input": copy.deepcopy(items) + [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "go"}]}], **headers()},
            "aresponses")["input"]
        self.assertEqual(again[:len(out)], out)
        off = self.run_req({"input": copy.deepcopy(items), **headers()}, "aresponses", dedup_results=False,
                           ledger=False)
        self.assertEqual(off["input"], items)
        self.assertEqual(self.blob(OUT), OUT)


if __name__ == "__main__":
    unittest.main()
