"""Context store files, per docs/context-store.md. Directories 700, files 600."""

import json
import os
import re
import threading
from pathlib import Path

DIR_MODE = 0o700
FILE_MODE = 0o600
TRIM_STATE_VERSION = 1
_SID_RE = re.compile(r"[^A-Za-z0-9._-]")
# ASCII whitespace only, so the hooks (JS) and the proxy trim the same characters.
ASCII_WHITESPACE = " \t\n\v\f\r"
_locks = {}
_locks_guard = threading.Lock()


class InvalidSessionId(ValueError):
    """The session id is empty after trimming."""


class RegistrationNotFound(LookupError):
    """No readable registration for the session."""


def sanitize_sid(sid):
    """Contract sanitizer (docs/context-store.md): trim, replace characters outside [A-Za-z0-9._-], reject empty."""
    clean = _SID_RE.sub("_", str(sid).strip(ASCII_WHITESPACE))
    if not clean:
        raise InvalidSessionId("empty session id")
    return clean


def session_lock(sid):
    """In-process lock per session. The proxy runs one LiteLLM worker."""
    with _locks_guard:
        lock = _locks.get(sid)
        if lock is None:
            lock = _locks[sid] = threading.Lock()
        return lock


def _ensure_dir(path):
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    try:
        os.chmod(path, DIR_MODE)
    except OSError:
        pass


def _write_atomic(path, data):
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.sessions = self.root / "sessions"
        self.blobs = self.root / "blobs"

    def _session_path(self, sid, suffix):
        return self.sessions / f"{sid}{suffix}"

    # Registration (written by the SessionStart hook)
    def registration(self, sid):
        """Registration dict; raises RegistrationNotFound when it is missing, unreadable or not an object."""
        try:
            with open(self._session_path(sid, ".json"), "rb") as fh:
                value = json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError) as exc:
            raise RegistrationNotFound(sid) from exc
        if not isinstance(value, dict):
            raise RegistrationNotFound(sid)
        return value

    def db_path(self, sid):
        """Session database (sessiondb.py)."""
        return self._session_path(sid, ".db")

    def ensure_sessions(self):
        _ensure_dir(self.sessions)

    # Force flag (written by the prompt hook)
    def force_requested(self, sid):
        return self._session_path(sid, ".force").exists()

    def clear_force(self, sid):
        try:
            os.unlink(self._session_path(sid, ".force"))
        except FileNotFoundError:
            pass

    # Proxy-private trim state
    def load_trim(self, sid):
        try:
            with open(self._session_path(sid, ".trim.json"), "rb") as fh:
                value = json.loads(fh.read().decode("utf-8"))
        except (OSError, ValueError):
            value = None
        if not isinstance(value, dict) or value.get("version") != TRIM_STATE_VERSION:
            return {"version": TRIM_STATE_VERSION, "threads": {}}
        if not isinstance(value.get("threads"), dict):
            value["threads"] = {}
        return value

    def save_trim(self, sid, state):
        _ensure_dir(self.sessions)
        data = json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8")
        _write_atomic(self._session_path(sid, ".trim.json"), data)

    # Blobs
    def write_blob(self, bid, text):
        path = self.blobs / f"{bid}.txt"
        if path.exists():
            return
        _ensure_dir(self.blobs)
        _write_atomic(path, text.encode("utf-8"))
