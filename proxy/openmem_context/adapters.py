"""Request-format adapters: Anthropic Messages, OpenAI Chat Completions, OpenAI Responses.

Every adapter works on the list of conversation items (messages or Responses input items) and never mutates
its inputs: methods that change an item return a shallow copy.
"""

import json
import re

from .compress import compress_text, compressed_blob_id

_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_RESULT_TYPES = ("function_call_output", "custom_tool_call_output", "local_shell_call_output")
_CALL_TYPES = ("function_call", "custom_tool_call", "local_shell_call")
# LiteLLM's ChatGPT-subscription pass-through (PR #41522): Codex posts Responses bodies here, and the pre-call hook
# sees them with call type "pass_through_endpoint".
CODEX_RESPONSES_ROUTE = "/backend-api/codex/responses"
# Anthropic thinking blocks: each carries a signature over the conversation before it, so an edited prefix voids it.
THINKING_TYPES = ("thinking", "redacted_thinking")


def _cache_markers(item):
    """cache_control markers on a message and its content blocks, tool_result inner blocks included."""
    count = 1 if item.get("cache_control") else 0
    content = item.get("content")
    for b in content if isinstance(content, list) else ():
        if isinstance(b, dict):
            count += _cache_markers(b)
    return count


class UnsupportedCallType(LookupError):
    """No adapter handles this LiteLLM call type."""


def _compact_json(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


def _parts_text(parts, text_types):
    """Join text parts; None when a non-text part (image, file) is present."""
    texts = []
    for part in parts:
        if not isinstance(part, dict) or part.get("type") not in text_types:
            return None
        texts.append(str(part.get("text", "")))
    return "\n".join(texts)


def _plain_text(content, text_types):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, dict) and part.get("type") in text_types:
                out.append(str(part.get("text", "")))
            elif isinstance(part, dict):
                out.append(f"[{part.get('type', 'part')}]")
        return "\n".join(out)
    return ""


def _log_result_text(text):
    """Log text for a tool result; compressed results reference their blob."""
    bid = compressed_blob_id(text)
    if bid is None:
        return text
    return f"[blob:{bid}]\n" + text.split("\n", 1)[1] if "\n" in text else f"[blob:{bid}]"


def _compress_content(content, over_tokens, text_type):
    """Compressed replacement for a tool-result content value, or None. Returns (new_content, bid, text)."""
    if isinstance(content, str):
        text = content
        res = compress_text(text, over_tokens)
        if res is None:
            return None
        return res[0], res[1], text
    if isinstance(content, list) and content:
        text = _parts_text(content, (text_type, "text"))
        if text is None:
            return None
        res = compress_text(text, over_tokens)
        if res is None:
            return None
        block = {"type": content[-1].get("type", text_type), "text": res[0]}
        if "cache_control" in content[-1]:
            block["cache_control"] = content[-1]["cache_control"]
        return [block], res[1], text
    return None


def _without_thinking_blocks(content):
    """Content with its thinking blocks removed, or the same object when it has none."""
    if not isinstance(content, list):
        return content
    kept = [b for b in content if not (isinstance(b, dict) and b.get("type") in THINKING_TYPES)]
    return content if len(kept) == len(content) else kept


def _litellm_thinking(encrypted):
    """Whether a Responses reasoning item's encrypted_content is LiteLLM's encoding of signed Anthropic thinking
    blocks (a JSON list holding a thinking block with a signature or a redacted_thinking block with data)."""
    if not isinstance(encrypted, str) or not encrypted.strip().startswith("["):
        return False
    try:
        blocks = json.loads(encrypted)
    except ValueError:
        return False
    return isinstance(blocks, list) and any(
        isinstance(b, dict) and ((b.get("type") == "thinking" and b.get("signature"))
                                 or (b.get("type") == "redacted_thinking" and b.get("data")))
        for b in blocks)


def strip_reminders(text):
    return _REMINDER_RE.sub("", text).strip()


class AnthropicAdapter:
    name = "anthropic"
    items_key = "messages"
    context_keys = ("system", "tools")
    text_type = "text"

    def get_items(self, data):
        return data.get("messages")

    def is_pinned(self, item):
        return False

    def _blocks(self, item):
        content = item.get("content")
        return content if isinstance(content, list) else None

    def is_turn_start(self, item):
        if item.get("role") != "user":
            return False
        content = item.get("content")
        if isinstance(content, str):
            return bool(content)
        if isinstance(content, list) and content:
            return not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
        return False

    def is_first_user(self, item):
        """A user message that is not only tool results (the task statement, when it is the first)."""
        if item.get("role") != "user":
            return False
        content = item.get("content")
        if isinstance(content, str):
            return bool(content)
        if isinstance(content, list):
            return any(not (isinstance(b, dict) and b.get("type") == "tool_result") for b in content)
        return False

    def call_ids(self, item):
        return {b.get("id") for b in self._blocks(item) or () if isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use")}

    def result_ids(self, item):
        return {b.get("tool_use_id") for b in self._blocks(item) or () if isinstance(b, dict) and b.get("type") == "tool_result"}

    def is_reply(self, item):
        """An agent message that calls no tool (the answer that closes a turn)."""
        return item.get("role") == "assistant" and not self.call_ids(item)

    def is_step_start(self, items, j):
        """Whether items[j] opens one tool exchange of a turn (where an in-turn cut may resume)."""
        return items[j].get("role") == "assistant"

    def is_assistant(self, item):
        return item.get("role") == "assistant"

    def without_reasoning(self, item):
        """The assistant message without thinking blocks: the same object when it has none, None when nothing
        else is left (the API merges the neighbours of a dropped message, and no tool_use goes with it)."""
        content = _without_thinking_blocks(item.get("content"))
        if content is item.get("content"):
            return item
        return {**item, "content": content} if content else None

    def tool_names(self, items):
        names = {}
        for item in items:
            for b in self._blocks(item) or ():
                if isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use"):
                    names[b.get("id")] = b.get("name")
        return names

    def system_text(self, data):
        return _plain_text(data.get("system", ""), ("text",))

    def cache_breakpoints(self, data):
        """(count, positions) of the request's cache_control markers: "tools", "system" or "m-<k>" (the k-th message
        from the end, 1 = last), in prefix order. A tool_result's inner blocks count for their message."""
        at = []
        for key in ("tools", "system"):
            value = data.get(key)
            at += [key] * sum(1 for b in value if isinstance(b, dict) and b.get("cache_control")) \
                if isinstance(value, list) else []
        messages = data.get("messages")
        messages = messages if isinstance(messages, list) else []
        for j, item in enumerate(messages):
            if isinstance(item, dict):
                at += [f"m-{len(messages) - j}"] * _cache_markers(item)
        return len(at), ",".join(at)

    def add_instruction(self, data, text):
        """Append one text block to `system`; a string system becomes its own leading block."""
        system = data.get("system")
        if isinstance(system, list):
            blocks = list(system)
        elif isinstance(system, str) and system:
            blocks = [{"type": "text", "text": system}]
        else:
            blocks = []
        data["system"] = [*blocks, {"type": "text", "text": text}]

    def user_text(self, item):
        return _plain_text(item.get("content", ""), ("text",))

    def compress_item(self, item, over_tokens, on_blob):
        if item.get("role") != "user":
            return item
        blocks = self._blocks(item)
        if not blocks:
            return item
        new_blocks = None
        for i, b in enumerate(blocks):
            if not (isinstance(b, dict) and b.get("type") == "tool_result"):
                continue
            res = _compress_content(b.get("content"), over_tokens, "text")
            if res is None:
                continue
            on_blob(res[1], res[2])
            if new_blocks is None:
                new_blocks = list(blocks)
            new_blocks[i] = {**b, "content": res[0]}
        return item if new_blocks is None else {**item, "content": new_blocks}

    def prepend_note(self, item, note):
        # Always list form: Claude Code sends the same turn as a text block first and as a string later, and both
        # must render to the same blocks (cache_control aside) to keep the prompt-cache prefix.
        content = item.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content)
        return {**item, "content": [{"type": "text", "text": note}, *blocks]}

    def rows(self, item, names):
        role = item.get("role", "user")
        content = item.get("content")
        if isinstance(content, str):
            return [(role, "text", None, content)]
        out = []
        for b in content or ():
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text":
                out.append((role, "text", None, str(b.get("text", ""))))
            elif t in ("tool_use", "server_tool_use"):
                out.append(("assistant", "tool_use", b.get("name"), _compact_json(b.get("input", {}))))
            elif t == "tool_result":
                text = _plain_text(b.get("content", ""), ("text",))
                out.append(("tool", "tool_result", names.get(b.get("tool_use_id")), _log_result_text(text)))
            elif t == "thinking":
                if b.get("thinking"):
                    out.append(("assistant", "thinking", None, str(b["thinking"])))
            elif t == "redacted_thinking":
                continue
            else:
                out.append((role, "text", None, f"[{t}]"))
        return out


class ChatAdapter:
    name = "chat"
    items_key = "messages"
    context_keys = ("tools", "functions")
    text_type = "text"

    def get_items(self, data):
        return data.get("messages")

    def is_pinned(self, item):
        return item.get("role") in ("system", "developer")

    def is_turn_start(self, item):
        return item.get("role") == "user"

    def is_first_user(self, item):
        return item.get("role") == "user"

    def call_ids(self, item):
        return {c.get("id") for c in item.get("tool_calls") or () if isinstance(c, dict)}

    def result_ids(self, item):
        return {item.get("tool_call_id")} if item.get("role") == "tool" else set()

    def is_reply(self, item):
        return item.get("role") == "assistant" and not item.get("tool_calls")

    def is_step_start(self, items, j):
        return items[j].get("role") == "assistant"

    def is_assistant(self, item):
        return item.get("role") == "assistant"

    def without_reasoning(self, item):
        """The assistant message without LiteLLM's signed thinking: the `thinking_blocks` field and thinking
        parts of list content. With `thinking_blocks`, `reasoning_content` is LiteLLM's text copy of the same
        thinking (never replayed to Anthropic) and goes too, as the OpenCode 2 port drops the whole reasoning part;
        `reasoning_content` alone is unsigned and stays. None when no content, tool call or function call is left."""
        content = _without_thinking_blocks(item.get("content"))
        if content is item.get("content") and "thinking_blocks" not in item:
            return item
        drop = ("thinking_blocks", "reasoning_content") if "thinking_blocks" in item else ()
        out = {k: v for k, v in item.items() if k not in drop}
        out["content"] = content
        if not (content or out.get("tool_calls") or out.get("function_call")):
            return None
        return out

    def tool_names(self, items):
        names = {}
        for item in items:
            for c in item.get("tool_calls") or ():
                if isinstance(c, dict):
                    names[c.get("id")] = (c.get("function") or {}).get("name")
        return names

    def system_text(self, data):
        msgs = data.get("messages") or []
        return "\n".join(_plain_text(m.get("content", ""), ("text",)) for m in msgs if self.is_pinned(m))

    def add_instruction(self, data, text):
        """Insert a system message after the leading system/developer messages."""
        msgs = data["messages"]
        at = next((i for i, m in enumerate(msgs) if not self.is_pinned(m)), len(msgs))
        data["messages"] = [*msgs[:at], {"role": "system", "content": text}, *msgs[at:]]

    def user_text(self, item):
        return _plain_text(item.get("content", ""), ("text",))

    def compress_item(self, item, over_tokens, on_blob):
        if item.get("role") != "tool":
            return item
        res = _compress_content(item.get("content"), over_tokens, "text")
        if res is None:
            return item
        on_blob(res[1], res[2])
        return {**item, "content": res[0]}

    def prepend_note(self, item, note):
        content = item.get("content")
        if isinstance(content, list):
            return {**item, "content": [{"type": "text", "text": note}, *content]}
        return {**item, "content": f"{note}\n\n{content or ''}"}

    def rows(self, item, names):
        role = item.get("role")
        out = []
        if role == "tool":
            text = _plain_text(item.get("content", ""), ("text",))
            name = item.get("name") or names.get(item.get("tool_call_id"))
            return [("tool", "tool_result", name, _log_result_text(text))]
        reasoning = item.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            out.append(("assistant", "thinking", None, reasoning))
        text = _plain_text(item.get("content") or "", ("text",))
        if text:
            out.append(("assistant" if role == "assistant" else "user", "text", None, text))
        for c in item.get("tool_calls") or ():
            fn = (c or {}).get("function") or {}
            out.append(("assistant", "tool_use", fn.get("name"), _compact_json(fn.get("arguments", "{}"))))
        return out


class ResponsesAdapter:
    name = "responses"
    items_key = "input"
    context_keys = ("instructions", "tools")
    text_type = "input_text"
    _TEXT_TYPES = ("input_text", "output_text", "text", "summary_text")

    def get_items(self, data):
        items = data.get("input")
        return items if isinstance(items, list) else None

    def _is_message(self, item):
        return item.get("type", "message") == "message" and "role" in item

    def is_pinned(self, item):
        return self._is_message(item) and item.get("role") in ("system", "developer")

    def is_turn_start(self, item):
        return self._is_message(item) and item.get("role") == "user"

    def is_first_user(self, item):
        return self.is_turn_start(item)

    def call_ids(self, item):
        return {item.get("call_id")} if item.get("type") in _CALL_TYPES else set()

    def result_ids(self, item):
        return {item.get("call_id")} if item.get("type") in _RESULT_TYPES else set()

    def is_reply(self, item):
        return self._is_message(item) and item.get("role") == "assistant"

    def is_step_start(self, items, j):
        """A reasoning item, or a call or agent message that follows neither reasoning nor another call, so a
        reasoning item stays with its calls and parallel calls stay together."""
        item = items[j]
        if item.get("type") == "reasoning":
            return True
        if not (item.get("type") in _CALL_TYPES or self.is_reply(item)):
            return False
        prev = items[j - 1] if j > 0 else {}
        return not (prev.get("type") == "reasoning" or prev.get("type") in _CALL_TYPES)

    def is_assistant(self, item):
        """An item the model produced: reasoning, a tool call or an assistant message."""
        return item.get("type") == "reasoning" or item.get("type") in _CALL_TYPES or self.is_reply(item)

    def without_reasoning(self, item):
        """None for a reasoning item that carries LiteLLM-encoded Anthropic thinking (signed against the prefix);
        every other item as is. OpenAI's own encrypted reasoning is not tied to the prefix and stays."""
        if item.get("type") == "reasoning" and _litellm_thinking(item.get("encrypted_content")):
            return None
        return item

    def tool_names(self, items):
        return {i.get("call_id"): i.get("name", "shell" if i.get("type") == "local_shell_call" else None)
                for i in items if i.get("type") in _CALL_TYPES}

    def system_text(self, data):
        parts = [str(data.get("instructions") or "")]
        for item in data.get("input") or ():
            if isinstance(item, dict) and self.is_pinned(item):
                parts.append(_plain_text(item.get("content", ""), self._TEXT_TYPES))
        return "\n".join(parts)

    def add_instruction(self, data, text):
        """Append to `instructions`, which precedes every input item and stays fixed for a session."""
        current = data.get("instructions")
        data["instructions"] = f"{current}\n\n{text}" if isinstance(current, str) and current else text

    def user_text(self, item):
        return _plain_text(item.get("content", ""), self._TEXT_TYPES)

    def compress_item(self, item, over_tokens, on_blob):
        if item.get("type") not in _RESULT_TYPES:
            return item
        res = _compress_content(item.get("output"), over_tokens, "input_text")
        if res is None:
            return item
        on_blob(res[1], res[2])
        return {**item, "output": res[0]}

    def prepend_note(self, item, note):
        content = item.get("content")
        if isinstance(content, list):
            return {**item, "content": [{"type": "input_text", "text": note}, *content]}
        return {**item, "content": f"{note}\n\n{content or ''}"}

    def rows(self, item, names):
        t = item.get("type", "message")
        if self._is_message(item):
            role = "assistant" if item.get("role") == "assistant" else "user"
            return [(role, "text", None, _plain_text(item.get("content", ""), self._TEXT_TYPES))]
        if t in _CALL_TYPES:
            arg = item.get("arguments", item.get("input", item.get("action", {})))
            return [("assistant", "tool_use", names.get(item.get("call_id")), _compact_json(arg))]
        if t in _RESULT_TYPES:
            text = _plain_text(item.get("output", ""), self._TEXT_TYPES)
            return [("tool", "tool_result", names.get(item.get("call_id")), _log_result_text(text))]
        if t == "reasoning":
            summary = _plain_text(item.get("summary") or [], self._TEXT_TYPES)
            return [("assistant", "thinking", None, summary)] if summary else []
        return [("assistant", "text", None, f"[{t}]")]


def adapter_for(call_type, route=None):
    """Adapter for a LiteLLM call type; raises UnsupportedCallType for any other call.

    A pass-through call is handled only on the Codex Responses route; every other pass-through body is opaque.
    """
    name = str(getattr(call_type, "value", call_type))
    if name == "pass_through_endpoint" and isinstance(route, str) and route.rstrip("/") == CODEX_RESPONSES_ROUTE:
        return ResponsesAdapter()
    match name:
        case "anthropic_messages":
            return AnthropicAdapter()
        case "acompletion" | "completion":
            return ChatAdapter()
        case "aresponses" | "responses":
            return ResponsesAdapter()
    raise UnsupportedCallType(name)
