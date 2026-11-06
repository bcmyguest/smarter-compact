"""LiteLLM callback: `litellm_settings.callbacks: smarter_compact_hook.handler`.

Thin wrapper; all logic lives in the dependency-free smarter_compact package next to this file.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from litellm.integrations.custom_logger import CustomLogger  # noqa: E402

from smarter_compact import Settings, process_request, process_usage, startup  # noqa: E402
from smarter_compact.core import STREAM_TAIL_BYTES, note_stream_edits  # noqa: E402


class SmarterCompactHook(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        # File and Hindsight I/O run off the event loop; process_request never raises. A pass-through call (the
        # Codex subscription route) is told apart by its route, and its call id lives on the logging object.
        route = getattr(user_api_key_dict, "request_route", None)
        call_id = getattr(data.get("litellm_logging_obj"), "litellm_call_id", None) if isinstance(data, dict) else None
        return await asyncio.to_thread(process_request, data, call_type, route=route, call_id=call_id)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        # Streams arrive here once, with the assembled response. kwargs carries the litellm_call_id that
        # async_pre_call_hook saw in data; process_usage never raises.
        await asyncio.to_thread(process_usage, kwargs, response_obj)


async def _streaming_iterator_hook(self, user_api_key_dict, response, request_data):
    # Passes every chunk through untouched; keeps the stream's tail for the applied context edits in message_delta.
    tail = bytearray()
    async for chunk in response:
        if isinstance(chunk, (bytes, bytearray, str)):
            tail += chunk.encode("utf-8") if isinstance(chunk, str) else chunk
            if len(tail) > 2 * STREAM_TAIL_BYTES:
                del tail[:-STREAM_TAIL_BYTES]
        yield chunk
    call_id = request_data.get("litellm_call_id") or getattr(request_data.get("litellm_logging_obj"), "litellm_call_id", None) \
        if isinstance(request_data, dict) else None
    await asyncio.to_thread(note_stream_edits, call_id, bytes(tail))


# Only with server-side clearing on: any override takes LiteLLM off its pass-through fast path for every stream.
_settings = Settings.from_env()
if _settings.enabled and _settings.server_clear:
    SmarterCompactHook.async_post_call_streaming_iterator_hook = _streaming_iterator_hook

# Session-file TTL cleanup, once per proxy start (and at most once a day); never blocks or raises.
startup()

handler = SmarterCompactHook()
