"""TTL cleanup of session files (docs/context-store.md, "Retention"). Same rules as sweepContextStore in
cli/coding-agent/session-db.mjs."""

import json
import os
import sqlite3
import time
from pathlib import Path

from .compress import blob_id, clearable_strings

DAY_S = 86400
STAMP = ".ttl-stamp"
# Longest first, so `x.trim.json` groups under `x`, not `x.trim`.
SESSION_SUFFIXES = (".log.jsonl", ".trim.json", ".db-wal", ".db-shm", ".force", ".json", ".db")


def session_of(name):
    """(sid, suffix) for a session file name, or None for anything else."""
    if name.startswith("."):
        return None
    for suffix in SESSION_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)], suffix
    return None


def _sqlite_header(path):
    """True when path starts with the SQLite magic (avoids opening placeholder .db files that would rewrite -wal)."""
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def referenced_blob_ids(sessions_dir):
    """Distinct messages.blob_id values, plus blob_id(text) of each other tool_result row (the blob a cleared
    result's placeholder names) and of each clearable string of a tool_use row's input (a cleared input's blobs),
    across remaining session databases, or None if any DB is unreadable.

    A missing sessions directory means no references (empty set). Other OS errors fail closed (None).
    """
    ids = set()
    try:
        entries = list(os.scandir(sessions_dir))
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    for entry in entries:
        if not entry.name.endswith(".db") or not entry.is_file(follow_symlinks=False):
            continue
        if not _sqlite_header(entry.path):
            return None
        try:
            con = sqlite3.connect(f"file:{entry.path}?mode=ro", uri=True)
            try:
                for (bid,) in con.execute("SELECT DISTINCT blob_id FROM messages WHERE blob_id IS NOT NULL"):
                    if bid:
                        ids.add(bid)
                # A cleared result's placeholder names blob_id(text) of its stored row.
                for (text,) in con.execute("SELECT text FROM messages WHERE kind = 'tool_result' AND blob_id IS NULL"):
                    ids.add(blob_id(text))
                # A cleared tool-input string's placeholder names blob_id of that string (compress.input_placeholder).
                for (text,) in con.execute("SELECT text FROM messages WHERE kind = 'tool_use'"):
                    ids.update(blob_id(s) for s in clearable_strings(_input_value(text)))
            finally:
                con.close()
        except sqlite3.Error:
            return None
    return ids


def _input_value(text):
    """A stored tool_use row's input: its JSON, else the text itself (a freeform input)."""
    try:
        return json.loads(text)
    except ValueError:
        return text


def sweep_orphan_blobs(root):
    """Delete blobs/<id>.txt with no remaining messages.blob_id reference. Returns deleted ids, sorted.

    Skips the whole pass when any remaining session database cannot be opened (fail closed).
    """
    sessions = Path(root) / "sessions"
    blobs = Path(root) / "blobs"
    refs = referenced_blob_ids(sessions)
    if refs is None:
        return []
    deleted = []
    try:
        entries = list(os.scandir(blobs))
    except OSError:
        return []
    for entry in entries:
        if not entry.name.endswith(".txt") or not entry.is_file(follow_symlinks=False):
            continue
        bid = entry.name[:-4]
        if bid in refs:
            continue
        try:
            os.unlink(entry.path)
        except FileNotFoundError:
            continue
        deleted.append(bid)
    return sorted(deleted)


def sweep(root, ttl_days, now=None):
    """Delete every session whose newest file is older than ttl_days; returns the deleted sids, sorted.

    After session cleanup, also deletes orphaned blobs under blobs/ (shared across sessions).
    """
    if not ttl_days or ttl_days <= 0:
        return []
    now = time.time() if now is None else now
    sessions = Path(root) / "sessions"
    groups = {}
    try:
        entries = list(os.scandir(sessions))
    except FileNotFoundError:
        sweep_orphan_blobs(root)
        return []
    except OSError:
        return []
    for entry in entries:
        parsed = session_of(entry.name)
        if parsed is None or not entry.is_file(follow_symlinks=False):
            continue
        try:
            mtime = entry.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        group = groups.setdefault(parsed[0], [0.0, []])
        group[0] = max(group[0], mtime)
        group[1].append(entry.path)
    cutoff = now - ttl_days * DAY_S
    deleted = []
    for sid, (newest, paths) in groups.items():
        if newest >= cutoff:
            continue
        for path in paths:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        deleted.append(sid)
    sweep_orphan_blobs(root)
    return sorted(deleted)


def maybe_sweep(root, ttl_days, now=None):
    """sweep() at most once a day, recorded by the mtime of <root>/.ttl-stamp; returns the deleted sids."""
    if not ttl_days or ttl_days <= 0:
        return []
    now = time.time() if now is None else now
    stamp = Path(root) / STAMP
    try:
        if now - stamp.stat().st_mtime < DAY_S:
            return []
    except FileNotFoundError:
        pass
    except OSError:
        return []
    if not Path(root).is_dir():
        return []
    fd = os.open(stamp, os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    os.utime(stamp, (now, now))
    return sweep(root, ttl_days, now)
