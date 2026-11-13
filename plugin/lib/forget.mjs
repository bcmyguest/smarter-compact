/**
 * Forget a string (a pasted key, a password, anything the user wants gone) from the context store: every occurrence
 * in a session's rows, their line index, both full-text indexes and vectors, the session's scratchpad, and every blob
 * file is replaced with FORGOTTEN. Whole-session deletion stays the TTL's job (docs/context-store.md, "Retention").
 *
 * A redacted row keeps its seq and chained fingerprint, and its original dedup content key goes to the `forgotten`
 * table: a request that still carries the original text (it may still be in the agent's live context) then matches
 * the stored row instead of storing the text again. The store cannot reach copies outside it (the agent's own
 * transcript files, the live context, provider logs).
 */
import fs from "node:fs";
import path from "node:path";
import { connect, contentKey, DB_SUFFIX } from "./session-db.mjs";

export const FORGOTTEN = "[forgotten]";
export const FORGET_MIN_CHARS = 4;

const nowIso = () => new Date().toISOString().replace(/\.\d+Z$/, "Z");

/** Replace every occurrence of text in one session database; returns the rows changed. */
export function forgetInSession(file, text, { at = nowIso() } = {}) {
  const db = connect(file);
  try {
    // Freed pages (old row text, old FTS segments) are zeroed rather than left in the file.
    db.exec("PRAGMA secure_delete = ON");
    db.exec("BEGIN IMMEDIATE");
    let changed = 0;
    try {
      const rows = db.prepare("SELECT m.seq, m.role, m.kind, m.name, m.text, f.content_key AS forgotten_key FROM messages m "
        + "LEFT JOIN forgotten f ON f.message_seq = m.seq WHERE instr(m.text, ?) > 0").all(text);
      const ftsDelete = db.prepare("INSERT INTO messages_fts (messages_fts, rowid, text) VALUES ('delete', ?, ?)");
      const ftsInsert = db.prepare("INSERT INTO messages_fts (rowid, text) VALUES (?, ?)");
      const update = db.prepare("UPDATE messages SET text = ? WHERE seq = ?");
      const lines = db.prepare("SELECT id, text FROM lines WHERE message_seq = ?");
      const lineFtsDelete = db.prepare("INSERT INTO lines_fts (lines_fts, rowid, text) VALUES ('delete', ?, ?)");
      const lineDelete = db.prepare("DELETE FROM lines WHERE message_seq = ?");
      const lineInsert = db.prepare("INSERT INTO lines (message_seq, line_no, role, text) VALUES (?, ?, ?, ?)");
      const vectors = db.prepare("DELETE FROM vectors WHERE message_seq = ?");
      const remember = db.prepare("INSERT OR IGNORE INTO forgotten (message_seq, content_key, at) VALUES (?, ?, ?)");
      for (const r of rows) {
        // The first forget records the key of the text the writers stored; later ones keep it.
        remember.run(r.seq, r.forgotten_key || contentKey(r), at);
        const next = r.text.split(text).join(FORGOTTEN);
        ftsDelete.run(r.seq, r.text);
        update.run(next, r.seq);
        ftsInsert.run(r.seq, next);
        // Rebuild the row's lines: the string may span several of them.
        for (const l of lines.all(r.seq)) lineFtsDelete.run(l.id, l.text);
        lineDelete.run(r.seq);
        next.split("\n").forEach((line, k) => lineInsert.run(r.seq, k + 1, r.role, line));
        // Re-embedded from the redacted text by the next background run.
        vectors.run(r.seq);
        changed++;
      }
      db.exec("COMMIT");
    } catch (error) {
      db.exec("ROLLBACK");
      throw error;
    }
    if (changed) {
      // Merge FTS segments so the deleted terms leave the index, then move the WAL into the zeroed main file.
      db.exec("INSERT INTO messages_fts (messages_fts) VALUES ('optimize')");
      db.exec("INSERT INTO lines_fts (lines_fts) VALUES ('optimize')");
      db.exec("PRAGMA wal_checkpoint(TRUNCATE)");
    }
    return changed;
  } finally {
    db.close();
  }
}

function redactFile(file, text) {
  let body;
  try {
    body = fs.readFileSync(file, "utf8");
  } catch {
    return false;
  }
  if (!body.includes(text)) return false;
  // Same name: blob ids are content hashes that placeholders already cite; the id now names the redacted text.
  fs.writeFileSync(file, body.split(text).join(FORGOTTEN));
  return true;
}

/**
 * Forget text from the store under root. sessionIds: the sessions to clean, or null for every session. Blob files
 * are shared across sessions (content-addressed), so every blob containing the text is redacted either way.
 * Returns { sessions: [{session_id, rows}], blobs, scratchpads }.
 */
export function forgetText(root, text, { sessionIds = null, at } = {}) {
  if (typeof text !== "string" || text.length < FORGET_MIN_CHARS) {
    throw new Error(`text must be a string of at least ${FORGET_MIN_CHARS} characters`);
  }
  const dir = path.join(root, "sessions");
  let ids = sessionIds;
  if (!ids) {
    try {
      ids = fs.readdirSync(dir).filter((f) => f.endsWith(DB_SUFFIX)).map((f) => f.slice(0, -DB_SUFFIX.length));
    } catch {
      ids = [];
    }
  }
  const sessions = [];
  for (const sid of ids) {
    if (!sid || /[/\\]/.test(sid) || sid === "." || sid === "..") throw new Error(`invalid session id: ${JSON.stringify(sid)}`);
    const file = path.join(dir, `${sid}${DB_SUFFIX}`);
    if (!fs.existsSync(file)) {
      sessions.push({ session_id: sid, rows: 0, found: false });
      continue;
    }
    sessions.push({ session_id: sid, rows: forgetInSession(file, text, { at }) });
  }
  let blobs = 0;
  const blobDir = path.join(root, "blobs");
  let names = [];
  try { names = fs.readdirSync(blobDir); } catch { /* none */ }
  for (const name of names) if (name.endsWith(".txt") && redactFile(path.join(blobDir, name), text)) blobs++;
  let scratchpads = 0;
  for (const sid of ids) if (redactFile(path.join(root, "scratchpad", `${sid}.md`), text)) scratchpads++;
  return { sessions, blobs, scratchpads };
}
