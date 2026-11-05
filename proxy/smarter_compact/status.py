"""proxy-<port>.json in the context dir: the running proxy's package version and last request time.

The plugin reads it at session start to restart a proxy that runs older code than the installed package
(plugin/lib/proxy-daemon.mjs). Every write is best effort and never raises.
"""

import json
import os
import threading
import time
from pathlib import Path

# A request rewrites the file at most this often; the plugin's idle window is far longer.
TOUCH_EVERY_S = 5.0

_lock = threading.Lock()
_last_touch = 0.0
_started = None


def package_version(root=None):
    """The version in package.json next to proxy/ (the npm package or clone); None when absent."""
    path = Path(root) if root else Path(__file__).resolve().parents[2]
    try:
        return json.loads((path / "package.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None


def status_path(context_dir, port):
    return Path(context_dir) / f"proxy-{port}.json"


def write(context_dir, port=None, now=None, request=False):
    """Record this proxy's version, pid and start time; request=True also stamps last_request (throttled)."""
    global _last_touch, _started
    port = port if port is not None else str(os.environ.get("CONTEXT_PROXY_PORT", "")).strip()
    if not port:
        return False
    now = time.time() if now is None else now
    with _lock:
        if request and now - _last_touch < TOUCH_EVERY_S:
            return False
        if _started is None:
            _started = now
        body = {"version": package_version(), "pid": os.getpid(), "started": _started,
                "last_request": now if request else None}
        if request:
            _last_touch = now
        try:
            path = status_path(context_dir, port)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(body), encoding="utf-8")
            os.replace(tmp, path)
            return True
        except OSError:
            return False
