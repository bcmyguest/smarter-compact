"""CONTEXT_DEBUG_SHAPES: one stderr line per request with counts only, never message content."""
import contextlib
import io
import unittest

from smarter_compact import core
from smarter_compact.adapters import AnthropicAdapter


class LogShapeTest(unittest.TestCase):
    def test_counts_without_content(self):
        data = {
            "system": [{"type": "text", "text": "secret system"}],
            "tools": [{"name": "Bash"}, {"name": "Read"}],
            "messages": [
                {"role": "user", "content": "secret question"},
                {"role": "assistant", "content": "secret answer"},
                {"role": "user", "content": "follow-up"},
            ],
        }
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            core._log_shape("sid1", "anthropic_messages", data, data["messages"], AnthropicAdapter())
        line = err.getvalue()
        self.assertIn("items=3 pinned=0", line)
        self.assertIn("tools=2", line)
        self.assertIn("tail_roles=uau", line)
        self.assertNotIn("secret", line)


if __name__ == "__main__":
    unittest.main()
