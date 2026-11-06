import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { chain, dropSession, openSession, summarizeEvents } from "../plugin/lib/session-db.mjs";

const TS = "2026-11-05T00:00:00Z";

/** Rows of a history of `turns` turns (question, tool call, tool result, "ok"), as storeMessages builds them. */
function history(turns, results = (t) => `out ${t}`) {
  const rows = [];
  for (let t = 0; t < turns; t++) {
    rows.push({ role: "user", kind: "text", name: null, text: `question ${t}` });
    rows.push({ role: "assistant", kind: "tool_use", name: "Bash", text: `{"command":"echo ${t}"}` });
    rows.push({ role: "tool", kind: "tool_result", name: "Bash", text: results(t) });
    rows.push({ role: "assistant", kind: "text", name: null, text: "ok" });
  }
  return rows;
}

const store = (rows) => chain(rows.map((r) => ({ ...r, blob_id: null, keep: true })));

function dbFile() {
  return path.join(fs.mkdtempSync(path.join(os.tmpdir(), "sc-session-db-")), "s.db");
}

const texts = (file, sql) => openSession(file).db.prepare(sql).all().map((r) => r.text);

test("an edited old message re-inserts only that message", () => {
  const file = dbFile();
  const rows = history(3);
  assert.equal(openSession(file).sync(store(rows), null, TS), 12);
  rows[4] = { ...rows[4], text: "question 1 reworded" };
  assert.equal(openSession(file).sync(store(rows), null, TS), 1);
  assert.deepEqual(texts(file, "SELECT text FROM messages WHERE seq > 12"), ["question 1 reworded"]);
  // A cold index, rebuilt from the stored rows, keys the same way.
  dropSession(file);
  rows[8] = { ...rows[8], text: "question 2 reworded" };
  assert.equal(openSession(file).sync(store(rows), null, TS), 1);
  assert.deepEqual(texts(file, "SELECT text FROM messages WHERE text = 'out 2'"), ["out 2"]);
  dropSession(file);
});

test("repeated identical messages are both stored", () => {
  const file = dbFile();
  const rows = history(2, () => "same");
  assert.equal(openSession(file).sync(store(rows), null, TS), 8);
  assert.equal(texts(file, "SELECT text FROM messages WHERE text = 'same'").length, 2);
  rows[4] = { ...rows[4], text: "question 1 reworded" };
  rows.push({ role: "tool", kind: "tool_result", name: "Bash", text: "same" });
  assert.equal(openSession(file).sync(store(rows), null, TS), 2);
  assert.equal(texts(file, "SELECT text FROM messages WHERE text = 'same'").length, 3);
  assert.equal(texts(file, "SELECT text FROM messages WHERE text = 'ok'").length, 2);
  dropSession(file);
});

test("a trim's saving is its estimate minus the billed prompt of the request that made the cut", () => {
  const file = dbFile();
  const s = openSession(file);
  const usage = (trimmed, read) => ({ trimmed, input_tokens: 2, cache_read_tokens: read, cache_creation_tokens: 0 });
  s.addEvent("usage", TS, "proxy:t", usage(0, 74_000));
  s.addEvent("trim", TS, "proxy:t", { tokens_before: 98_000, tokens_after: 27_000, dropped: 3 });
  s.addEvent("usage", TS, "proxy:t", usage(0, 91_000)); // an in-flight request that went up whole
  s.addEvent("usage", TS, "proxy:t", usage(2, 31_000));
  s.addEvent("trim", TS, "proxy:t", { tokens_before: 96_000, tokens_after: 20_000, dropped: 2 }); // never billed
  dropSession(file);
  const out = summarizeEvents(path.dirname(file), { now: Date.parse(TS) });
  assert.deepEqual([out.trims, out.tokensBefore, out.tokensAfter, out.tokensSaved, out.unmeasured],
    [2, 98_000, 31_002, 66_998, 1]);
});
