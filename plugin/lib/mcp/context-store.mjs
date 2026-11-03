/**
 * Provider-independent smart_compact_* tools over the session databases (`sessions/<sid>.db`) and blobs, and
 * context_status over their events tables.
 * Schema and file layout follow docs/context-store.md. Reads only: search and get open each database
 * read-only; smart_compact_sql runs in a child process on a read-only connection.
 */

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { contextRoot as hookContextRoot, sanitizeSessionId as hookSanitizeSessionId } from "../context-store.mjs";
import {
  DB_SUFFIX, EVENT_WINDOW_HOURS, SCHEMA_VERSION, fetchEmbeddings, maybeSweepContextStore, nearest, proxyCodeVersion,
  recordToolCall, sqlite, storeSettings, summarizeEvents, vectorCounts,
} from "../session-db.mjs";

const PKG_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const SQL_RUNNER = path.join(path.dirname(fileURLToPath(import.meta.url)), "context-sql-runner.mjs");
const DEFAULT_LIMIT = 8;
const MAX_SESSIONS = 20;
const RESULT_MAX_CHARS = 8000;
const EXCERPT_CHARS = 400;
const BLOB_MAX_CHARS = 100_000;
const BLOB_ID_RE = /^[0-9a-f]{16}$/;
const PATTERN_MAX_CHARS = 200;
const PATTERN_ROW_CHARS = 20_000;
const PATTERN_BUDGET_MS = 1000;
const CONTEXT_MAX = 5;
const ROLES = ["user", "assistant", "tool"];
const DEFAULT_ROLES = ["user", "tool"];
/** Reciprocal rank fusion constant (Cormack et al. 2009) and vector candidates per search. */
const RRF_K = 60;
const VECTOR_CANDIDATES = 50;
const SQL_TIMEOUT_MS = 2000;
const SQL_ROW_CAP = 200;
const SQL_CELL_MAX_CHARS = 2000;
const SQL_RESULT_MAX_CHARS = 30_000;
const SQL_MAX_CHARS = 10_000;
const STATUS_MAX_HOURS = 24 * 30;
/** Words that may not appear outside string literals and quoted identifiers in smart_compact_sql. */
const SQL_FORBIDDEN = new Set([
  "ATTACH", "DETACH", "PRAGMA", "INSERT", "UPDATE", "DELETE", "UPSERT", "CREATE", "DROP", "ALTER",
  "VACUUM", "REINDEX", "ANALYZE", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE", "TRANSACTION",
]);

const SCHEMA_TEXT = "messages(seq INTEGER PRIMARY KEY, fingerprint, thread, role 'user'|'assistant'|'tool', "
  + "kind 'text'|'tool_use'|'tool_result'|'thinking', name (tool name or NULL), text, blob_id, in_context 0|1, "
  + "trimmed_at_seq, trimmed_at, created_at); lines(id, message_seq, line_no (1-based), role, text) with one row per "
  + "line of each message; messages_fts(text) and lines_fts(text): FTS5 indexes whose rowid is messages.seq / lines.id; "
  + "vectors(message_seq, model, dim, blob); events(id, at, kind 'trim'|'fail_open'|'usage', version, location, error, "
  + "tokens_before, tokens_after, dropped, model, thread_root, trimmed 0|1|2, input_tokens, cache_read_tokens, "
  + "cache_creation_tokens, output_tokens, breakpoints, breakpoint_at), one row per trim, fail-open or model call; "
  + "trim token figures estimated, usage counts provider-reported (NULL when not reported).";

export const CONTEXT_STORE_MCP_TOOLS = [
  {
    name: "smart_compact_search",
    title: "Search this session's stored context",
    description:
      "Search every stored message of this session, including messages trimmed from your context, and return matching "
      + "rows with an excerpt. Use it when an [smarter-compact: continuing session …] pointer says earlier messages were trimmed; "
      + "pass that session_id (without one it searches recent sessions in this git root). Pass exactly one of query "
      + "(full-text words) or pattern (regex). Roles default to user and tool. total_matches counts rows; line_matches "
      + "is an exact count of matching lines across the whole session; use it for 'how many' questions, or smart_compact_sql "
      + "for grouped counts. Rows citing a compressed result return blob_ids for smart_compact_get.",
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    inputSchema: {
      type: "object",
      properties: {
        query: { type: "string", description: "Words to find (case-insensitive whole words; rows with more words rank first). Omit when passing pattern" },
        pattern: {
          type: "string",
          description: `JavaScript regex source, at most ${PATTERN_MAX_CHARS} characters; tested against each row's first ${PATTERN_ROW_CHARS} characters and against every line. Omit when passing query`,
        },
        ignore_case: { type: "boolean", description: "Match pattern case-insensitively (default false)" },
        context: { type: "number", description: `Neighbouring rows of the same conversation to return before and after each hit, 0-${CONTEXT_MAX} (default 0)` },
        session_id: { type: "string", description: "Session id from the [smarter-compact: continuing session …] pointer. Omit it, or pass \"current\", to search this workspace's own sessions" },
        limit: { type: "number", description: `Max rows (default ${DEFAULT_LIMIT})` },
        role: {
          description: 'Roles to search: "user", "assistant", "tool", a list of them, or "all" (default ["user", "tool"])',
          anyOf: [{ type: "string" }, { type: "array", items: { type: "string" } }],
        },
        in_context: { type: "boolean", description: "true: only rows still in your context; false: only trimmed rows (default both)" },
        mode: {
          type: "string",
          enum: ["hybrid", "lexical"],
          description: "hybrid merges full-text and embedding rankings (default when the session has vectors); lexical is full-text only",
        },
      },
      additionalProperties: false,
    },
  },
  {
    name: "smart_compact_get",
    title: "Read a compressed tool result",
    description:
      "Return the full text of a tool result the context proxy compressed, by the 16-hex id from "
      + '[smarter-compact: tool result compressed … smart_compact_get(id="<id>")] or a smart_compact_search blob_ids entry. '
      + `Output is capped at ${BLOB_MAX_CHARS} characters; pass offset (next_offset) to page.`,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    inputSchema: {
      type: "object",
      properties: {
        id: { type: "string", description: "Blob id: exactly 16 lowercase hex characters" },
        offset: { type: "number", description: "Character offset to start from (default 0)" },
      },
      required: ["id"],
      additionalProperties: false,
    },
  },
  {
    name: "smart_compact_sql",
    title: "Query a session database with SQL",
    description:
      "Run one read-only SQLite SELECT (or WITH … SELECT) against a session's database, for counts, aggregates and "
      + "joins that smart_compact_search cannot express. The database holds every message of the session, in or out of "
      + `your context. Schema: ${SCHEMA_TEXT} `
      + "Full-text: `lines_fts MATCH 'error AND payments'` (FTS5 syntax; join lines ON lines.id = lines_fts.rowid); "
      + "exact substrings: `text LIKE '%level=error%'` (case-insensitive for ASCII). Examples: "
      + "(1) lines naming a level and a service: SELECT count(*) AS n FROM lines WHERE text LIKE '%level=error%' AND text LIKE '%service=payments%'; "
      + "(2) tool calls per tool: SELECT name, count(*) AS calls FROM messages WHERE kind = 'tool_use' GROUP BY name ORDER BY calls DESC; "
      + "(3) error lines per tool result: SELECT m.seq, m.name, count(*) AS errors FROM lines_fts JOIN lines l ON l.id = lines_fts.rowid "
      + "JOIN messages m ON m.seq = l.message_seq WHERE lines_fts MATCH 'error' AND m.kind = 'tool_result' GROUP BY m.seq ORDER BY errors DESC LIMIT 10. "
      + `Writes, ATTACH and PRAGMA are rejected; the query stops after ${SQL_TIMEOUT_MS / 1000} s; at most ${SQL_ROW_CAP} rows return `
      + `(truncated: true when there were more; long text cells are cut at ${SQL_CELL_MAX_CHARS} characters).`,
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    inputSchema: {
      type: "object",
      properties: {
        session_id: { type: "string", description: "Session id from the [smarter-compact: continuing session …] pointer. Pass \"current\" when you have no pointer: the newest session of this workspace answers" },
        sql: { type: "string", description: `One SELECT or WITH statement, at most ${SQL_MAX_CHARS} characters` },
      },
      required: ["session_id", "sql"],
      additionalProperties: false,
    },
  },
  {
    name: "context_status",
    title: "Context management health",
    description:
      "Summarize context management over recent sessions from the session store: trims, estimated tokens saved, "
      + "fail-opens with their error type and location, the proxy code version (stale when it differs from the "
      + "checkout), and provider-reported prompt-cache usage (hit ratio, cache-creation tokens on requests that made a "
      + "new cut vs others, cache_control breakpoint counts). A fail-open means the request still succeeded but was sent untrimmed. Use this instead of docker "
      + "logs; per-session detail is in smart_compact_sql's events table.",
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
    inputSchema: {
      type: "object",
      properties: {
        hours: { type: "number", description: `Window in hours, 1-${STATUS_MAX_HOURS} (default ${EVENT_WINDOW_HOURS})` },
      },
      additionalProperties: false,
    },
  },
];

export const CONTEXT_STORE_TOOL_NAMES = new Set(CONTEXT_STORE_MCP_TOOLS.map((t) => t.name));

/** `$SMARTER_COMPACT_STATE_DIR/context` (trimmed), read at call time; same resolver as the hooks. */
export function contextRoot(env = process.env) {
  return hookContextRoot(env.HOME || os.homedir(), env);
}

/** Hook-side sanitization (docs/context-store.md). An explicit search id containing a path separator, or
 *  equal to `.`/`..`, is rejected: pointers hand the agent an already-sanitized id, which never has one. */
export function sanitizeSessionId(raw) {
  const text = String(raw ?? "");
  if (/[/\\]/.test(text) || /^\s*\.{1,2}\s*$/.test(text)) throw new Error(`invalid session_id: ${JSON.stringify(text)}`);
  let sid = "";
  try {
    sid = hookSanitizeSessionId(text);
  } catch {
    // empty after trimming
  }
  if (!sid) throw new Error(`invalid session_id: ${JSON.stringify(text)}`);
  return sid;
}

/**
 * Store settings (storeSettings) with config.toml `[context_store]` under the environment, which wins.
 * config.toml is read lazily; an unreadable one leaves the defaults.
 */
export async function contextStoreSettings(env = process.env, { home = env.HOME || os.homedir(), stackRoot = PKG_ROOT } = {}) {
  let fromConfig = {};
  try {
    const { contextTrimEnvFromPmConfig, loadPmConfig } = await import("../../wizard/lib/pm-config.mjs");
    fromConfig = contextTrimEnvFromPmConfig(loadPmConfig(home, stackRoot).merged);
  } catch {
    // defaults
  }
  return storeSettings({ ...fromConfig, ...env });
}

/** MCP server start: TTL cleanup of old session files, at most once a day. Never throws; returns deleted sids. */
export async function startContextStore(env = process.env, opts = {}) {
  try {
    const settings = await contextStoreSettings(env, opts);
    return maybeSweepContextStore(contextRoot(env), settings.ttlDays, opts.now);
  } catch (err) {
    process.stderr.write(`smarter-compact-mcp: context store cleanup failed: ${err instanceof Error ? err.message : err}\n`);
    return [];
  }
}

function isGitWorkTreeRoot(dir) {
  const entry = path.join(dir, ".git");
  if (!fs.existsSync(entry)) return false;
  try {
    const st = fs.statSync(entry);
    if (st.isDirectory()) return fs.existsSync(path.join(entry, "HEAD"));
    if (st.isFile()) return /^gitdir:\s*\S+/m.test(fs.readFileSync(entry, "utf8"));
  } catch {
    return false;
  }
  return false;
}

function gitRoot(dir) {
  let candidate = path.resolve(dir);
  while (true) {
    if (isGitWorkTreeRoot(candidate)) return candidate;
    const parent = path.dirname(candidate);
    if (parent === candidate) return null;
    candidate = parent;
  }
}

/** The repository's shared git dir: linked worktrees of one repo resolve to the same path. */
function repoIdentity(root) {
  if (!root) return null;
  const entry = path.join(root, ".git");
  try {
    if (fs.statSync(entry).isDirectory()) return fs.realpathSync(entry);
    const match = /^gitdir:\s*(.+)\s*$/m.exec(fs.readFileSync(entry, "utf8"));
    if (!match) return null;
    const gitDir = path.resolve(root, match[1].trim());
    const common = path.join(gitDir, "commondir");
    const shared = fs.existsSync(common) ? path.resolve(gitDir, fs.readFileSync(common, "utf8").trim()) : gitDir;
    return fs.realpathSync(shared);
  } catch {
    return null;
  }
}

function readJsonFile(file) {
  try {
    const value = JSON.parse(fs.readFileSync(file, "utf8"));
    return value && typeof value === "object" && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

function within(child, parent) {
  return child === parent || child.startsWith(parent.endsWith(path.sep) ? parent : `${parent}${path.sep}`);
}

/** Registered sessions whose cwd lies in the workspace git root, equals cwd, or belongs to a linked
 *  worktree of the same repository, newest first. */
function workspaceSessions(sessionsDir, cwd) {
  let names;
  try {
    names = fs.readdirSync(sessionsDir);
  } catch {
    return [];
  }
  const here = path.resolve(cwd);
  const root = gitRoot(here);
  const repo = repoIdentity(root);
  const found = [];
  for (const file of names) {
    if (!file.endsWith(".json") || file.endsWith(".trim.json")) continue;
    const reg = readJsonFile(path.join(sessionsDir, file));
    if (!reg || typeof reg.cwd !== "string" || !path.isAbsolute(reg.cwd)) continue;
    const regCwd = path.resolve(reg.cwd);
    const sameRepo = repo && repoIdentity(gitRoot(regCwd)) === repo;
    if (!(regCwd === here || (root && within(regCwd, root)) || sameRepo)) continue;
    const at = Date.parse(reg.registered_at);
    found.push({ sid: file.slice(0, -".json".length), at: Number.isFinite(at) ? at : 0 });
  }
  return found.sort((a, b) => b.at - a.at).slice(0, MAX_SESSIONS).map((s) => s.sid);
}

/** What an agent writes for "this session" when nothing ever handed it an id: the recall tools are offered
 *  from the first request, but the id arrives only with a trim pointer. */
const SESSION_PLACEHOLDERS = new Set(["current", "this", "these", "latest", "self", "default", "now", "recent"]);

/**
 * Which session databases a recall call reads, and why.
 *
 * A call that carries a pointer's id is the normal path. A call whose id names no store must not answer
 * with an empty success: the agent asked a real question, and `total_matches: 0` from a store that never
 * existed reads as "this is not in your history" and sends it re-deriving what it already had. So a
 * placeholder, or any id in a workspace that has exactly one session, resolves to that workspace's
 * sessions; a plausible id that is wrong where several sessions exist fails, naming the ids that do exist.
 * `sessions_searched` always reports what was actually read, so a substitution is never hidden.
 */
function targetSessions(args, sessionsDir, cwd) {
  if (!given(args.session_id)) return { sessions: workspaceSessions(sessionsDir, cwd) };
  const sid = sanitizeSessionId(args.session_id);
  if (fs.existsSync(path.join(sessionsDir, `${sid}${DB_SUFFIX}`))) return { sessions: [sid] };
  const here = workspaceSessions(sessionsDir, cwd);
  if (!here.length) {
    return { sessions: [], note: `no session store for ${JSON.stringify(sid)} and no session registered for this workspace` };
  }
  if (SESSION_PLACEHOLDERS.has(String(args.session_id).trim().toLowerCase()) || here.length === 1) {
    return {
      sessions: here,
      note: `no session store for ${JSON.stringify(sid)}; searched this workspace's ${here.length === 1 ? "session" : `${here.length} sessions`} instead`,
    };
  }
  throw new Error(`no session database for ${JSON.stringify(sid)}; this workspace has ${here.length}: `
    + `${here.join(", ")} — pass one of those, or omit session_id to search all of them`);
}

/** Read-only connection to a session database, or null when it is missing or of a newer schema version (a v1
 * database, not yet migrated by a writer, reads the same except that it has no events table). */
function openReadOnly(file) {
  if (!fs.existsSync(file)) return null;
  const { DatabaseSync } = sqlite();
  const db = new DatabaseSync(file, { readOnly: true });
  try {
    db.exec("PRAGMA busy_timeout = 1000");
    const version = db.prepare("PRAGMA user_version").get().user_version;
    if (version < 1 || version > SCHEMA_VERSION) {
      db.close();
      return null;
    }
  } catch (err) {
    db.close();
    throw err;
  }
  return db;
}

function excerpt(text, at) {
  if (text.length <= EXCERPT_CHARS) return text;
  const start = Math.max(0, Math.min(at - Math.floor(EXCERPT_CHARS / 4), text.length - EXCERPT_CHARS));
  const end = start + EXCERPT_CHARS;
  return `${start > 0 ? "…" : ""}${text.slice(start, end)}${end < text.length ? "…" : ""}`;
}

function given(value) {
  return value !== undefined && value !== null && String(value).trim() !== "";
}

function compilePattern(args) {
  const source = String(args.pattern);
  if (source.length > PATTERN_MAX_CHARS) throw new Error(`pattern must be at most ${PATTERN_MAX_CHARS} characters`);
  try {
    return new RegExp(source, args.ignore_case === true ? "i" : "");
  } catch (err) {
    throw new Error(`invalid pattern: ${err instanceof Error ? err.message : String(err)}`);
  }
}

function contextCount(raw) {
  if (raw === undefined || raw === null) return 0;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < 0 || n > CONTEXT_MAX) throw new Error(`context must be an integer from 0 to ${CONTEXT_MAX}`);
  return n;
}

function roleFilter(raw) {
  if (raw === undefined || raw === null) return DEFAULT_ROLES;
  const list = (Array.isArray(raw) ? raw : [raw]).map((r) => String(r).trim().toLowerCase());
  if (list.includes("all")) return ROLES;
  const bad = list.filter((r) => !ROLES.includes(r));
  if (bad.length || !list.length) throw new Error(`role must be "user", "assistant", "tool", a list of them, or "all"`);
  return [...new Set(list)];
}

function inContextFilter(raw) {
  if (raw === undefined || raw === null) return null;
  if (typeof raw !== "boolean") throw new Error("in_context must be true or false");
  return raw ? 1 : 0;
}

/** WHERE clause over messages `m` (and optionally lines `l` for the role) for the role and in_context filters. */
function filterSql(roles, inContext, roleColumn = "m.role") {
  const parts = [`${roleColumn} IN (${roles.map(() => "?").join(", ")})`];
  const params = [...roles];
  if (inContext !== null) {
    parts.push("m.in_context = ?");
    params.push(inContext);
  }
  return { where: parts.join(" AND "), params };
}

/** Query words as FTS5 strings; a word without letters or digits matches nothing and is dropped. */
function ftsTerms(query) {
  const words = [...new Set(query.toLowerCase().split(/\s+/).filter(Boolean))];
  return words.filter((w) => /[\p{L}\p{N}]/u.test(w)).map((w) => ({ word: w, fts: `"${w.replaceAll('"', '""')}"` }));
}

/**
 * Regex hits as [rowIndex, offset] pairs over the first PATTERN_ROW_CHARS of each text, and matching lines
 * (lines[i]: the lines-table rows of texts[i]) added to counter.lines. Runs in a vm context so V8 interrupts catastrophic backtracking at the deadline;
 * hits and lines counted before the deadline are kept. Returns whether the deadline cut the scan short.
 */
function patternScan(re, texts, lines, deadline, out, counter) {
  const timeout = Math.floor(deadline - Date.now());
  if (timeout < 1) return true;
  const cut = (t) => t.slice(0, PATTERN_ROW_CHARS);
  const sandbox = { re, texts: texts.map(cut), lines: lines.map((group) => group.map(cut)), out, counter };
  try {
    vm.runInNewContext(
      // Row by row, its lines with it, so a deadline keeps the counts of every row scanned before it.
      "for (let i = 0; i < texts.length; i++) { const m = re.exec(texts[i]); if (m) out.push([i, m.index]); "
        + "for (const line of lines[i]) if (re.test(line)) counter.lines++; }",
      sandbox,
      { timeout },
    );
    return false;
  } catch (err) {
    if (err?.code === "ERR_SCRIPT_EXECUTION_TIMEOUT") return true;
    throw err;
  }
}

function rowSummary(row, text) {
  return { seq: row.seq ?? null, role: row.role ?? null, kind: row.kind ?? null, name: row.name ?? null, in_context: row.in_context === 1, text };
}

const ROW_COLUMNS = "m.seq AS seq, m.thread AS thread, m.role AS role, m.kind AS kind, m.name AS name, m.text AS text, "
  + "m.blob_id AS blob_id, m.in_context AS in_context";

/** Lexical hits of one session: [{seq, matched, score, first}] and the exact line count. */
function lexicalSession(db, terms, filter) {
  const anyExpr = terms.map((t) => t.fts).join(" OR ");
  const allExpr = terms.map((t) => t.fts).join(" AND ");
  const hits = db.prepare(`SELECT m.seq AS seq, bm25(messages_fts) AS score FROM messages_fts JOIN messages m ON m.seq = messages_fts.rowid `
    + `WHERE messages_fts MATCH ? AND ${filter.where}`).all(anyExpr, ...filter.params);
  const matched = new Map(hits.map((h) => [h.seq, 0]));
  if (terms.length > 1) {
    const one = db.prepare("SELECT rowid AS seq FROM messages_fts WHERE messages_fts MATCH ?");
    for (const t of terms) for (const r of one.all(t.fts)) if (matched.has(r.seq)) matched.set(r.seq, matched.get(r.seq) + 1);
  } else for (const h of hits) matched.set(h.seq, 1);
  const lines = db.prepare(`SELECT count(*) AS n FROM lines_fts JOIN lines l ON l.id = lines_fts.rowid JOIN messages m ON m.seq = l.message_seq `
    + `WHERE lines_fts MATCH ? AND ${filterSql(filter.roles, filter.inContext, "l.role").where}`)
    .get(allExpr, ...filterSql(filter.roles, filter.inContext, "l.role").params).n;
  return { hits: hits.map((h) => ({ seq: h.seq, score: h.score, matched: matched.get(h.seq) })), lines };
}

function vectorModel(db, preferred) {
  const models = db.prepare("SELECT model, count(*) AS n FROM vectors WHERE dim > 0 GROUP BY model ORDER BY n DESC, model").all();
  if (!models.length) return null;
  return models.find((m) => m.model === preferred)?.model ?? models[0].model;
}

function firstTermAt(text, terms) {
  const lower = text.toLowerCase();
  let first = Infinity;
  for (const t of terms) {
    const i = lower.indexOf(t.word);
    if (i >= 0) first = Math.min(first, i);
  }
  return Number.isFinite(first) ? first : 0;
}

async function search(args, env, cwd, opts) {
  const hasQuery = given(args.query);
  const hasPattern = args.pattern !== undefined && args.pattern !== null && String(args.pattern) !== "";
  if (hasQuery === hasPattern) throw new Error("pass exactly one of query or pattern");
  const re = hasPattern ? compilePattern(args) : null;
  const query = hasQuery ? String(args.query).trim() : null;
  const terms = hasQuery ? ftsTerms(query) : [];
  if (hasQuery && !terms.length) throw new Error("query has no searchable words (letters or digits); use pattern for punctuation");
  const context = contextCount(args.context);
  const limit = Number(args.limit) > 0 ? Math.floor(Number(args.limit)) : DEFAULT_LIMIT;
  const roles = roleFilter(args.role);
  const inContext = inContextFilter(args.in_context);
  if (args.mode !== undefined && args.mode !== "hybrid" && args.mode !== "lexical") throw new Error('mode must be "hybrid" or "lexical"');
  const filter = { ...filterSql(roles, inContext), roles, inContext };
  const sessionsDir = path.join(contextRoot(env), "sessions");
  const target = targetSessions(args, sessionsDir, cwd);
  const sessions = target.sessions;

  const deadline = Date.now() + PATTERN_BUDGET_MS;
  let timedOut = false;
  let lineMatches = 0;
  const databases = {};
  const open = new Map();
  const lexical = [];
  const vectorDbs = [];
  try {
    for (const [sessionRank, sid] of sessions.entries()) {
      const file = path.join(sessionsDir, `${sid}${DB_SUFFIX}`);
      const db = openReadOnly(file);
      if (!db) continue;
      open.set(sid, db);
      databases[sid] = file;
      if (re) {
        if (timedOut) continue;
        const rows = db.prepare(`SELECT ${ROW_COLUMNS} FROM messages m WHERE ${filter.where} ORDER BY m.seq`).all(...filter.params);
        const lineFilter = filterSql(roles, inContext, "l.role");
        const bySeq = new Map(rows.map((r) => [r.seq, []]));
        for (const l of db.prepare(`SELECT l.message_seq AS seq, l.text AS text FROM lines l JOIN messages m ON m.seq = l.message_seq `
          + `WHERE ${lineFilter.where} ORDER BY l.message_seq, l.line_no`).all(...lineFilter.params)) bySeq.get(l.seq)?.push(l.text);
        const lines = rows.map((r) => bySeq.get(r.seq));
        const found = [];
        const counter = { lines: 0 };
        timedOut = patternScan(re, rows.map((r) => r.text), lines, deadline, found, counter);
        lineMatches += counter.lines;
        for (const [index, at] of found) lexical.push({ sid, sessionRank, seq: rows[index].seq, matched: 1, score: 0, first: at });
        continue;
      }
      const result = lexicalSession(db, terms, filter);
      lineMatches += result.lines;
      for (const h of result.hits) lexical.push({ sid, sessionRank, ...h });
      vectorDbs.push({ sid, sessionRank, db });
    }
    // Lexical rank: more query words first, then BM25 (lower is better), newer session, later row.
    lexical.sort((a, b) => b.matched - a.matched || a.score - b.score || a.sessionRank - b.sessionRank || b.seq - a.seq);

    const settings = re ? null : opts.settings || await contextStoreSettings(env, opts);
    const withVectors = re ? [] : vectorDbs.map((v) => ({ ...v, model: vectorModel(v.db, settings.embeddingModel) })).filter((v) => v.model);
    // Background embedding progress of the searched sessions: rows with a vector, still waiting, skipped.
    let vectorStatus;
    for (const v of re ? [] : vectorDbs) {
      const model = vectorModel(v.db, settings.embeddingModel) ?? settings.embeddingModel;
      if (!model) continue;
      const counts = vectorCounts(v.db, model);
      vectorStatus ??= { filled: 0, pending: 0, skipped: 0 };
      for (const k of Object.keys(vectorStatus)) vectorStatus[k] += counts[k];
    }
    let mode = "lexical";
    let embeddingError;
    let ranked = lexical.map((h) => ({ ...h, key: `${h.sid}\u0000${h.seq}` }));
    if (!re && args.mode !== "lexical" && withVectors.length) {
      try {
        const vectorHits = [];
        const byModel = new Map();
        for (const v of withVectors) {
          if (!byModel.has(v.model)) {
            const config = { model: v.model, url: settings.embeddingUrl, apiKey: settings.embeddingApiKey };
            byModel.set(v.model, (await fetchEmbeddings(config, [query], opts.fetch))[0]);
          }
          for (const [seq, similarity] of nearest(v.db, byModel.get(v.model), v.model, VECTOR_CANDIDATES, filter.where, filter.params)) {
            vectorHits.push({ sid: v.sid, sessionRank: v.sessionRank, seq, similarity });
          }
        }
        vectorHits.sort((a, b) => b.similarity - a.similarity || a.sessionRank - b.sessionRank || b.seq - a.seq);
        const fused = new Map();
        const add = (h, rank, extra) => {
          const key = `${h.sid}\u0000${h.seq}`;
          const entry = fused.get(key) || { sid: h.sid, sessionRank: h.sessionRank, seq: h.seq, matched: 0, rrf: 0, key };
          entry.rrf += 1 / (RRF_K + rank + 1);
          Object.assign(entry, extra);
          fused.set(key, entry);
        };
        lexical.forEach((h, rank) => add(h, rank, { matched: h.matched, first: h.first }));
        vectorHits.slice(0, VECTOR_CANDIDATES).forEach((h, rank) => add(h, rank, { similarity: Math.round(h.similarity * 1e4) / 1e4 }));
        ranked = [...fused.values()].sort((a, b) => b.rrf - a.rrf || a.sessionRank - b.sessionRank || b.seq - a.seq);
        mode = "hybrid";
      } catch (err) {
        embeddingError = err instanceof Error ? err.message : String(err);
      }
    }

    const envelope = {
      ...(re ? { pattern: String(args.pattern) } : { query }),
      mode,
      roles,
      ...(inContext === null ? {} : { in_context: inContext === 1 }),
      sessions_searched: sessions,
      ...(target.note ? { session_id_note: target.note } : {}),
      databases,
      total_matches: lexical.length,
      line_matches: lineMatches,
      ...(vectorStatus ? { vectors: vectorStatus } : {}),
      ...(embeddingError ? { embedding_error: embeddingError } : {}),
    };
    const overhead = JSON.stringify(envelope, null, 2).length;
    const results = [];
    let truncated = timedOut || ranked.length > limit;
    for (const h of ranked.slice(0, limit)) {
      const db = open.get(h.sid);
      const row = db.prepare(`SELECT ${ROW_COLUMNS} FROM messages m WHERE m.seq = ?`).get(h.seq);
      const at = h.first ?? (re ? 0 : firstTermAt(row.text, terms));
      const neighbour = (r) => rowSummary(r, excerpt(r.text, 0));
      const item = {
        session_id: h.sid,
        ...rowSummary(row, excerpt(row.text, at)),
        ...(re ? {} : { matched_terms: h.matched ?? 0 }),
        ...(h.similarity !== undefined ? { similarity: h.similarity } : {}),
        ...(row.blob_id ? { blob_ids: [row.blob_id] } : {}),
        ...(context
          ? {
              before: db.prepare(`SELECT ${ROW_COLUMNS} FROM messages m WHERE m.thread = ? AND m.seq < ? ORDER BY m.seq DESC LIMIT ?`)
                .all(row.thread, row.seq, context).reverse().map(neighbour),
              after: db.prepare(`SELECT ${ROW_COLUMNS} FROM messages m WHERE m.thread = ? AND m.seq > ? ORDER BY m.seq LIMIT ?`)
                .all(row.thread, row.seq, context).map(neighbour),
            }
          : {}),
      };
      if (overhead + JSON.stringify([...results, item], null, 2).length > RESULT_MAX_CHARS) {
        truncated = true;
        break;
      }
      results.push(item);
    }
    return { ...envelope, results, truncated, ...(timedOut ? { timed_out: true } : {}) };
  } finally {
    for (const db of open.values()) db.close();
  }
}

/**
 * Reject anything but one SELECT / WITH statement: comments, string literals and quoted identifiers are
 * blanked, then the first word must be SELECT or WITH, no `;` may precede trailing whitespace, and no
 * write, ATTACH, PRAGMA or transaction keyword may remain. The read-only connection is the second line of defence.
 */
export function checkReadOnlySql(sql) {
  const text = String(sql ?? "");
  if (!text.trim()) throw new Error("sql is required");
  if (text.length > SQL_MAX_CHARS) throw new Error(`sql must be at most ${SQL_MAX_CHARS} characters`);
  const blanked = text
    .replace(/--[^\n]*|\/\*[\s\S]*?(\*\/|$)/g, " ")
    .replace(/'(?:[^']|'')*'?|"(?:[^"]|"")*"?|`(?:[^`]|``)*`?|\[[^\]]*\]?/g, " '' ");
  const statement = blanked.replace(/;\s*$/, "");
  if (statement.includes(";")) throw new Error("smart_compact_sql runs one statement; remove the extra ';'");
  const words = statement.toUpperCase().match(/[A-Z_][A-Z0-9_$]*/g) || [];
  if (words[0] !== "SELECT" && words[0] !== "WITH") throw new Error("smart_compact_sql accepts only SELECT or WITH … SELECT");
  // replace() is a function; REPLACE INTO is a write.
  const bad = words.find((w) => SQL_FORBIDDEN.has(w)) || (/\bREPLACE\s+INTO\b/i.test(statement) ? "REPLACE INTO" : null);
  if (bad) throw new Error(`smart_compact_sql is read-only: ${bad} is not allowed`);
  return text.trim().replace(/;\s*$/, "");
}

function runSql(args, env, cwd) {
  if (!given(args.session_id)) throw new Error("session_id is required");
  const sessionsDir = path.join(contextRoot(env), "sessions");
  const sql = checkReadOnlySql(args.sql);
  const target = targetSessions(args, sessionsDir, cwd);
  // One statement runs against one database: the newest session the id resolved to.
  const sid = target.sessions[0];
  if (!sid) throw new Error(target.note || "no session database to query");
  const note = target.note
    ? (target.sessions.length > 1 ? `${target.note}, and the query ran against ${sid}` : target.note)
    : null;
  const file = path.join(sessionsDir, `${sid}${DB_SUFFIX}`);
  const child = spawnSync(process.execPath, ["--no-warnings", SQL_RUNNER], {
    input: JSON.stringify({ file, sql, cap: SQL_ROW_CAP, cellMax: SQL_CELL_MAX_CHARS }),
    encoding: "utf8",
    timeout: SQL_TIMEOUT_MS,
    killSignal: "SIGKILL",
    maxBuffer: 64 * 1024 * 1024,
  });
  if (child.error?.code === "ETIMEDOUT" || child.signal === "SIGKILL") {
    throw new Error(`query stopped after ${SQL_TIMEOUT_MS / 1000} s; narrow it (LIMIT, FTS MATCH) and retry`);
  }
  if (child.error) throw child.error;
  let out;
  try {
    out = JSON.parse(child.stdout);
  } catch {
    throw new Error(`query failed: ${String(child.stderr || "no output").trim().split("\n").at(-1)}`);
  }
  if (out.error) throw new Error(`query failed: ${out.error}`);
  const result = { session_id: sid, ...(note ? { session_id_note: note } : {}), columns: out.columns, rows: out.rows, row_count: out.rows.length, truncated: out.truncated };
  while (result.rows.length && JSON.stringify(result).length > SQL_RESULT_MAX_CHARS) {
    result.rows = result.rows.slice(0, Math.floor(result.rows.length / 2));
    result.row_count = result.rows.length;
    result.truncated = true;
  }
  return result;
}

function getBlob(args, env) {
  const id = String(args.id ?? "").trim();
  if (!BLOB_ID_RE.test(id)) throw new Error("id must be exactly 16 lowercase hex characters");
  const offset = Number(args.offset) > 0 ? Math.floor(Number(args.offset)) : 0;
  let text;
  try {
    text = fs.readFileSync(path.join(contextRoot(env), "blobs", `${id}.txt`), "utf8");
  } catch {
    return { id, found: false, text: "" };
  }
  const chunk = text.slice(offset, offset + BLOB_MAX_CHARS);
  const end = offset + chunk.length;
  return {
    id,
    found: true,
    total_chars: text.length,
    offset,
    text: chunk,
    truncated: end < text.length,
    ...(end < text.length ? { next_offset: end } : {}),
  };
}

function contextStatusTool(args, env) {
  const hours = given(args.hours) ? Number(args.hours) : EVENT_WINDOW_HOURS;
  if (!Number.isFinite(hours) || hours < 1 || hours > STATUS_MAX_HOURS) throw new Error(`hours must be 1-${STATUS_MAX_HOURS}`);
  let checkout = null;
  try { checkout = proxyCodeVersion(path.join(PKG_ROOT, "proxy", "smarter_compact")); } catch { /* installed runtime has no proxy/ */ }
  const events = summarizeEvents(path.join(contextRoot(env), "sessions"), { hours });
  const running = events.latest.proxy || null;
  return {
    ...events,
    checkout_proxy_version: checkout,
    proxy_stale: running && checkout ? running !== checkout : null,
    not_recorded: "fail-opens before the session id is known or from the session database itself (proxy stderr only)",
  };
}

/**
 * @param {string} name
 * @param {Record<string, unknown>} args
 * @param {{ env?: NodeJS.ProcessEnv, cwd?: string, settings?: object, fetch?: typeof fetch, home?: string, stackRoot?: string }} [opts]
 */
export async function callContextStoreTool(name, args = {}, opts = {}) {
  const env = opts.env || process.env;
  let result;
  if (name === "smart_compact_search") result = await search(args, env, opts.cwd || process.cwd(), opts);
  else if (name === "smart_compact_get") result = getBlob(args, env);
  else if (name === "smart_compact_sql") result = runSql(args, env, opts.cwd || process.cwd());
  else if (name === "context_status") return contextStatusTool(args, env);
  else throw new Error(`unknown tool: ${name}`);
  // Telemetry for `smarter-compact context stats`; never affects the tool result.
  if (name === "smart_compact_search") {
    recordToolCall(contextRoot(env), { tool: name, hits: result.total_matches ?? result.results?.length ?? 0,
      sessions: result.sessions_searched?.length ?? 0, mode: result.mode });
  } else if (name === "smart_compact_sql") {
    recordToolCall(contextRoot(env), { tool: name, session_id: result.session_id, rows: result.row_count ?? 0 });
  } else if (name === "smart_compact_get") {
    recordToolCall(contextRoot(env), { tool: name, id: result.id, found: !!result.found,
      chars: result.found ? (result.text?.length ?? 0) : 0 });
  }
  return result;
}
