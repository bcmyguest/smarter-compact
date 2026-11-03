"""Background embeddings for session databases, off the request path (docs/context-store.md, "Vectors").

The request path calls Embedder.notify(path, ...) after it stores new rows; one daemon thread embeds rows that have
no vector for the configured model through an OpenAI-compatible POST <url>/embeddings and stores them. Trimmed rows
go first, newest first. Batches adapt to the endpoint: they start at START_BATCH, double while a call takes under
GROW_FRACTION of its timeout, and halve on a failure down to 1. A row that fails alone MAX_FAILURES times is skipped
and recorded as a dim-0 vector. Errors are logged (type and location only) and an endpoint that keeps failing is
retried after RETRY_S. Stdlib only; cli/coding-agent/session-db.mjs embeds the same way.
"""

import json
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request

from .sessiondb import BUSY_TIMEOUT_MS, pack_vector
from .settings import DEFAULT_EMBEDDING_MAX_CHARS as DEFAULT_MAX_CHARS
from .settings import DEFAULT_EMBEDDING_TIMEOUT_S as DEFAULT_TIMEOUT_S

START_BATCH = 4
MAX_BATCH = 32
# A call finishing under this fraction of its timeout doubles the next batch.
GROW_FRACTION = 0.25
# Single-row failures before a row is skipped; also the consecutive single-row failures that end a run.
MAX_FAILURES = 3
RETRY_S = 60
SKIPPED_BLOB = b""


def _where(exc):
    tb = traceback.extract_tb(exc.__traceback__)
    return f"{tb[-1].filename.rsplit('/', 1)[-1]}:{tb[-1].lineno}" if tb else "?"


def _log(message):
    print(f"openmem-context: {message}", file=sys.stderr, flush=True)


def _log_error(exc):
    _log(f"embedding failed {type(exc).__name__} at {_where(exc)}")


def urllib_embed(url, model, api_key, texts, timeout=DEFAULT_TIMEOUT_S):
    """Vectors for texts from an OpenAI-compatible embeddings endpoint, in input order."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps({"model": model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(url.rstrip("/") + "/embeddings", data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    data = sorted(payload["data"], key=lambda d: d.get("index", 0))
    vectors = [[float(x) for x in d["embedding"]] for d in data]
    if len(vectors) != len(texts):
        raise ValueError("embedding count mismatch")
    return vectors


def pending_rows(conn, model, limit, exclude=()):
    """Rows without a `model` vector, trimmed (in_context = 0) first, then newest first; seqs in exclude are left out."""
    marks = ",".join("?" * len(exclude))
    skip = f" AND m.seq NOT IN ({marks})" if exclude else ""
    return conn.execute(
        "SELECT m.seq, m.text FROM messages m LEFT JOIN vectors v ON v.message_seq = m.seq AND v.model = ? "
        f"WHERE v.message_seq IS NULL AND length(trim(m.text)) > 0{skip} ORDER BY m.in_context, m.seq DESC LIMIT ?",
        (model, *sorted(exclude), limit)).fetchall()


def embed_pending(path, model, url, api_key="", embed=urllib_embed, *, state=None, timeout_s=DEFAULT_TIMEOUT_S,
                  max_chars=DEFAULT_MAX_CHARS):
    """Embed every row of the database at path that lacks a `model` vector; returns the number stored.

    state carries "batch" (the next batch size), "ok" (the endpoint's last call succeeded) and "failures"
    ({seq: single-row failures}) between runs. A failed size caps growth for the rest of the run.
    Re-raises the last error once MAX_FAILURES single-row calls fail in a row with nothing stored this run.
    """
    state = {} if state is None else state
    state.setdefault("batch", START_BATCH)
    state.setdefault("ok", False)
    failures = state.setdefault("failures", {})
    # mode=rw: never create a database that TTL cleanup removed meanwhile.
    uri = "file:" + urllib.parse.quote(str(path)) + "?mode=rw"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    stored = 0
    tried = set()
    streak = 0
    ceiling = MAX_BATCH
    try:
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        while True:
            batch = state["batch"]
            # Rows that never failed first; earlier failures get another try once those are done.
            rows = pending_rows(conn, model, batch, tried | set(failures)) or pending_rows(conn, model, batch, tried)
            if not rows:
                return stored
            timeout = timeout_s * len(rows)
            start = time.monotonic()
            try:
                vectors = embed(url, model, api_key, [text[:max_chars] for _, text in rows], timeout)
            except Exception as exc:
                seconds = time.monotonic() - start
                if len(rows) > 1:
                    ceiling = max(1, len(rows) // 2)
                    state["batch"] = max(1, len(rows) // 2)
                    _log(f"embedding batch failed {type(exc).__name__} rows={len(rows)} seconds={seconds:.1f} "
                         f"batch={state['batch']}")
                    continue
                seq = rows[0][0]
                tried.add(seq)
                failures[seq] = failures.get(seq, 0) + 1
                streak += 1
                _log(f"embedding row failed {type(exc).__name__} at {_where(exc)} seq={seq} failures={failures[seq]} "
                     f"seconds={seconds:.1f} batch=1")
                # Blame the row only while the endpoint works for other rows.
                if failures[seq] >= MAX_FAILURES and (stored or state["ok"]):
                    _insert(conn, [(seq, model, 0, SKIPPED_BLOB)])
                    del failures[seq]
                    _log(f"embedding skipped seq={seq} after {MAX_FAILURES} failures")
                # The endpoint looks down: the caller retries the database after RETRY_S.
                if streak >= MAX_FAILURES and not stored:
                    state["ok"] = False
                    raise
                continue
            seconds = time.monotonic() - start
            _insert(conn, [(seq, model, len(vec), pack_vector(vec)) for (seq, _), vec in zip(rows, vectors)])
            for seq, _ in rows:
                failures.pop(seq, None)
            stored += len(rows)
            streak = 0
            state["ok"] = True
            if len(rows) == batch and seconds < GROW_FRACTION * timeout:
                state["batch"] = min(ceiling, batch * 2)
            _log(f"embedded rows={len(rows)} seconds={seconds:.1f} batch={state['batch']}")
    finally:
        conn.close()


def _insert(conn, rows):
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany("INSERT OR IGNORE INTO vectors (message_seq, model, dim, blob) VALUES (?, ?, ?, ?)", rows)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


class Embedder:
    """One daemon thread; notify() queues a database path and never blocks on the network."""

    def __init__(self, embed=urllib_embed):
        self._embed = embed
        self._queue = {}
        self._retry_at = {}
        # {"batch", "ok"} per (url, model); single-row failure counts per (path, model).
        self._endpoint = {}
        self._failures = {}
        self._cond = threading.Condition()
        self._thread = None
        self._busy = False

    def notify(self, path, model, url, api_key="", timeout_s=DEFAULT_TIMEOUT_S, max_chars=DEFAULT_MAX_CHARS):
        if not model or not url:
            return
        with self._cond:
            if time.monotonic() < self._retry_at.get(str(path), 0):
                return
            self._queue[str(path)] = (model, url, api_key, timeout_s, max_chars)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="openmem-embed", daemon=True)
                self._thread.start()
            self._cond.notify()

    def batch_size(self, url, model):
        return self._endpoint.get((url, model), {}).get("batch", START_BATCH)

    def drain(self, timeout=10):
        """Wait until the queue is empty (tests)."""
        end = time.monotonic() + timeout
        with self._cond:
            while (self._queue or self._busy) and time.monotonic() < end:
                self._cond.wait(0.05)

    def _run(self):
        while True:
            with self._cond:
                while not self._queue:
                    self._busy = False
                    self._cond.notify_all()
                    self._cond.wait()
                path, job = next(iter(self._queue.items()))
                del self._queue[path]
                self._busy = True
            model, url, api_key, timeout_s, max_chars = job
            endpoint = self._endpoint.setdefault((url, model), {"batch": START_BATCH, "ok": False})
            state = {**endpoint, "failures": self._failures.setdefault((path, model), {})}
            try:
                embed_pending(path, model, url, api_key, embed=self._embed, state=state, timeout_s=timeout_s,
                              max_chars=max_chars)
            except Exception as exc:  # never let the thread die
                _log_error(exc)
                with self._cond:
                    self._retry_at[path] = time.monotonic() + RETRY_S
            finally:
                endpoint.update(batch=state["batch"], ok=state["ok"])


embedder = Embedder()
