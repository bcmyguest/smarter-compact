"""LiteLLM callback: `litellm_settings.callbacks: openmem_context_hook.handler`.

Thin wrapper; all logic lives in the dependency-free openmem_context package next to this file.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from litellm.integrations.custom_logger import CustomLogger  # noqa: E402

from openmem_context import process_request, process_usage, startup  # noqa: E402


class OpenMemContextHook(CustomLogger):
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


# Session-file TTL cleanup, once per proxy start (and at most once a day); never blocks or raises.
startup()

handler = OpenMemContextHook()
