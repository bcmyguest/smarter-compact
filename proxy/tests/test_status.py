"""proxy-<port>.json status file. Run: python3 -m unittest discover -s proxy/tests"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smarter_compact import status  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        status._last_touch = 0.0
        status._started = None

    def read(self):
        return json.loads(status.status_path(self.dir, "4101").read_text())

    def test_reports_package_version(self):
        self.assertEqual(status.package_version(), json.loads((ROOT / "package.json").read_text())["version"])
        self.assertIsNone(status.package_version(self.dir))

    def test_start_then_throttled_requests(self):
        self.assertTrue(status.write(self.dir, "4101", now=100.0))
        self.assertEqual((self.read()["started"], self.read()["last_request"]), (100.0, None))
        self.assertTrue(status.write(self.dir, "4101", now=101.0, request=True))
        self.assertFalse(status.write(self.dir, "4101", now=103.0, request=True))
        self.assertTrue(status.write(self.dir, "4101", now=107.0, request=True))
        self.assertEqual((self.read()["started"], self.read()["last_request"]), (100.0, 107.0))

    def test_no_port_writes_nothing(self):
        self.assertFalse(status.write(self.dir, ""))
        self.assertEqual(list(self.dir.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
