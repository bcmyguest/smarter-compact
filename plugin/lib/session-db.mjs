/**
 * Per-session SQLite store `sessions/<sid>.db` (docs/context-store.md, "Session database"): the JS side of
 * proxy/smarter_compact/sessiondb.py, ttl.py and embed.py. The schema and row rules must match the Python
 * byte for byte (scripts/tests/test_context_parity.mjs).
 *
 * node:sqlite is loaded on first use, so modules that import this one (hooks among them) never load it
 * unless they touch a database. Requires Node >= 24 (engines.node; node:sqlite with FTS5).
 */
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { rotateLog } from "./log-rotate.mjs";

export const SCHEMA_VERSION = 7;
/** Version 1, executed in order on an empty database. Same strings as SCHEMA in sessiondb.py. */
export const SCHEMA = [
  "CREATE TABLE messages (seq INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, thread TEXT NOT NULL, "
    + "role TEXT NOT NULL, kind TEXT NOT NULL, name TEXT, text TEXT NOT NULL, blob_id TEXT, "
    + "in_context INTEGER NOT NULL DEFAULT 1, trimmed_at_seq INTEGER, trimmed_at TEXT, created_at TEXT NOT NULL)",
  "CREATE INDEX messages_thread ON messages (thread, seq)",
  "CREATE TABLE lines (id INTEGER PRIMARY KEY, message_seq INTEGER NOT NULL REFERENCES messages (seq), "
    + "line_no INTEGER NOT NULL, role TEXT NOT NULL, text TEXT NOT NULL, UNIQUE (message_seq, line_no))",
  "CREATE VIRTUAL TABLE messages_fts USING fts5 (text, content='messages', content_rowid='seq')",
  "CREATE VIRTUAL TABLE lines_fts USING fts5 (text, content='lines', content_rowid='id')",
  "CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN "
    + "INSERT INTO messages_fts (rowid, text) VALUES (new.seq, new.text); END",
  "CREATE TRIGGER lines_fts_insert AFTER INSERT ON lines BEGIN "
    + "INSERT INTO lines_fts (rowid, text) VALUES (new.id, new.text); END",
  "CREATE TABLE vectors (message_seq INTEGER NOT NULL REFERENCES messages (seq), model TEXT NOT NULL, "
    + "dim INTEGER NOT NULL, blob BLOB NOT NULL, PRIMARY KEY (message_seq, model))",
];
/**
 * [version, DDL] run in order after SCHEMA on a new database, or from the stored version up on an older one; then
 * PRAGMA user_version = SCHEMA_VERSION. Same strings as MIGRATIONS in sessiondb.py.
 */
export const MIGRATIONS = [
  // Trims and fail-opens: kind 'trim' | 'fail_open'; version names the writer ("proxy:<hash>", "opencode:<v>").
  [2, ["CREATE TABLE events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, version TEXT NOT NULL, "
    + "location TEXT, error TEXT, tokens_before INTEGER, tokens_after INTEGER, dropped INTEGER)"]],
  // Per-request usage: kind 'usage', with provider-reported token counts, prompt-cache reads and writes, whether
  // the request carried a cut, and its cache_control breakpoints (Anthropic route).
  [3, ["model TEXT", "thread_root TEXT", "trimmed INTEGER", "input_tokens INTEGER", "cache_read_tokens INTEGER",
    "cache_creation_tokens INTEGER", "output_tokens INTEGER", "breakpoints INTEGER", "breakpoint_at TEXT"]
    .map((c) => `ALTER TABLE events ADD COLUMN ${c}`)],
  // Usage: the provider's applied context edits (Anthropic context_management.applied_edits) as compact JSON.
  [4, ["ALTER TABLE events ADD COLUMN applied_edits TEXT"]],
  // Trims: the characters of agent scratchpad the note carried (CONTEXT_SCRATCHPAD); NULL with it off.
  [5, ["ALTER TABLE events ADD COLUMN scratchpad_chars INTEGER"]],
  // Usage: Anthropic's cache writes by lifetime (usage.cache_creation ephemeral_1h / ephemeral_5m input tokens).
  [6, ["ALTER TABLE events ADD COLUMN cache_creation_1h_tokens INTEGER",
    "ALTER TABLE events ADD COLUMN cache_creation_5m_tokens INTEGER"]],
  // Forget (smart_compact_forget): a redacted row keeps the dedup content key of its original text, so a request
  // that still carries the original matches the stored row instead of storing the text again.
  [7, ["CREATE TABLE forgotten (message_seq INTEGER PRIMARY KEY REFERENCES messages (seq), "
    + "content_key TEXT NOT NULL, at TEXT NOT NULL)"]],
];
export const EVENT_COLUMNS = ["at", "kind", "version", "location", "error", "tokens_before", "tokens_after", "dropped", "model",
  "thread_root", "trimmed", "input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens", "breakpoints",
  "breakpoint_at", "applied_edits", "scratchpad_chars", "cache_creation_1h_tokens", "cache_creation_5m_tokens"];
export const DB_SUFFIX = ".db";
export const BUSY_TIMEOUT_MS = 1000;
export const DEFAULT_TTL_DAYS = 30;
export const DEFAULT_EMBEDDING_URL = "http://127.0.0.1:11434/v1";
/** Seconds per row of an embeddings call and characters of a row's head sent (settings.py DEFAULT_EMBEDDING_*). */
export const DEFAULT_EMBEDDING_TIMEOUT_S = 30;
export const DEFAULT_EMBEDDING_MAX_CHARS = 4000;
/** Adaptive batches and row skipping (embed.py START_BATCH, MAX_BATCH, GROW_FRACTION, MAX_FAILURES). */
export const EMBED_START_BATCH = 4;
const EMBED_MAX_BATCH = 32;
const EMBED_GROW_FRACTION = 0.25;
export const EMBED_MAX_FAILURES = 3;
const EMBED_RETRY_MS = 60_000;
const MAX_OPEN = 16;
const DAY_MS = 86_400_000;
export const TTL_STAMP = ".ttl-stamp";
/** Longest first, so `x.trim.json` groups under `x` (ttl.py SESSION_SUFFIXES). */
const SESSION_SUFFIXES = [".log.jsonl", ".trim.json", ".db-wal", ".db-shm", ".force", ".json", ".db"];
const BLOB_REF_RE = /^\[blob:([0-9a-f]{16})\]/;
const open = new Map();
const embedState = new Map();
/** { batch, ok } per url + model (embed.py Embedder._endpoint). */
const embedBatch = new Map();

/** node:sqlite, loaded on first use. */
export function sqlite() {
  return process.getBuiltinModule("node:sqlite");
}

/** Blob id named by a stored tool-result text ("[blob:<id>]..."), else null. */
export function blobRef(text) {
  return typeof text === "string" ? BLOB_REF_RE.exec(text)?.[1] ?? null : null;
}

/** Python fingerprint() of {prev, role, kind, name, text}: sorted keys, compact JSON, SHA-256, 32 hex. */
function rowFingerprint(prev, row) {
  const blob = JSON.stringify({ kind: row.kind, name: row.name ?? null, prev, role: row.role, text: row.text });
  return crypto.createHash("sha256").update(blob, "utf8").digest("hex").slice(0, 32);
}

/**
 * Give each row its fingerprint and thread, in order, in place (sessiondb.chain). A row's fingerprint hashes
 * the previous row's fingerprint with its own role, kind, name and text: a unique name for the row and its
 * position. thread is the first row's fingerprint. Dedup uses SessionDB's (thread, content, occurrence) key
 * instead, so an edited earlier message does not re-key the rows after it.
 */
export function chain(rows) {
  let prev = "";
  for (const row of rows) {
    prev = row.fingerprint = rowFingerprint(prev, row);
    row.thread = rows[0].fingerprint;
  }
  return rows;
}

function ensureFile(file) {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  try { fs.chmodSync(dir, 0o700); } catch { /* not ours */ }
  if (!fs.existsSync(file)) fs.closeSync(fs.openSync(file, "a", 0o600));
}

/** Writer connection in WAL mode with the schema in place; throws on a database of another schema version. */
export function connect(file) {
  ensureFile(file);
  const { DatabaseSync } = sqlite();
  const db = new DatabaseSync(file);
  try {
    db.exec(`PRAGMA busy_timeout = ${BUSY_TIMEOUT_MS}`);
    db.exec("PRAGMA journal_mode = WAL");
    db.exec("PRAGMA synchronous = NORMAL");
    const version = () => db.prepare("PRAGMA user_version").get().user_version;
    if (version() < SCHEMA_VERSION) {
      db.exec("BEGIN IMMEDIATE");
      try {
        const from = version();
        if (from < SCHEMA_VERSION) {
          if (from === 0) for (const ddl of SCHEMA) db.exec(ddl);
          for (const [target, ddls] of MIGRATIONS) {
            if (target > Math.max(from, 1)) for (const ddl of ddls) db.exec(ddl);
          }
          db.exec(`PRAGMA user_version = ${SCHEMA_VERSION}`);
        }
        db.exec("COMMIT");
      } catch (error) {
        db.exec("ROLLBACK");
        throw error;
      }
    }
    if (version() !== SCHEMA_VERSION) throw new Error(`unsupported session database user_version ${version()}`);
  } catch (error) {
    db.close();
    throw error;
  }
  return db;
}

function identity(file) {
  try {
    const st = fs.statSync(file);
    return `${st.dev}:${st.ino}`;
  } catch { return null; }
}

/** Hash of a row's role, kind, name and text (sessiondb.content_key): what dedup compares, with thread and occurrence. */
export function contentKey(row) {
  const blob = JSON.stringify({ kind: row.kind, name: row.name ?? null, role: row.role, text: row.text });
  return crypto.createHash("sha256").update(blob, "utf8").digest("hex").slice(0, 32);
}

const dedupKey = (thread, ck, n) => JSON.stringify([thread, ck, n]);

/**
 * One session's database plus an in-memory index of its rows (sessiondb.SessionDB). Dedup key: (thread,
 * contentKey, n), n being how many earlier rows of the same thread carry the same content; in a request n
 * counts in message order, over stored rows in seq order (docs/context-store.md).
 */
export class SessionDB {
  constructor(file) {
    this.file = file;
    this.db = connect(file);
    this.identity = identity(file);
    this.reset();
  }

  reset() {
    this.index = new Map(); // dedupKey -> seq
    this.counts = new Map(); // [thread, contentKey] -> stored rows with that content
    this.flags = new Map(); // seq -> in_context
    this.lastSeq = 0;
    this.dataVersion = null;
  }

  close() {
    try { this.db.close(); } catch { /* already closed */ }
  }

  add(seq, thread, ck) {
    const at = JSON.stringify([thread, ck]);
    const n = this.counts.get(at) ?? 0;
    this.counts.set(at, n + 1);
    const key = dedupKey(thread, ck, n);
    if (!this.index.has(key)) this.index.set(key, seq);
  }

  /** Index rows another connection appended and reload in_context flags, when it committed since the last look. */
  refresh() {
    const version = this.db.prepare("PRAGMA data_version").get().data_version;
    if (version === this.dataVersion) return;
    for (const r of this.db.prepare("SELECT m.seq, m.thread, m.role, m.kind, m.name, m.text, f.content_key AS forgotten_key "
      + "FROM messages m LEFT JOIN forgotten f ON f.message_seq = m.seq WHERE m.seq > ? ORDER BY m.seq").all(this.lastSeq)) {
      this.add(r.seq, r.thread, r.forgotten_key || contentKey(r));
      this.lastSeq = r.seq;
    }
    this.flags.clear();
    for (const r of this.db.prepare("SELECT seq, in_context FROM messages").all()) this.flags.set(r.seq, r.in_context);
    this.dataVersion = version;
  }

  /** Dedup key of each row of one request, in order. */
  keys(rows) {
    const seen = new Map();
    return rows.map((r) => {
      const ck = contentKey(r);
      const at = JSON.stringify([r.thread, ck]);
      const n = seen.get(at) ?? 0;
      seen.set(at, n + 1);
      return { ck, key: dedupKey(r.thread, ck, n) };
    });
  }

  /**
   * Store chained rows and their in_context flags (sessiondb.SessionDB.sync); returns the number of new rows.
   * rows: {fingerprint, thread, role, kind, name, text, blob_id, keep}; cutRow: index of the cut turn's first row or null.
   */
  sync(rows, cutRow, ts) {
    this.refresh();
    const keys = this.keys(rows);
    const { index, flags, db } = this;
    const fresh = keys.some(({ key }) => !index.has(key));
    const changes = rows.some((r, i) => index.has(keys[i].key) && flags.get(index.get(keys[i].key)) !== (r.keep ? 1 : 0));
    if (!fresh && !changes) return 0;
    db.exec("BEGIN IMMEDIATE");
    try {
      // Another writer may have committed since the look above; with the lock held, index its rows first.
      this.refresh();
      let added = 0;
      const insert = db.prepare("INSERT INTO messages (fingerprint, thread, role, kind, name, text, blob_id, created_at) "
        + "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (fingerprint) DO NOTHING");
      const insertLine = db.prepare("INSERT INTO lines (message_seq, line_no, role, text) VALUES (?, ?, ?, ?)");
      const seqs = keys.map(({ key }) => index.get(key));
      rows.forEach((r, i) => {
        if (seqs[i] !== undefined) return;
        const res = insert.run(r.fingerprint, r.thread, r.role, r.kind, r.name ?? null, r.text, r.blob_id ?? null, ts);
        let seq;
        if (res.changes === 1) {
          seq = Number(res.lastInsertRowid);
          r.text.split("\n").forEach((line, k) => insertLine.run(seq, k + 1, r.role, line));
          this.add(seq, r.thread, keys[i].ck);
          this.lastSeq = seq;
          flags.set(seq, 1);
          added++;
        } else {
          // Same chained fingerprint, stored at another occurrence index: reuse that row.
          const row = db.prepare("SELECT seq, in_context FROM messages WHERE fingerprint = ?").get(r.fingerprint);
          seq = row.seq;
          flags.set(seq, row.in_context);
        }
        if (!index.has(keys[i].key)) index.set(keys[i].key, seq);
        seqs[i] = seq;
      });
      const cutSeq = cutRow === null || cutRow === undefined ? null : seqs[cutRow];
      const back = db.prepare("UPDATE messages SET in_context = 1, trimmed_at_seq = NULL, trimmed_at = NULL WHERE seq = ?");
      const out = db.prepare("UPDATE messages SET in_context = 0, trimmed_at_seq = ?, trimmed_at = ? WHERE seq = ?");
      rows.forEach((r, i) => {
        const want = r.keep ? 1 : 0;
        if (flags.get(seqs[i]) === want) return;
        if (want) back.run(seqs[i]);
        else out.run(cutSeq, ts, seqs[i]);
        flags.set(seqs[i], want);
      });
      db.exec("COMMIT");
      this.dataVersion = db.prepare("PRAGMA data_version").get().data_version;
      return added;
    } catch (error) {
      try { db.exec("ROLLBACK"); } catch { /* no transaction */ }
      this.reset();
      throw error;
    }
  }

  /** Append one events row (SessionDB.add_event); fields are the other EVENT_COLUMNS. */
  addEvent(kind, at, version, fields = {}) {
    const values = { at, kind, version, ...fields };
    this.db.prepare(`INSERT INTO events (${EVENT_COLUMNS.join(", ")}) VALUES (${EVENT_COLUMNS.map(() => "?").join(", ")})`)
      .run(...EVENT_COLUMNS.map((c) => values[c] ?? null));
  }
}

/** Cached SessionDB for file; reopened when the file was deleted or replaced (TTL cleanup). */
export function openSession(file) {
  let entry = open.get(file);
  open.delete(file);
  if (entry && entry.identity !== identity(file)) {
    entry.close();
    entry = null;
  }
  entry ||= new SessionDB(file);
  open.set(file, entry);
  while (open.size > MAX_OPEN) {
    const [key, oldest] = open.entries().next().value;
    open.delete(key);
    oldest.close();
  }
  return entry;
}

/** Forget a cached connection after an error, so the next request reopens it. */
export function dropSession(file) {
  const entry = open.get(file);
  open.delete(file);
  entry?.close();
}

/** Close every cached connection; Windows cannot delete a database file that is still open. */
export function closeSessions() {
  for (const entry of open.values()) entry.close();
  open.clear();
}

/** Hours of events `smarter-compact context status` and the context_status MCP tool summarize by default. */
export const EVENT_WINDOW_HOURS = 24;
const LAST_ERRORS = 5;
// events.trimmed of a usage row whose request made a new cut (core.TRIM_NEW, context-trim.mjs TRIM_NEW).
const USAGE_NEW_CUT = 2;
const TOP_BREAKPOINT_LAYOUTS = 5;

function emptyUsage() {
  return { requests: 0, withCacheCounts: 0, inputTokens: 0, cacheReadTokens: 0, cacheCreationTokens: 0, outputTokens: 0,
    hitRatio: null, newCut: { requests: 0, withCacheCounts: 0, cacheCreationTokens: 0 },
    other: { requests: 0, withCacheCounts: 0, cacheCreationTokens: 0 }, breakpoints: {}, breakpointLayouts: [] };
}

/** Add one usage row to u. Only rows reporting input, cache read and cache creation enter the token sums. */
function addUsage(u, r, layouts) {
  u.requests++;
  const group = r.trimmed === USAGE_NEW_CUT ? u.newCut : u.other;
  group.requests++;
  if (r.breakpoints !== null && r.breakpoints !== undefined) {
    u.breakpoints[r.breakpoints] = (u.breakpoints[r.breakpoints] || 0) + 1;
    const at = r.breakpoint_at || "(none)";
    layouts.set(at, (layouts.get(at) || 0) + 1);
  }
  if (promptTokens(r) === null) return;
  u.withCacheCounts++;
  group.withCacheCounts++;
  u.inputTokens += r.input_tokens;
  u.cacheReadTokens += r.cache_read_tokens;
  u.cacheCreationTokens += r.cache_creation_tokens;
  u.outputTokens += r.output_tokens ?? 0;
  group.cacheCreationTokens += r.cache_creation_tokens;
}

/** A usage row's provider-reported prompt (uncached + cache read + cache write), or null when a count is missing. */
function promptTokens(r) {
  const counts = [r.input_tokens, r.cache_read_tokens, r.cache_creation_tokens];
  return counts.some((v) => v === null || v === undefined) ? null : counts.reduce((a, b) => a + b, 0);
}

/** Append-only MCP tool telemetry under the context root (smart_compact_search / sql / get); rotated to `.1` at 5 MB. */
export const TOOL_CALLS_LOG = "tool-calls.jsonl";
const TOP_SESSIONS = 5;

/**
 * Summary of the events rows written in the last `hours` across `sessions/*.db` (only files modified in that
 * window are opened): trims, estimated tokens saved, fail-opens, the newest errors, row counts per version and the
 * newest version per writer (`latest.proxy`, `latest.opencode`).
 * A trim's saving is its calibrated pre-trim estimate (`tokensBefore`) minus the provider-reported prompt of the request
 * that made the cut (the next usage row with trimmed = 2, `tokensAfter`); trims with no such row count in `unmeasured`
 * and add to neither. The writers' post-trim estimate (tokens_after) is not used: thinking blocks the cut voids are
 * dropped after it is taken. `usage` sums the usage rows'
 * provider-reported counts: cache hit ratio read / (read + creation + uncached input), cache-creation tokens on
 * requests that made a new cut (`newCut`) and on the rest (`other`), and the cache_control breakpoint counts
 * (`breakpoints`: count -> requests) and most common layouts (Anthropic route only).
 */
export function summarizeEvents(sessionsDir, { hours = EVENT_WINDOW_HOURS, now = Date.now() } = {}) {
  const sinceMs = now - hours * 3_600_000;
  const since = new Date(sinceMs).toISOString().replace(/\.\d{3}Z$/, "Z");
  const out = { hours, since, sessions: 0, trims: 0, tokensBefore: 0, tokensAfter: 0, tokensSaved: 0, unmeasured: 0, dropped: 0,
    failOpens: 0, lastErrors: [], versions: {}, latest: {}, unreadable: 0, usage: emptyUsage(),
    messages: 0, inContext: 0, outOfContext: 0, withBlob: 0, topSessions: [] };
  const layouts = new Map();
  const perSession = [];
  let names = [];
  try { names = fs.readdirSync(sessionsDir).filter((n) => n.endsWith(DB_SUFFIX)); } catch { return out; }
  const errors = [];
  const latestAt = {};
  for (const name of names) {
    const file = path.join(sessionsDir, name);
    try { if (fs.statSync(file).mtimeMs < sinceMs) continue; } catch { continue; }
    let db;
    try {
      db = new (sqlite().DatabaseSync)(file, { readOnly: true });
      db.exec(`PRAGMA busy_timeout = ${BUSY_TIMEOUT_MS}`);
      if (!db.prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'").get()) continue;
      const rows = db.prepare("SELECT * FROM events WHERE at >= ? ORDER BY id").all(since);
      if (!rows.length) continue;
      out.sessions++;
      const sid = name.slice(0, -DB_SUFFIX.length);
      let sessionTrims = 0;
      let sessionDropped = 0;
      let pending = null;
      for (const r of rows) {
        out.versions[r.version] = (out.versions[r.version] || 0) + 1;
        const writer = String(r.version).split(":")[0];
        if (r.at >= (latestAt[writer] || "")) [latestAt[writer], out.latest[writer]] = [r.at, r.version];
        if (r.kind === "trim") {
          out.trims++;
          sessionTrims++;
          if (pending !== null) out.unmeasured++;
          pending = r.tokens_before ?? 0;
          out.dropped += r.dropped ?? 0;
          sessionDropped += r.dropped ?? 0;
        } else if (r.kind === "fail_open") {
          out.failOpens++;
          errors.push({ at: r.at, session: sid, error: r.error, location: r.location, version: r.version });
        } else if (r.kind === "usage") {
          addUsage(out.usage, r, layouts);
          const prompt = promptTokens(r);
          if (pending !== null && r.trimmed === USAGE_NEW_CUT && prompt !== null) {
            out.tokensBefore += pending;
            out.tokensAfter += prompt;
            pending = null;
          }
        }
      }
      if (pending !== null) out.unmeasured++;
      let msg = { n: 0, inn: 0, out: 0, blobs: 0 };
      if (db.prepare("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages'").get()) {
        msg = db.prepare("SELECT count(*) AS n, coalesce(sum(in_context = 1), 0) AS inn, "
          + "coalesce(sum(in_context = 0), 0) AS out, coalesce(sum(blob_id IS NOT NULL), 0) AS blobs FROM messages").get();
        out.messages += Number(msg.n);
        out.inContext += Number(msg.inn);
        out.outOfContext += Number(msg.out);
        out.withBlob += Number(msg.blobs);
      }
      perSession.push({ session: sid, trims: sessionTrims, dropped: sessionDropped,
        messages: Number(msg.n), outOfContext: Number(msg.out) });
    } catch { out.unreadable++; } finally { db?.close(); }
  }
  out.tokensSaved = out.tokensBefore - out.tokensAfter;
  const u = out.usage;
  const prompt = u.inputTokens + u.cacheReadTokens + u.cacheCreationTokens;
  u.hitRatio = prompt > 0 ? u.cacheReadTokens / prompt : null;
  u.breakpointLayouts = [...layouts].sort((a, b) => b[1] - a[1] || (a[0] < b[0] ? -1 : 1)).slice(0, TOP_BREAKPOINT_LAYOUTS)
    .map(([at, requests]) => ({ at, requests }));
  out.lastErrors = errors.sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0)).slice(0, LAST_ERRORS);
  out.topSessions = perSession.sort((a, b) => b.trims - a.trims || b.dropped - a.dropped || b.outOfContext - a.outOfContext
    || (a.session < b.session ? -1 : 1)).slice(0, TOP_SESSIONS);
  return out;
}

/** Best-effort append of one MCP tool call; never throws (stats are optional). */
export function recordToolCall(contextDir, entry) {
  try {
    fs.mkdirSync(contextDir, { recursive: true, mode: 0o700 });
    const line = `${JSON.stringify({ at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z"), ...entry })}\n`;
    const file = path.join(contextDir, TOOL_CALLS_LOG);
    rotateLog(file);
    fs.appendFileSync(file, line, { mode: 0o600 });
  } catch { /* leave stats incomplete rather than fail the tool */ }
}

/**
 * Counts of smart_compact_* MCP calls from `tool-calls.jsonl` in the last `hours`.
 * @returns {{ searches: number, sql: number, gets: number, searchHits: number, sqlRows: number, getChars: number, unreadable: number }}
 */
export function summarizeToolCalls(contextDir, { hours = EVENT_WINDOW_HOURS, now = Date.now() } = {}) {
  const out = { searches: 0, sql: 0, gets: 0, searchHits: 0, sqlRows: 0, getChars: 0, unreadable: 0 };
  const sinceMs = now - hours * 3_600_000;
  let text = "";
  for (const name of [`${TOOL_CALLS_LOG}.1`, TOOL_CALLS_LOG]) {
    try { text += fs.readFileSync(path.join(contextDir, name), "utf8"); } catch { /* generation absent */ }
  }
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    let row;
    try { row = JSON.parse(line); } catch { out.unreadable++; continue; }
    const at = Date.parse(row.at || "");
    if (!Number.isFinite(at) || at < sinceMs) continue;
    if (row.tool === "smart_compact_search") {
      out.searches++;
      out.searchHits += Number(row.hits) || 0;
    } else if (row.tool === "smart_compact_sql") {
      out.sql++;
      out.sqlRows += Number(row.rows) || 0;
    } else if (row.tool === "smart_compact_get") {
      out.gets++;
      out.getChars += Number(row.chars) || 0;
    }
  }
  return out;
}

/** Blob files under `blobs/`: count and total bytes (mtime in the window when hours is set). */
export function summarizeBlobs(blobsDir, { hours = EVENT_WINDOW_HOURS, now = Date.now() } = {}) {
  const out = { files: 0, bytes: 0 };
  const sinceMs = now - hours * 3_600_000;
  let names = [];
  try { names = fs.readdirSync(blobsDir); } catch { return out; }
  for (const name of names) {
    if (!name.endsWith(".txt")) continue;
    try {
      const st = fs.statSync(path.join(blobsDir, name));
      if (st.mtimeMs < sinceMs) continue;
      out.files++;
      out.bytes += st.size;
    } catch { /* skip */ }
  }
  return out;
}

/**
 * Full context-stats payload: events (+ message inventory), MCP tool calls, and compressed blobs.
 * `root` is `$SMARTER_COMPACT_STATE_DIR/context`.
 */
export function summarizeContextStats(root, opts = {}) {
  const events = summarizeEvents(path.join(root, "sessions"), opts);
  return {
    ...events,
    tools: summarizeToolCalls(root, opts),
    blobs: summarizeBlobs(path.join(root, "blobs"), opts),
  };
}

/** "proxy:" + first 12 hex of SHA-256 over dir's *.py files (name, NUL, bytes, NUL, by name): core.code_hash. */
export function proxyCodeVersion(dir) {
  const hash = crypto.createHash("sha256");
  for (const name of fs.readdirSync(dir).filter((n) => n.endsWith(".py")).sort()) {
    hash.update(Buffer.concat([Buffer.from(`${name}\0`), fs.readFileSync(path.join(dir, name)), Buffer.from("\0")]));
  }
  return `proxy:${hash.digest("hex").slice(0, 12)}`;
}

// Vectors: float32 little-endian, brute-force cosine (sessions hold thousands of rows).
export function packVector(values) {
  const buffer = Buffer.alloc(values.length * 4);
  values.forEach((v, i) => buffer.writeFloatLE(v, i * 4));
  return buffer;
}

export function unpackVector(blob, dim) {
  const buffer = Buffer.from(blob.buffer, blob.byteOffset, blob.byteLength);
  return Array.from({ length: dim }, (_, i) => buffer.readFloatLE(i * 4));
}

export function cosine(a, b) {
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < Math.min(a.length, b.length); i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return na && nb ? dot / (Math.sqrt(na) * Math.sqrt(nb)) : 0;
}

/**
 * [seq, similarity] of the k vectors of `model` closest to query, best first, ties by seq (sessiondb.nearest).
 * `where` / `params` narrow the rows through the joined messages table `m`.
 */
export function nearest(db, query, model, k, where = "", params = []) {
  const rows = db.prepare(`SELECT v.message_seq AS seq, v.dim AS dim, v.blob AS blob FROM vectors v `
    + `JOIN messages m ON m.seq = v.message_seq WHERE v.model = ?${where ? ` AND ${where}` : ""}`).all(model, ...params);
  return rows.filter((r) => r.dim === query.length)
    .map((r) => [r.seq, cosine(query, unpackVector(r.blob, r.dim))])
    .sort((a, b) => b[1] - a[1] || a[0] - b[0])
    .slice(0, k);
}

/** Vectors for texts from an OpenAI-compatible POST <url>/embeddings, in input order; timeoutMs per call. */
export async function fetchEmbeddings({ url, model, apiKey }, texts, fetchFn = globalThis.fetch,
  timeoutMs = DEFAULT_EMBEDDING_TIMEOUT_S * 1000) {
  const response = await fetchFn(`${String(url).replace(/\/+$/, "")}/embeddings`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}) },
    body: JSON.stringify({ model, input: texts }),
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (!response.ok) throw new Error(`embeddings HTTP ${response.status}`);
  const payload = await response.json();
  const data = [...(payload?.data || [])].sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
  const vectors = data.map((d) => d.embedding.map(Number));
  if (vectors.length !== texts.length) throw new Error("embedding count mismatch");
  return vectors;
}

/** Rows without a `model` vector, trimmed first, then newest first; seqs in exclude are left out (embed.py pending_rows). */
export function pendingRows(db, model, limit, exclude = []) {
  const skip = exclude.length ? ` AND m.seq NOT IN (${exclude.map(() => "?").join(",")})` : "";
  return db.prepare("SELECT m.seq AS seq, m.text AS text FROM messages m LEFT JOIN vectors v "
    + `ON v.message_seq = m.seq AND v.model = ? WHERE v.message_seq IS NULL AND length(trim(m.text)) > 0${skip} `
    + "ORDER BY m.in_context, m.seq DESC LIMIT ?").all(model, ...[...exclude].sort((a, b) => a - b), limit);
}

/** { filled, pending, skipped } row counts for `model`; skipped rows carry a dim-0 vector. */
export function vectorCounts(db, model) {
  const v = db.prepare("SELECT coalesce(sum(dim > 0), 0) AS filled, coalesce(sum(dim = 0), 0) AS skipped FROM vectors WHERE model = ?").get(model);
  const pending = db.prepare("SELECT count(*) AS n FROM messages m LEFT JOIN vectors v ON v.message_seq = m.seq AND v.model = ? "
    + "WHERE v.message_seq IS NULL AND length(trim(m.text)) > 0").get(model).n;
  return { filled: Number(v.filled), pending: Number(pending), skipped: Number(v.skipped) };
}

function insertVectors(db, model, rows) {
  const insert = db.prepare("INSERT OR IGNORE INTO vectors (message_seq, model, dim, blob) VALUES (?, ?, ?, ?)");
  db.exec("BEGIN IMMEDIATE");
  try {
    for (const [seq, vec] of rows) insert.run(seq, model, vec.length, packVector(vec));
    db.exec("COMMIT");
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  }
}

const errorName = (error) => error?.name || typeof error;

/**
 * Embed every row of an open SessionDB that lacks a `model` vector (embed.py embed_pending); returns rows stored.
 * config: { model, url, apiKey, timeoutS, maxChars }. state carries `batch`, `ok` (the endpoint's last call
 * succeeded) and `failures` (Map seq → single-row failures) between runs. A failed size caps growth for the run. Rethrows the last error once MAX_FAILURES single-row calls fail in a row with nothing
 * stored this run.
 */
export async function embedPending(session, config, fetchFn, { state = {}, log = () => {} } = {}) {
  const timeoutS = config.timeoutS > 0 ? config.timeoutS : DEFAULT_EMBEDDING_TIMEOUT_S;
  const maxChars = config.maxChars > 0 ? config.maxChars : DEFAULT_EMBEDDING_MAX_CHARS;
  state.batch ??= EMBED_START_BATCH;
  state.ok ??= false;
  state.failures ??= new Map();
  const { failures } = state;
  const tried = new Set();
  let stored = 0;
  let streak = 0;
  let ceiling = EMBED_MAX_BATCH;
  for (;;) {
    const batch = state.batch;
    // Rows that never failed first; earlier failures get another try once those are done.
    let rows = pendingRows(session.db, config.model, batch, [...new Set([...tried, ...failures.keys()])]);
    if (!rows.length) rows = pendingRows(session.db, config.model, batch, [...tried]);
    if (!rows.length) return stored;
    const timeoutMs = timeoutS * 1000 * rows.length;
    const start = performance.now();
    let vectors;
    try {
      vectors = await fetchEmbeddings(config, rows.map((r) => Array.from(r.text).slice(0, maxChars).join("")), fetchFn, timeoutMs);
    } catch (error) {
      const seconds = ((performance.now() - start) / 1000).toFixed(1);
      if (rows.length > 1) {
        ceiling = Math.max(1, Math.floor(rows.length / 2));
        state.batch = Math.max(1, Math.floor(rows.length / 2));
        log(`embedding batch failed ${errorName(error)} rows=${rows.length} seconds=${seconds} batch=${state.batch}`);
        continue;
      }
      const seq = rows[0].seq;
      tried.add(seq);
      failures.set(seq, (failures.get(seq) || 0) + 1);
      streak++;
      log(`embedding row failed ${errorName(error)} seq=${seq} failures=${failures.get(seq)} seconds=${seconds} batch=1`);
      // Blame the row only while the endpoint works for other rows.
      if (failures.get(seq) >= EMBED_MAX_FAILURES && (stored || state.ok)) {
        insertVectors(session.db, config.model, [[seq, []]]);
        failures.delete(seq);
        log(`embedding skipped seq=${seq} after ${EMBED_MAX_FAILURES} failures`);
      }
      // The endpoint looks down: scheduleEmbedding retries the database after EMBED_RETRY_MS.
      if (streak >= EMBED_MAX_FAILURES && !stored) {
        state.ok = false;
        throw error;
      }
      continue;
    }
    const ms = performance.now() - start;
    insertVectors(session.db, config.model, rows.map((r, i) => [r.seq, vectors[i]]));
    for (const r of rows) failures.delete(r.seq);
    stored += rows.length;
    streak = 0;
    state.ok = true;
    if (rows.length === batch && ms < EMBED_GROW_FRACTION * timeoutMs) state.batch = Math.min(ceiling, batch * 2);
    log(`embedded rows=${rows.length} seconds=${(ms / 1000).toFixed(1)} batch=${state.batch}`);
  }
}

/** Next batch size for an embeddings endpoint and model. */
export function embeddingBatchSize(url, model) {
  return embedBatch.get(`${url}\u0000${model}`)?.batch ?? EMBED_START_BATCH;
}

const stderrLog = (message) => process.stderr.write(`smarter-compact: ${message}\n`);

/**
 * Embed a session's new rows in the background: at most one run per database at a time, retried no sooner
 * than a minute after a failing run. Returns the run's promise (resolves to rows stored, or 0); never rejects.
 * One log line per batch goes to `log` (default: stderr).
 */
export function scheduleEmbedding(file, config, { fetchFn, onError, log = stderrLog } = {}) {
  if (!config?.model || !config?.url) return Promise.resolve(0);
  const state = embedState.get(file) || { running: null, again: false, retryAt: 0, failures: new Map() };
  embedState.set(file, state);
  if (Date.now() < state.retryAt) return Promise.resolve(0);
  if (state.running) {
    state.again = true;
    return state.running;
  }
  const batchKey = `${config.url}\u0000${config.model}`;
  state.running = (async () => {
    let total = 0;
    const endpoint = embedBatch.get(batchKey) || { batch: EMBED_START_BATCH, ok: false };
    const run = { ...endpoint, failures: state.failures };
    try {
      do {
        state.again = false;
        total += await embedPending(openSession(file), config, fetchFn, { state: run, log });
      } while (state.again);
    } catch (error) {
      state.retryAt = Date.now() + EMBED_RETRY_MS;
      onError?.(error);
    } finally {
      embedBatch.set(batchKey, { batch: run.batch, ok: run.ok });
      state.running = null;
    }
    return total;
  })();
  return state.running;
}

/** [sid, suffix] for a session file name, or null for anything else (ttl.py session_of). */
export function sessionOf(name) {
  if (name.startsWith(".")) return null;
  const suffix = SESSION_SUFFIXES.find((s) => name.endsWith(s) && name.length > s.length);
  return suffix ? [name.slice(0, -suffix.length), suffix] : null;
}

/**
 * Distinct messages.blob_id values across remaining session databases, or null if any DB is unreadable
 * (ttl.py referenced_blob_ids). Fail closed so a corrupt DB cannot orphan its blobs into deletion.
 */
const SQLITE_MAGIC = Buffer.from("SQLite format 3\0");
// Start of a cleared tool-input string's placeholder and its length besides the count digits; context-trim.mjs.
const INPUT_MASK_PREFIX = "[smarter-compact: tool input cleared to save context;";
const INPUT_PLACEHOLDER_FIXED = INPUT_MASK_PREFIX.length + ' chars; smart_compact_get(id="'.length + 16 + '") returns it]'.length + 1;

/** True when path starts with the SQLite magic (avoids opening placeholder .db files that would rewrite -wal). */
function sqliteHeader(file) {
  const fd = fs.openSync(file, "r");
  try {
    const buf = Buffer.alloc(16);
    return fs.readSync(fd, buf, 0, 16, 0) === 16 && buf.equals(SQLITE_MAGIC);
  } catch {
    return false;
  } finally {
    fs.closeSync(fd);
  }
}

/** A stored tool_use row's input: its JSON, else the text itself (a freeform input); ttl.py _input_value. */
function inputValue(text) {
  try { return JSON.parse(text); } catch { return text; }
}

/** Every string of value a cleared input may name as a blob, at any bar; ttl.py clearable_strings. */
function clearableStrings(value) {
  if (typeof value === "string") {
    const n = Array.from(value).length;
    return n > INPUT_PLACEHOLDER_FIXED + String(n).length && !value.startsWith(INPUT_MASK_PREFIX) ? [value] : [];
  }
  if (Array.isArray(value)) return value.flatMap(clearableStrings);
  if (value && typeof value === "object") return Object.values(value).flatMap(clearableStrings);
  return [];
}

export function referencedBlobIds(sessionsDir) {
  let names;
  try { names = fs.readdirSync(sessionsDir, { withFileTypes: true }); } catch (error) {
    // Missing sessions → no refs; any other read error fails closed so blobs are not deleted.
    return error?.code === "ENOENT" ? new Set() : null;
  }
  const ids = new Set();
  const { DatabaseSync } = sqlite();
  for (const entry of names) {
    if (!entry.isFile() || !entry.name.endsWith(DB_SUFFIX)) continue;
    const file = path.join(sessionsDir, entry.name);
    if (!sqliteHeader(file)) return null;
    let db;
    try {
      db = new DatabaseSync(file, { readOnly: true });
      for (const row of db.prepare("SELECT DISTINCT blob_id FROM messages WHERE blob_id IS NOT NULL").all()) {
        if (row.blob_id) ids.add(row.blob_id);
      }
      // A cleared result's placeholder names blobId(text) of its stored row (context-trim.mjs blobId).
      for (const row of db.prepare("SELECT text FROM messages WHERE kind = 'tool_result' AND blob_id IS NULL").iterate()) {
        ids.add(crypto.createHash("sha256").update(row.text, "utf8").digest("hex").slice(0, 16));
      }
      // A cleared tool-input string's placeholder names blobId of that string (context-trim.mjs inputPlaceholder).
      for (const row of db.prepare("SELECT text FROM messages WHERE kind = 'tool_use'").iterate()) {
        for (const text of clearableStrings(inputValue(row.text))) {
          ids.add(crypto.createHash("sha256").update(text, "utf8").digest("hex").slice(0, 16));
        }
      }
    } catch {
      try { db?.close(); } catch { /* already broken */ }
      return null;
    }
    try { db.close(); } catch { /* ignore */ }
  }
  return ids;
}

/**
 * Delete blobs/<id>.txt with no remaining messages.blob_id reference (ttl.py sweep_orphan_blobs).
 * Returns deleted ids, sorted. Skips the whole pass when any remaining session database is unreadable.
 */
export function sweepOrphanBlobs(root) {
  const refs = referencedBlobIds(path.join(root, "sessions"));
  if (!refs) return [];
  const dir = path.join(root, "blobs");
  let names;
  try { names = fs.readdirSync(dir, { withFileTypes: true }); } catch { return []; }
  const deleted = [];
  for (const entry of names) {
    if (!entry.isFile() || !entry.name.endsWith(".txt")) continue;
    const bid = entry.name.slice(0, -".txt".length);
    if (refs.has(bid)) continue;
    try { fs.unlinkSync(path.join(dir, entry.name)); } catch (error) {
      if (error?.code !== "ENOENT") throw error;
      continue;
    }
    deleted.push(bid);
  }
  return deleted.sort();
}

/** Delete every session whose newest file is older than ttlDays (ttl.py sweep); returns the deleted sids, sorted.
 * After session cleanup, also deletes orphaned blobs under blobs/ (shared across sessions). */
export function sweepContextStore(root, ttlDays, now = Date.now()) {
  if (!(ttlDays > 0)) return [];
  const dir = path.join(root, "sessions");
  let names;
  try { names = fs.readdirSync(dir, { withFileTypes: true }); } catch (error) {
    if (error?.code === "ENOENT") {
      sweepOrphanBlobs(root);
      return [];
    }
    return [];
  }
  const groups = new Map();
  for (const entry of names) {
    const parsed = sessionOf(entry.name);
    if (!parsed || !entry.isFile()) continue;
    const file = path.join(dir, entry.name);
    let mtime;
    try { mtime = fs.lstatSync(file).mtimeMs; } catch { continue; }
    const group = groups.get(parsed[0]) || { newest: 0, files: [] };
    group.newest = Math.max(group.newest, mtime);
    group.files.push(file);
    groups.set(parsed[0], group);
  }
  const cutoff = now - ttlDays * DAY_MS;
  const deleted = [];
  for (const [sid, group] of groups) {
    if (group.newest >= cutoff) continue;
    for (const file of group.files) {
      dropSession(file);
      try { fs.unlinkSync(file); } catch (error) { if (error?.code !== "ENOENT") throw error; }
    }
    deleted.push(sid);
  }
  sweepOrphanBlobs(root);
  return deleted.sort();
}

/** sweepContextStore at most once a day, recorded by the mtime of <root>/.ttl-stamp (ttl.py maybe_sweep). */
export function maybeSweepContextStore(root, ttlDays, now = Date.now()) {
  if (!(ttlDays > 0)) return [];
  const stamp = path.join(root, TTL_STAMP);
  try {
    if (now - fs.statSync(stamp).mtimeMs < DAY_MS) return [];
  } catch (error) {
    if (error?.code !== "ENOENT") return [];
  }
  try {
    if (!fs.statSync(root).isDirectory()) return [];
  } catch { return []; }
  fs.closeSync(fs.openSync(stamp, "a", 0o600));
  fs.utimesSync(stamp, now / 1000, now / 1000);
  return sweepContextStore(root, ttlDays, now);
}

function positiveNumber(raw, fallback) {
  const text = String(raw ?? "").trim();
  const n = text ? Number(text) : NaN;
  return Number.isFinite(n) && n > 0 ? n : fallback;
}

function positiveInt(raw, fallback) {
  const text = String(raw ?? "").trim();
  return /^[+-]?\d+$/.test(text) && Number.parseInt(text, 10) > 0 ? Number.parseInt(text, 10) : fallback;
}

/**
 * Store settings from the environment (the proxy's Settings): CONTEXT_STORE_TTL_DAYS (default 30, 0 keeps
 * everything), CONTEXT_EMBEDDING_MODEL (empty turns vectors off), CONTEXT_EMBEDDING_URL, CONTEXT_EMBEDDING_API_KEY,
 * CONTEXT_EMBEDDING_TIMEOUT_S (seconds per row of a call, default 30) and CONTEXT_EMBEDDING_MAX_CHARS (default 4000).
 */
export function storeSettings(env = process.env) {
  const ttl = String(env.CONTEXT_STORE_TTL_DAYS ?? "").trim();
  return {
    ttlDays: /^[+-]?\d+$/.test(ttl) ? Number.parseInt(ttl, 10) : DEFAULT_TTL_DAYS,
    embeddingModel: String(env.CONTEXT_EMBEDDING_MODEL ?? "").trim(),
    embeddingUrl: (String(env.CONTEXT_EMBEDDING_URL ?? "").trim() || DEFAULT_EMBEDDING_URL).replace(/\/+$/, ""),
    embeddingApiKey: String(env.CONTEXT_EMBEDDING_API_KEY ?? "").trim(),
    embeddingTimeoutS: positiveNumber(env.CONTEXT_EMBEDDING_TIMEOUT_S, DEFAULT_EMBEDDING_TIMEOUT_S),
    embeddingMaxChars: positiveInt(env.CONTEXT_EMBEDDING_MAX_CHARS, DEFAULT_EMBEDDING_MAX_CHARS),
  };
}
