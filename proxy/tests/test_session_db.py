"""Session database (sessions/<sid>.db), TTL cleanup and background embeddings. Run: python3 -m unittest discover -s proxy/tests"""

import copy
import io
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smarter_compact import Settings, process_request  # noqa: E402
from smarter_compact import embed as embed_mod  # noqa: E402
from smarter_compact import sessiondb, ttl  # noqa: E402
from smarter_compact import core  # noqa: E402
from smarter_compact.core import startup  # noqa: E402

SID = "db-1"
BIG = 10_000_000


def req(msgs, sid=SID):
    return {"model": "claude", "system": [{"type": "text", "text": "SYSTEM"}],
            "messages": copy.deepcopy(msgs), "proxy_server_request": {"headers": {"x-claude-code-session-id": sid}}}


def history(turns, pad=0):
    # `pad` widens each turn's payload: the contract note is ~250 tokens, so a test that means
    # "the dropped rows outweigh the note" needs dropped rows with real content in them.
    fill = " padding" * pad
    msgs = []
    for t in range(turns):
        msgs.append({"role": "user", "content": [{"type": "text", "text": f"question {t}\nsecond line {t}{fill}"}]})
        msgs.append({"role": "assistant", "content": [
            {"type": "tool_use", "id": f"tu{t}", "name": "Bash", "input": {"command": f"echo {t}"}}]})
        msgs.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"tu{t}", "content": f"out {t}"}]})
        msgs.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
    return msgs


class DbBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Path(self.tmp.name) / "context"
        self.addCleanup(lambda: [sessiondb.drop_session(p) for p in list(sessiondb._open)])

    def run_req(self, msgs, sid=SID, **kw):
        base = dict(trim_at_tokens=BIG, compress_over_tokens=0, context_dir=self.ctx, standing_instruction=False)
        base.update(kw)
        return process_request(req(msgs, sid), "anthropic_messages", Settings(**base))

    def path(self, sid=SID):
        return self.ctx / "sessions" / f"{sid}.db"

    def query(self, sql, params=(), sid=SID):
        conn = sqlite3.connect(self.path(sid))
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()


class StoreEveryMessageTests(DbBase):
    def test_every_message_stored_once_across_resends(self):
        msgs = history(2)
        self.run_req(msgs)
        self.assertEqual(self.query("SELECT count(*) FROM messages")[0][0], 8)
        self.assertEqual(self.query("SELECT DISTINCT in_context FROM messages"), [(1,)])
        # The same history again, with Claude Code's moving cache_control marker and string/block change.
        again = copy.deepcopy(msgs)
        again[-1]["content"][0]["cache_control"] = {"type": "ephemeral"}
        again[0]["content"] = again[0]["content"][0]["text"]
        self.run_req(again)
        self.assertEqual(self.query("SELECT count(*) FROM messages")[0][0], 8)
        msgs.append({"role": "user", "content": "next"})
        self.run_req(msgs)
        self.assertEqual(self.query("SELECT count(*) FROM messages")[0][0], 9)
        self.assertEqual(self.query("SELECT text FROM messages ORDER BY seq DESC LIMIT 1"), [("next",)])

    def test_same_text_twice_is_two_rows_one_thread(self):
        self.run_req(history(2))
        self.assertEqual(self.query("SELECT count(*) FROM messages WHERE text = 'ok'")[0][0], 2)
        self.assertEqual(self.query("SELECT count(DISTINCT thread) FROM messages")[0][0], 1)
        self.run_req([{"role": "user", "content": "subagent task"}])
        self.assertEqual(self.query("SELECT count(DISTINCT thread) FROM messages")[0][0], 2)

    def test_edited_old_message_reinserts_only_that_message(self):
        msgs = history(3)
        self.run_req(msgs)
        self.assertEqual(self.query("SELECT count(*) FROM messages")[0][0], 12)
        edited = copy.deepcopy(msgs)
        edited[4]["content"][0]["text"] = "question 1 reworded"
        self.run_req(edited)
        self.assertEqual(self.query("SELECT text FROM messages WHERE seq > 12"), [("question 1 reworded",)])
        # A cold index, rebuilt from the stored rows, keys the same way.
        sessiondb.drop_session(self.path())
        edited[8]["content"][0]["text"] = "question 2 reworded"
        self.run_req(edited)
        self.assertEqual(self.query("SELECT text FROM messages WHERE seq > 13"), [("question 2 reworded",)])
        self.assertEqual(self.query("SELECT count(*) FROM messages WHERE text = 'out 2'")[0][0], 1)

    def test_repeated_identical_messages_are_both_stored(self):
        msgs = history(2)
        for m in (msgs[2], msgs[6]):
            m["content"][0]["content"] = "same"
        self.run_req(msgs)
        self.assertEqual(self.query("SELECT count(*) FROM messages WHERE text = 'same'")[0][0], 2)
        edited = copy.deepcopy(msgs)
        edited[4]["content"][0]["text"] = "question 1 reworded"
        edited.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu1", "content": "same"}]})
        self.run_req(edited)
        self.assertEqual(self.query("SELECT count(*) FROM messages WHERE text = 'same'")[0][0], 3)
        self.assertEqual(self.query("SELECT count(*) FROM messages WHERE text = 'ok'")[0][0], 2)

    def test_lines_fts_schema_and_wal(self):
        self.run_req(history(1))
        self.assertEqual(self.query("PRAGMA user_version")[0][0], sessiondb.SCHEMA_VERSION)
        self.assertEqual(self.query("PRAGMA journal_mode")[0][0], "wal")
        self.assertEqual(self.query("SELECT line_no, text FROM lines WHERE message_seq = 1 ORDER BY line_no"),
                         [(1, "question 0"), (2, "second line 0")])
        self.assertEqual(self.query("SELECT count(*) FROM lines_fts WHERE lines_fts MATCH 'second'")[0][0], 1)
        self.assertEqual(self.query("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'echo'")[0][0], 2)
        self.assertEqual(stat.S_IMODE(os.stat(self.path()).st_mode), 0o600)
        shadow = ("_data", "_idx", "_docsize", "_config")
        names = [r[0] for r in self.query("SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger') ORDER BY name")
                 if not r[0].endswith(shadow)]
        self.assertEqual(names, ["events", "lines", "lines_fts", "lines_fts_insert", "messages", "messages_fts",
                                 "messages_fts_insert", "vectors"])

    def test_trim_marks_rows_out_of_context_with_cut_seq(self):
        msgs = history(3)
        self.run_req(msgs, trim_at_tokens=1)
        rows = self.query("SELECT seq, text, in_context, trimmed_at_seq FROM messages ORDER BY seq")
        cut_seq = next(seq for seq, text, _, _ in rows if text.startswith("question 2"))
        out = [r for r in rows if r[2] == 0]
        # Everything before the cut leaves context except the pinned first user turn (seq 1) and the reply
        # just before the cut turn.
        self.assertEqual([r[0] for r in out], list(range(2, cut_seq - 1)))
        self.assertTrue(all(r[3] == cut_seq for r in out))
        self.assertEqual(rows[0][2], 1)
        self.assertEqual(rows[cut_seq - 2][1:3], ("ok", 1))
        self.assertTrue(all(r[2] == 1 and r[3] is None for r in rows if r[0] >= cut_seq))

    def test_cut_reset_brings_rows_back(self):
        msgs = history(3)
        self.run_req(msgs, trim_at_tokens=1)
        self.assertGreater(self.query("SELECT count(*) FROM messages WHERE in_context = 0")[0][0], 0)
        # The client rewrote the history: the stored cut message is gone, so the thread resets and nothing is cut.
        changed = copy.deepcopy(msgs)
        changed[8]["content"][0]["text"] = "edited"
        self.run_req(changed[:8] + [changed[8]])
        old = self.query("SELECT in_context, trimmed_at_seq, trimmed_at FROM messages WHERE seq < 9")
        self.assertTrue(all(r == (1, None, None) for r in old), old)

    def test_compressed_result_stored_in_full(self):
        big = "\n".join(str(i) for i in range(20000))
        msgs = history(1)
        msgs[2]["content"][0]["content"] = big
        out = self.run_req(msgs, compress_over_tokens=100)
        self.assertTrue(out["messages"][2]["content"][0]["content"].startswith("[smarter-compact: tool result compressed"))
        text, blob = self.query("SELECT text, blob_id FROM messages WHERE kind = 'tool_result'")[0]
        self.assertEqual(text, big)
        self.assertRegex(blob, r"^[0-9a-f]{16}$")
        self.assertEqual(self.query("SELECT count(*) FROM lines WHERE message_seq = 3")[0][0], 20000)

    def test_external_writer_refreshes_cache(self):
        self.run_req(history(1))
        conn = sqlite3.connect(self.path())
        conn.execute("UPDATE messages SET in_context = 0 WHERE seq = 2")
        conn.commit()
        conn.close()
        self.run_req(history(1))
        self.assertEqual(self.query("SELECT in_context FROM messages WHERE seq = 2"), [(1,)])

    def test_deleted_database_is_recreated(self):
        self.run_req(history(1))
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(f"{self.path()}{suffix}")
            except FileNotFoundError:
                pass
        self.run_req(history(1))
        self.assertEqual(self.query("SELECT count(*) FROM messages")[0][0], 4)

    def test_event_from_another_thread_waits_for_a_running_sync(self):
        # The proxy shares one SessionDB across threads; a usage event written mid-sync took sync's last_insert_rowid
        # and its lines collided (IntegrityError at the lines insert, probe trial adaptive-rejection-sampler__d4PUepu).
        self.path().parent.mkdir(parents=True)
        db = sessiondb.open_session(self.path())
        real, waited = db.conn, []

        class Conn:
            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, sql, *args):
                cur = real.execute(sql, *args)
                if sql.startswith("INSERT INTO messages") and not waited:
                    t = threading.Thread(target=db.add_event, args=("usage", "t", "v"))
                    t.start()
                    t.join(0.3)
                    waited.append(t)
                    waited.append(t.is_alive())
                return cur

        db.conn = Conn()
        rows = sessiondb.chain([{"role": "user", "kind": "text", "name": None, "text": "a\nb", "blob_id": None,
                                 "keep": True}])
        self.assertEqual(db.sync(rows, None, "t"), 1)
        waited[0].join(5)
        self.assertTrue(waited[1], "add_event ran inside another thread's sync transaction")
        self.assertEqual(self.query("SELECT count(*) FROM events")[0][0], 1)
        self.assertEqual(self.query("SELECT message_seq, line_no FROM lines"), [(1, 1), (1, 2)])


class EventTests(DbBase):
    def test_trim_records_tokens_before_and_after(self):
        # Six turns with real payload, so the dropped rows outweigh the pointer note that replaces them.
        self.run_req(history(6, pad=600), trim_at_tokens=1)
        rows = self.query("SELECT kind, version, tokens_before, tokens_after, dropped, error FROM events")
        self.assertEqual(len(rows), 1)
        kind, version, before, after, dropped, error = rows[0]
        self.assertEqual((kind, version, error), ("trim", core.VERSION, None))
        self.assertGreater(before, after)
        self.assertEqual(dropped, 18)

    def test_request_without_trim_records_nothing(self):
        self.run_req(history(3))
        self.assertEqual(self.query("SELECT count(*) FROM events"), [(0,)])

    def test_fail_open_is_recorded_without_content(self):
        def broken(*args, **kwargs):
            raise RuntimeError("question 0 must not leak")

        with mock.patch.object(core, "choose_cut", broken), redirect_stderr(io.StringIO()):
            out = self.run_req(history(3), trim_at_tokens=1)
        self.assertEqual(len(out["messages"]), 12)
        rows = self.query("SELECT kind, version, error, location FROM events")
        self.assertEqual([r[:3] for r in rows], [("fail_open", core.VERSION, "RuntimeError")])
        self.assertRegex(rows[0][3], r"^test_session_db\.py:\d+$")
        self.assertEqual(self.query("SELECT count(*) FROM events WHERE location LIKE '%question%'"), [(0,)])

    def test_code_hash_names_the_package_sources(self):
        self.assertRegex(core.VERSION, r"^proxy:[0-9a-f]{12}$")
        self.assertEqual(core.VERSION, f"proxy:{core.code_hash(Path(core.__file__).parent)}")


class FailOpenTests(DbBase):
    def test_database_error_logs_and_request_still_trims(self):
        (self.ctx / "sessions" / f"{SID}.db").mkdir(parents=True)
        err = io.StringIO()
        with redirect_stderr(err):
            out = self.run_req(history(3), trim_at_tokens=1)
        self.assertTrue(out["messages"][2]["content"][0]["text"].startswith("[smarter-compact: continuing session"))
        self.assertEqual(err.getvalue().count("smarter-compact: fail-open"), 1)
        self.assertNotIn("question", err.getvalue())

    def test_newer_schema_is_left_alone(self):
        (self.ctx / "sessions").mkdir(parents=True)
        conn = sqlite3.connect(self.path())
        conn.execute("PRAGMA user_version = 99")
        conn.close()
        err = io.StringIO()
        with redirect_stderr(err):
            out = self.run_req(history(1))
        self.assertEqual(len(out["messages"]), 4)
        self.assertIn("UnsupportedSchema", err.getvalue())
        self.assertEqual(self.query("PRAGMA user_version")[0][0], 99)

    def test_version_1_database_migrates_in_place(self):
        (self.ctx / "sessions").mkdir(parents=True)
        conn = sqlite3.connect(self.path())
        for ddl in sessiondb.SCHEMA:
            conn.execute(ddl)
        conn.execute("INSERT INTO messages (fingerprint, thread, role, kind, text, created_at) "
                     "VALUES ('old', 'old', 'user', 'text', 'kept', 't')")
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        conn.close()
        self.run_req(history(1))
        self.assertEqual(self.query("PRAGMA user_version")[0][0], sessiondb.SCHEMA_VERSION)
        self.assertEqual(self.query("SELECT text FROM messages WHERE fingerprint = 'old'"), [("kept",)])
        self.assertEqual(self.query("SELECT count(*) FROM events"), [(0,)])

    def test_version_2_database_gains_usage_columns_and_keeps_events(self):
        (self.ctx / "sessions").mkdir(parents=True)
        conn = sqlite3.connect(self.path())
        for ddl in sessiondb.SCHEMA + next(s for v, s in sessiondb.MIGRATIONS if v == 2):
            conn.execute(ddl)
        conn.execute("INSERT INTO events (at, kind, version, dropped) VALUES ('t', 'trim', 'proxy:x', 3)")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()
        conn.close()
        self.run_req(history(1))
        self.assertEqual(self.query("PRAGMA user_version")[0][0], sessiondb.SCHEMA_VERSION)
        self.assertEqual(self.query("SELECT kind, dropped, cache_read_tokens, breakpoints FROM events"),
                         [("trim", 3, None, None)])


class TtlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "context"
        (self.root / "sessions").mkdir(parents=True)

    def touch(self, name, age_days):
        path = self.root / "sessions" / name
        path.write_text("x")
        t = time.time() - age_days * 86400
        os.utime(path, (t, t))
        return path

    def test_sweep_groups_by_session_and_keeps_recent(self):
        for name in ("old.db", "old.db-wal", "old.db-shm", "old.json", "old.trim.json", "old.log.jsonl", "old.force"):
            self.touch(name, 40)
        self.touch("mixed.db", 40)
        self.touch("mixed.db-wal", 1)
        self.touch("new.json", 2)
        self.touch(".hidden.tmp", 90)
        self.touch("notes.txt", 90)
        self.assertEqual(ttl.sweep(self.root, 30), ["old"])
        left = sorted(p.name for p in (self.root / "sessions").iterdir())
        self.assertEqual(left, [".hidden.tmp", "mixed.db", "mixed.db-wal", "new.json", "notes.txt"])

    def test_sweep_deletes_orphan_blobs(self):
        import sqlite3

        sessions = self.root / "sessions"
        blobs = self.root / "blobs"
        blobs.mkdir()

        def write_db(sid, blob_id):
            path = sessions / f"{sid}.db"
            con = sqlite3.connect(path)
            con.executescript(
                "CREATE TABLE messages (seq INTEGER PRIMARY KEY, fingerprint TEXT, thread TEXT, "
                "role TEXT, kind TEXT, name TEXT, text TEXT, blob_id TEXT, in_context INTEGER, "
                "trimmed_at_seq INTEGER, trimmed_at TEXT, created_at TEXT);"
            )
            con.execute(
                "INSERT INTO messages VALUES (1,'f','t','tool','tool_result','Bash','x',?,1,NULL,NULL,'t')",
                (blob_id,),
            )
            con.commit()
            con.close()
            return path

        write_db("keep", "aaaaaaaaaaaaaaaa")
        gone = write_db("gone", "bbbbbbbbbbbbbbbb")
        for bid in ("aaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbb", "cccccccccccccccc"):
            (blobs / f"{bid}.txt").write_text(bid)
        t = time.time() - 40 * 86400
        os.utime(gone, (t, t))
        self.assertEqual(ttl.sweep(self.root, 30), ["gone"])
        self.assertEqual(sorted(p.name for p in blobs.iterdir()), ["aaaaaaaaaaaaaaaa.txt"])

    def test_sweep_orphan_blobs_skips_when_db_unreadable(self):
        blobs = self.root / "blobs"
        blobs.mkdir()
        (self.root / "sessions" / "bad.db").write_text("not-sqlite")
        target = blobs / "dddddddddddddddd.txt"
        target.write_text("d")
        self.assertEqual(ttl.sweep_orphan_blobs(self.root), [])
        self.assertTrue(target.exists())

    def test_zero_keeps_everything_and_stamp_limits_to_daily(self):
        self.touch("a.db", 40)
        self.assertEqual(ttl.maybe_sweep(self.root, 0), [])
        self.assertTrue((self.root / "sessions" / "a.db").exists())
        self.assertEqual(ttl.maybe_sweep(self.root, 30), ["a"])
        self.touch("b.db", 40)
        self.assertEqual(ttl.maybe_sweep(self.root, 30), [])
        self.assertTrue((self.root / "sessions" / "b.db").exists())
        self.assertEqual(ttl.maybe_sweep(self.root, 30, now=time.time() + 86401), ["b"])

    def test_startup_runs_in_background(self):
        self.touch("a.db", 40)
        startup(Settings(context_dir=self.root, ttl_days=30)).join(5)
        self.assertFalse((self.root / "sessions" / "a.db").exists())

    def test_settings_from_env(self):
        s = Settings.from_env({})
        self.assertEqual((s.ttl_days, s.embedding_model, s.embedding_url), (30, "", "http://127.0.0.1:11434/v1"))
        s = Settings.from_env({"CONTEXT_STORE_TTL_DAYS": "0", "CONTEXT_EMBEDDING_MODEL": " nomic-embed-text ",
                               "CONTEXT_EMBEDDING_URL": "http://x:1/v1/", "CONTEXT_EMBEDDING_API_KEY": "k"})
        self.assertEqual((s.ttl_days, s.embedding_model, s.embedding_url, s.embedding_api_key),
                         (0, "nomic-embed-text", "http://x:1/v1", "k"))


def fake_embed(url, model, key, texts, timeout=None):
    # Deterministic 3-d vectors: counts of "a", "b" and other characters.
    return [[float(t.count("a")), float(t.count("b")), float(len(t) - t.count("a") - t.count("b"))] for t in texts]


class EmbeddingTests(DbBase):
    def test_vectors_stored_off_request_path_and_nearest(self):
        embedder = embed_mod.Embedder(embed=fake_embed)
        original = embed_mod.embedder
        embed_mod.embedder = embedder
        self.addCleanup(setattr, embed_mod, "embedder", original)
        msgs = [{"role": "user", "content": "aaaa"}, {"role": "assistant", "content": "bbbb"},
                {"role": "user", "content": "aab"}]
        self.run_req(msgs, embedding_model="fake", embedding_url="http://unused")
        embedder.drain()
        rows = self.query("SELECT message_seq, model, dim FROM vectors ORDER BY message_seq")
        self.assertEqual(rows, [(1, "fake", 3), (2, "fake", 3), (3, "fake", 3)])
        conn = sqlite3.connect(self.path())
        try:
            near = sessiondb.nearest(conn, [1.0, 0.0, 0.0], "fake", 2)
        finally:
            conn.close()
        self.assertEqual([seq for seq, _ in near], [1, 3])
        self.assertAlmostEqual(near[0][1], 1.0, places=5)

    def test_embedding_failure_is_logged_and_retried_later(self):
        calls = []

        def broken(*a):
            calls.append(a)
            raise OSError("down")
        embedder = embed_mod.Embedder(embed=broken)
        self.run_req(history(1))
        err = io.StringIO()
        with redirect_stderr(err):
            embedder.notify(self.path(), "m", "http://x")
            embedder.drain()
            first_run = len(calls)
            embedder.notify(self.path(), "m", "http://x")
            embedder.drain()
        # 4 rows: batches of 4 and 2, then MAX_FAILURES single rows; no retry before RETRY_S.
        self.assertEqual((first_run, len(calls)), (5, 5))
        self.assertIn("smarter-compact: embedding failed OSError", err.getvalue())
        self.assertEqual(self.query("SELECT count(*) FROM vectors")[0][0], 0)

    def notify_all(self, embedder, max_chars=embed_mod.DEFAULT_MAX_CHARS, runs=1):
        with redirect_stderr(io.StringIO()) as err:
            for _ in range(runs):
                embedder.notify(self.path(), "m", "http://x", "", 30.0, max_chars)
                embedder.drain()
        return err.getvalue()

    def test_slow_endpoint_shrinks_batches_until_vectors_fill(self):
        self.run_req(history(5))
        conn = sqlite3.connect(self.path())
        conn.execute("UPDATE messages SET in_context = 0 WHERE seq BETWEEN 2 AND 6")
        conn.commit()
        conn.close()
        calls = []

        def slow(url, model, key, texts, timeout):
            # Like a CPU-only Ollama: more than two rows per call never finish inside the timeout.
            calls.append((list(texts), timeout))
            if len(texts) > 2:
                raise TimeoutError("timed out")
            return fake_embed(url, model, key, texts)
        embedder = embed_mod.Embedder(embed=slow)
        log = self.notify_all(embedder, max_chars=6)
        sizes = [len(t) for t, _ in calls]
        self.assertEqual(sizes[:2], [4, 2])
        self.assertEqual(self.query("SELECT count(*) FROM vectors WHERE dim = 3")[0][0], 20)
        self.assertEqual(embedder.batch_size("http://x", "m"), 2)
        # One failed probe at 4, then the run never grows past the size that failed.
        self.assertEqual(sizes.count(4), 1)
        self.assertTrue(all(n <= 2 for n in sizes[1:]), sizes)
        self.assertTrue(all(timeout == 30.0 * len(t) for t, timeout in calls))
        # Trimmed rows first, newest first; each row's head only.
        texts = self.query("SELECT seq, substr(text, 1, 6) FROM messages ORDER BY seq")
        self.assertEqual(calls[1][0], [texts[5][1], texts[4][1]])
        self.assertTrue(all(len(t) <= 6 for batch, _ in calls for t in batch))
        self.assertIn("smarter-compact: embedded rows=2 seconds=", log)
        self.assertIn("smarter-compact: embedding batch failed TimeoutError rows=4", log)

    def test_fast_endpoint_grows_batches(self):
        self.run_req(history(10))
        sizes = []

        def fast(url, model, key, texts, timeout):
            sizes.append(len(texts))
            return fake_embed(url, model, key, texts)
        embedder = embed_mod.Embedder(embed=fast)
        log = self.notify_all(embedder)
        self.assertEqual(sizes, [4, 8, 16, 12])
        self.assertEqual(embedder.batch_size("http://x", "m"), 32)
        self.assertIn("embedded rows=16 seconds=0.0 batch=32", log)

    def test_row_failing_alone_is_skipped_and_recorded(self):
        self.run_req(history(2))
        poison = self.query("SELECT seq FROM messages WHERE text = 'out 1'")[0][0]
        calls = []

        def picky(url, model, key, texts, timeout):
            calls.append(len(texts))
            if "out 1" in texts:
                raise ValueError("bad input")
            return fake_embed(url, model, key, texts)
        embedder = embed_mod.Embedder(embed=picky)
        log = self.notify_all(embedder, runs=embed_mod.MAX_FAILURES)
        rows = dict(self.query("SELECT message_seq, dim FROM vectors WHERE model = 'm'"))
        self.assertEqual(rows.pop(poison), 0)
        self.assertEqual(set(rows.values()), {3})
        self.assertEqual(len(rows), 7)
        self.assertIn(f"embedding skipped seq={poison} after {embed_mod.MAX_FAILURES} failures", log)
        before = len(calls)
        self.notify_all(embedder)
        self.assertEqual(len(calls), before)
        conn = sqlite3.connect(self.path())
        try:
            self.assertNotIn(poison, [seq for seq, _ in sessiondb.nearest(conn, [1.0, 1.0, 1.0], "m", 10)])
        finally:
            conn.close()

    def test_embedding_settings_from_env(self):
        s = Settings.from_env({})
        self.assertEqual((s.embedding_timeout_s, s.embedding_max_chars), (30.0, 4000))
        s = Settings.from_env({"CONTEXT_EMBEDDING_TIMEOUT_S": "120", "CONTEXT_EMBEDDING_MAX_CHARS": "2000"})
        self.assertEqual((s.embedding_timeout_s, s.embedding_max_chars), (120.0, 2000))
        s = Settings.from_env({"CONTEXT_EMBEDDING_TIMEOUT_S": "0", "CONTEXT_EMBEDDING_MAX_CHARS": "-5"})
        self.assertEqual((s.embedding_timeout_s, s.embedding_max_chars), (30.0, 4000))

    def test_embedding_off_by_default(self):
        self.assertEqual(Settings().embedding_model, "")
        self.run_req(history(1))
        self.assertEqual(self.query("SELECT count(*) FROM vectors")[0][0], 0)


if __name__ == "__main__":
    unittest.main()
