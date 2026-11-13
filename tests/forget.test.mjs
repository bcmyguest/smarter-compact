import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { chain, closeSessions, openSession, packVector } from "../plugin/lib/session-db.mjs";
import { FORGOTTEN, forgetText } from "../plugin/lib/forget.mjs";
import { callContextStoreTool } from "../plugin/lib/mcp/context-store.mjs";
import { runContextCommand } from "../plugin/lib/context-config.mjs";

const TS = "2026-11-13T00:00:00Z";
const SECRET = "sk-live-4f9a2c1d8e7b";

const rows = () => [
  { role: "user", kind: "text", name: null, text: `here is the key\n${SECRET}\nuse it for staging` },
  { role: "assistant", kind: "tool_use", name: "Bash", text: `{"command":"export KEY=${SECRET}"}` },
  { role: "tool", kind: "tool_result", name: "Bash", text: "ok" },
  { role: "assistant", kind: "text", name: null, text: "done" },
];
const store = (rs) => chain(rs.map((r) => ({ ...r, blob_id: null, keep: true })));

function root() {
  const state = fs.mkdtempSync(path.join(os.tmpdir(), "sc-forget-"));
  const ctx = path.join(state, "context");
  fs.mkdirSync(path.join(ctx, "sessions"), { recursive: true });
  fs.mkdirSync(path.join(ctx, "blobs"), { recursive: true });
  fs.mkdirSync(path.join(ctx, "scratchpad"), { recursive: true });
  return { state, ctx };
}

function seed(ctx, sid = "s1") {
  const file = path.join(ctx, "sessions", `${sid}.db`);
  const s = openSession(file);
  assert.equal(s.sync(store(rows()), null, TS), 4);
  s.db.prepare("INSERT INTO vectors (message_seq, model, dim, blob) VALUES (1, 'm', 2, ?)").run(packVector([0.1, 0.2]));
  fs.writeFileSync(path.join(ctx, "blobs", "0123456789abcdef.txt"), `log line with ${SECRET} inside`);
  fs.writeFileSync(path.join(ctx, "blobs", "fedcba9876543210.txt"), "nothing secret here");
  fs.writeFileSync(path.join(ctx, "scratchpad", `${sid}.md`), `key: ${SECRET}`);
  return file;
}

const all = (file, sql, ...p) => openSession(file).db.prepare(sql).all(...p);

test("forget redacts rows, lines, both indexes, vectors, blobs and the scratchpad", () => {
  const { ctx } = root();
  const file = seed(ctx);
  const r = forgetText(ctx, SECRET, { sessionIds: ["s1"] });
  assert.deepEqual(r, { sessions: [{ session_id: "s1", rows: 2 }], blobs: 1, scratchpads: 1 });
  closeSessions();
  for (const { text } of all(file, "SELECT text FROM messages")) assert.equal(text.includes(SECRET), false);
  assert.equal(all(file, "SELECT text FROM messages WHERE seq = 1")[0].text, `here is the key\n${FORGOTTEN}\nuse it for staging`);
  assert.deepEqual(all(file, "SELECT line_no, text FROM lines WHERE message_seq = 1 ORDER BY line_no").map((l) => l.text),
    ["here is the key", FORGOTTEN, "use it for staging"]);
  // The secret's tokens are gone from both full-text indexes; the rest of the row is still searchable.
  assert.equal(all(file, "SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?", '"4f9a2c1d8e7b"').length, 0);
  assert.equal(all(file, "SELECT rowid FROM lines_fts WHERE lines_fts MATCH ?", '"4f9a2c1d8e7b"').length, 0);
  assert.deepEqual(all(file, "SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'staging'").map((x) => x.rowid), [1]);
  assert.equal(all(file, "SELECT count(*) AS n FROM vectors")[0].n, 0);
  assert.equal(all(file, "SELECT count(*) AS n FROM forgotten")[0].n, 2);
  assert.equal(fs.readFileSync(path.join(ctx, "blobs", "0123456789abcdef.txt"), "utf8"), `log line with ${FORGOTTEN} inside`);
  assert.equal(fs.readFileSync(path.join(ctx, "scratchpad", "s1.md"), "utf8"), `key: ${FORGOTTEN}`);
  // Nothing left in the file itself (secure_delete, FTS optimize, WAL checkpoint).
  for (const f of [file, `${file}-wal`]) {
    if (fs.existsSync(f)) assert.equal(fs.readFileSync(f).includes(Buffer.from(SECRET)), false, f);
  }
});

test("a request still carrying the original text does not store it again, after a restart too", () => {
  const { ctx } = root();
  const file = seed(ctx);
  forgetText(ctx, SECRET, { sessionIds: ["s1"] });
  assert.equal(openSession(file).sync(store(rows()), null, TS), 0);
  closeSessions(); // a fresh writer rebuilds its dedup index from the database
  assert.equal(openSession(file).sync(store(rows()), null, TS), 0);
  assert.equal(all(file, "SELECT count(*) AS n FROM messages WHERE instr(text, ?) > 0", SECRET)[0].n, 0);
  // Forgetting again keeps the original key and changes nothing new.
  assert.equal(forgetText(ctx, SECRET, { sessionIds: ["s1"] }).sessions[0].rows, 0);
});

test("without session ids every session is cleaned; bad input is refused", () => {
  const { ctx } = root();
  seed(ctx, "a");
  seed(ctx, "b");
  const r = forgetText(ctx, SECRET);
  assert.deepEqual(r.sessions.map((s) => [s.session_id, s.rows]).sort(), [["a", 2], ["b", 2]]);
  assert.throws(() => forgetText(ctx, "abc"), /at least 4/);
  assert.throws(() => forgetText(ctx, SECRET, { sessionIds: ["../x"] }), /invalid session id/);
});

test("smart_compact_forget reports counts and never echoes the text", async () => {
  const { state, ctx } = root();
  seed(ctx);
  const out = await callContextStoreTool("smart_compact_forget", { text: SECRET, session_id: "s1" },
    { env: { ...process.env, SMARTER_COMPACT_STATE_DIR: state } });
  assert.equal(out.forgotten, true);
  assert.deepEqual([out.rows, out.blobs, out.scratchpads, out.sessions_changed], [2, 1, 1, ["s1"]]);
  assert.equal(JSON.stringify(out).includes(SECRET), false);
});

test("context forget on the command line", () => {
  const { state, ctx } = root();
  seed(ctx);
  const lines = [];
  const log = { log: (l) => lines.push(l), error: (l) => lines.push(`ERR ${l}`) };
  const prev = process.env.SMARTER_COMPACT_STATE_DIR;
  process.env.SMARTER_COMPACT_STATE_DIR = state;
  try {
    assert.equal(runContextCommand(["forget", "--session", "s1", SECRET], { home: os.tmpdir(), log }), 0);
    assert.match(lines[0], /^forgot 2 row\(s\) in 1 of 1 session\(s\), 1 blob\(s\), 1 scratchpad\(s\)$/);
    assert.equal(runContextCommand(["forget"], { home: os.tmpdir(), log }), 2);
  } finally {
    if (prev === undefined) delete process.env.SMARTER_COMPACT_STATE_DIR;
    else process.env.SMARTER_COMPACT_STATE_DIR = prev;
  }
});

test("an empty search says where it looked", async () => {
  const { state, ctx } = root();
  seed(ctx);
  const env = { ...process.env, SMARTER_COMPACT_STATE_DIR: state, CONTEXT_EMBEDDING_MODEL: "" };
  const miss = await callContextStoreTool("smart_compact_search", { session_id: "s1", query: "kubernetes" }, { env });
  assert.equal(miss.total_matches, 0);
  assert.match(miss.empty_result_note, /^No match in session s1, by keyword search only, over user\/tool messages\. Tell the user where you looked/);
  const hit = await callContextStoreTool("smart_compact_search", { session_id: "s1", query: "staging" }, { env });
  assert.equal("empty_result_note" in hit, false);
});
