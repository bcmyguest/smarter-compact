"""Per-session SQLite store `sessions/<sid>.db` (docs/context-store.md, "Session database"). Stdlib only.

The proxy writes every request's messages here, idempotently; the MCP tools read it. The schema and the row
rules must match cli/coding-agent/session-db.mjs byte for byte (scripts/tests/test_context_parity.mjs).
"""

import math
import os
import re
import sqlite3
import struct
import threading
from collections import OrderedDict
from pathlib import Path

from .tokens import fingerprint

SCHEMA_VERSION = 3
# Version 1, executed in order on an empty database. Same strings as SCHEMA in cli/coding-agent/session-db.mjs.
SCHEMA = (
    "CREATE TABLE messages (seq INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, thread TEXT NOT NULL, "
    "role TEXT NOT NULL, kind TEXT NOT NULL, name TEXT, text TEXT NOT NULL, blob_id TEXT, "
    "in_context INTEGER NOT NULL DEFAULT 1, trimmed_at_seq INTEGER, trimmed_at TEXT, created_at TEXT NOT NULL)",
    "CREATE INDEX messages_thread ON messages (thread, seq)",
    "CREATE TABLE lines (id INTEGER PRIMARY KEY, message_seq INTEGER NOT NULL REFERENCES messages (seq), "
    "line_no INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, UNIQUE (message_seq, line_no))",
    "CREATE VIRTUAL TABLE messages_fts USING fts5 (text, content='messages', content_rowid='seq')",
    "CREATE VIRTUAL TABLE lines_fts USING fts5 (text, content='lines', content_rowid='id')",
    "CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN "
    "INSERT INTO messages_fts (rowid, text) VALUES (new.seq, new.text); END",
    "CREATE TRIGGER lines_fts_insert AFTER INSERT ON lines BEGIN "
    "INSERT INTO lines_fts (rowid, text) VALUES (new.id, new.text); END",
    "CREATE TABLE vectors (message_seq INTEGER NOT NULL REFERENCES messages (seq), model TEXT NOT NULL, "
    "dim INTEGER NOT NULL, blob BLOB NOT NULL, PRIMARY KEY (message_seq, model))",
)
# (version, DDL) run in order after SCHEMA on a new database, or from the stored version up on an older one; then
# PRAGMA user_version = SCHEMA_VERSION. Same strings as MIGRATIONS in cli/coding-agent/session-db.mjs.
MIGRATIONS = (
    # Trims and fail-opens: kind 'trim' | 'fail_open'; version names the writer ("proxy:<hash>", "opencode:<v>").
    (2, ("CREATE TABLE events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL, "
         "location TEXT, error TEXT, tokens_before INTEGER, tokens_after INTEGER, dropped INTEGER)",)),
    # Per-request usage: kind 'usage', with provider-reported token counts, prompt-cache reads and writes, whether
    # the request carried a cut, and its cache_control breakpoints (Anthropic route).
    (3, tuple(f"ALTER TABLE events ADD COLUMN {c}" for c in (
        "model TEXT", "thread_root TEXT", "trimmed INTEGER", "input_tokens INTEGER", "cache_read_tokens INTEGER",
        "cache_creation_tokens INTEGER", "output_tokens INTEGER", "breakpoints INTEGER", "breakpoint_at TEXT"))),
)
EVENT_COLUMNS = ("at", "kind", "version", "location", "error", "tokens_before", "tokens_after", "dropped", "model",
                 "thread_root", "trimmed", "input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens",
                 "breakpoints", "breakpoint_at")
_INSERT_EVENT = f"INSERT INTO events ({', '.join(EVENT_COLUMNS)}) VALUES ({', '.join('?' * len(EVENT_COLUMNS))})"
DB_SUFFIX = ".db"
FILE_MODE = 0o600
DIR_MODE = 0o700
BUSY_TIMEOUT_MS = 1000
MAX_OPEN = 16
_BLOB_REF_RE = re.compile(r"^\[blob:([0-9a-f]{16})\]")
_open = OrderedDict()
_open_guard = threading.Lock()


class UnsupportedSchema(RuntimeError):
    """The database has a user_version this code does not know."""


def blob_ref(text):
    """Blob id named by a stored tool-result text ("[blob:<id>]..."), else None."""
    m = _BLOB_REF_RE.match(text) if isinstance(text, str) else None
    return m.group(1) if m else None


def chain(rows):
    """Give each row its fingerprint and thread, in order, in place; returns rows.

    A row's fingerprint hashes the previous row's fingerprint with the row's own role, kind, name and text, so it
    names the row and its position in the conversation: a resent history yields the same keys, and the same text
    said twice yields two rows. thread is the fingerprint of the conversation's first row.
    """
    prev = ""
    for row in rows:
        prev = row["fingerprint"] = fingerprint(
            {"prev": prev, "role": row["role"], "kind": row["kind"], "name": row["name"], "text": row["text"]})
        row["thread"] = rows[0]["fingerprint"]
    return rows


def _ensure_file(path):
    path.parent.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, DIR_MODE)
    except OSError:
        pass
    if not path.exists():
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT, FILE_MODE))


def connect(path, create=True):
    """Writer connection in WAL mode with the schema in place; raises UnsupportedSchema on a newer database."""
    path = Path(path)
    if create:
        _ensure_file(path)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version < SCHEMA_VERSION:
                    if version == 0:
                        for ddl in SCHEMA:
                            conn.execute(ddl)
                    for target, ddls in MIGRATIONS:
                        if target > max(version, 1):
                            for ddl in ddls:
                                conn.execute(ddl)
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise UnsupportedSchema(f"user_version {version}")
    except BaseException:
        conn.close()
        raise
    return conn


def _identity(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return st.st_dev, st.st_ino


class SessionDB:
    """One session's database plus an in-memory index of its rows (fingerprint -> [seq, in_context])."""

    def __init__(self, path):
        self.path = Path(path)
        self.conn = connect(self.path)
        self.identity = _identity(self.path)
        self.known = {}
        self.data_version = None

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    def _refresh(self):
        """Reload the index when another connection committed since the last look."""
        version = self.conn.execute("PRAGMA data_version").fetchone()[0]
        if version != self.data_version:
            self.known = {fp: [seq, flag] for fp, seq, flag in
                          self.conn.execute("SELECT fingerprint, seq, in_context FROM messages")}
            self.data_version = version

    def sync(self, rows, cut_row, ts):
        """Store chained rows and their in_context flags; returns the number of new rows.

        rows: dicts with fingerprint, thread, role, kind, name, text, blob_id and keep (in context after this
        request). cut_row: index into rows of the first row of the cut turn, or None. Rows already stored are left
        alone except for in_context: a row that leaves context records trimmed_at_seq (the cut row's seq, NULL
        without one) and trimmed_at; a row that returns clears both.
        """
        self._refresh()
        known = self.known
        fresh = [r for r in rows if r["fingerprint"] not in known]
        changes = [r for r in rows if r["fingerprint"] in known and known[r["fingerprint"]][1] != int(r["keep"])]
        if not fresh and not changes:
            return 0
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            added = 0
            for r in fresh:
                cur = conn.execute(
                    "INSERT INTO messages (fingerprint, thread, role, kind, name, text, blob_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (fingerprint) DO NOTHING",
                    (r["fingerprint"], r["thread"], r["role"], r["kind"], r["name"], r["text"], r["blob_id"], ts))
                if cur.rowcount == 1:
                    seq = cur.lastrowid
                    conn.executemany("INSERT INTO lines (message_seq, line_no, role, text) VALUES (?, ?, ?, ?)",
                                     [(seq, n, r["role"], line) for n, line in enumerate(r["text"].split("\n"), 1)])
                    known[r["fingerprint"]] = [seq, 1]
                    added += 1
                else:
                    seq, flag = conn.execute("SELECT seq, in_context FROM messages WHERE fingerprint = ?",
                                             (r["fingerprint"],)).fetchone()
                    known[r["fingerprint"]] = [seq, flag]
            cut_seq = known[rows[cut_row]["fingerprint"]][0] if cut_row is not None else None
            for r in rows:
                entry = known[r["fingerprint"]]
                want = int(r["keep"])
                if entry[1] == want:
                    continue
                if want:
                    conn.execute("UPDATE messages SET in_context = 1, trimmed_at_seq = NULL, trimmed_at = NULL "
                                 "WHERE seq = ?", (entry[0],))
                else:
                    conn.execute("UPDATE messages SET in_context = 0, trimmed_at_seq = ?, trimmed_at = ? "
                                 "WHERE seq = ?", (cut_seq, ts, entry[0]))
                entry[1] = want
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            self.data_version = None
            raise
        self.data_version = conn.execute("PRAGMA data_version").fetchone()[0]
        return added


    def add_event(self, kind, at, version, **fields):
        """Append one events row; fields are the other EVENT_COLUMNS (location, error, tokens_before, ...)."""
        values = {"at": at, "kind": kind, "version": version, **fields}
        self.conn.execute(_INSERT_EVENT, tuple(values.get(c) for c in EVENT_COLUMNS))


def open_session(path):
    """Cached SessionDB for path; reopened when the file was deleted or replaced (TTL cleanup)."""
    path = Path(path)
    with _open_guard:
        db = _open.pop(str(path), None)
        if db is not None and db.identity != _identity(path):
            db.close()
            db = None
        if db is None:
            db = SessionDB(path)
        _open[str(path)] = db
        while len(_open) > MAX_OPEN:
            _open.popitem(last=False)[1].close()
        return db


def drop_session(path):
    """Forget a cached connection, after an error, so the next request reopens it."""
    with _open_guard:
        db = _open.pop(str(Path(path)), None)
    if db is not None:
        db.close()


# Vectors: float32 little-endian, brute-force cosine (sessions hold thousands of rows).
def pack_vector(values):
    return struct.pack(f"<{len(values)}f", *values)


def unpack_vector(blob, dim):
    return struct.unpack(f"<{dim}f", blob)


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def nearest(conn, query, model, k):
    """(seq, similarity) of the k stored vectors of `model` closest to query, best first; ties by seq."""
    scored = []
    for seq, dim, blob in conn.execute("SELECT message_seq, dim, blob FROM vectors WHERE model = ?", (model,)):
        if dim == len(query):
            scored.append((seq, cosine(query, unpack_vector(blob, dim))))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:k]
