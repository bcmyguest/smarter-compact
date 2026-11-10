// CONTEXT_COLD_TRIM and CONTEXT_CACHE_TTL_SPLIT in the OpenCode 2 / Pi plugin; same contract as
// proxy/tests/test_cold_trim.py. The plugin sees no cache_control, so the lifetime is CONTEXT_COLD_TRIM_SECONDS and
// the TTL split is a no-op.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { trimContext, trimSettings } from "../plugin/lib/context-trim.mjs";
import { rmHome } from "./rm-home.mjs";

const SID = "sess-1";
const T0 = 1_800_000_000;
const text = (role, t) => ({ role, content: [{ type: "text", text: t }] });

function history(turns) {
  const messages = [];
  for (let i = 0; i < turns; i++) messages.push(text("user", `question ${i} ${"q".repeat(400)}`), text("assistant", `answer ${i} ${"a".repeat(400)}`));
  messages.push(text("user", "now this"));
  return messages;
}

function withHome(fn) {
  return async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-cold-"));
    try { await fn(home); } finally { rmHome(home); }
  };
}

function at(home, seconds, messages, env = {}) {
  const settings = trimSettings({ CONTEXT_TRIM_AT_TOKENS: "40000", CONTEXT_TRIM_TO_TOKENS: "4000", CONTEXT_CHARS_PER_TOKEN: "1",
    CONTEXT_COMPRESS_OVER_TOKENS: "0", CONTEXT_STANDING_INSTRUCTION: "0", CONTEXT_SCRATCHPAD: "0",
    CONTEXT_COLD_TRIM_MIN_GAIN_TOKENS: "0", SMARTER_COMPACT_STATE_DIR: home, ...env }, home);
  return trimContext({ sessionID: SID, messages }, settings, { now: () => new Date((T0 + seconds) * 1000) });
}

function events(home, kind) {
  const file = path.join(home, "context", "sessions", `${SID}.db`);
  if (!fs.existsSync(file)) return [];
  const db = new DatabaseSync(file);
  try { return db.prepare("SELECT * FROM events WHERE kind = ? ORDER BY id").all(kind); } finally { db.close(); }
}

function state(home) {
  const file = path.join(home, "context", "sessions", `${SID}.trim.json`);
  return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
}

const COLD = { CONTEXT_COLD_TRIM: "1" };

test("cold trim settings: defaults and parsing", () => {
  const s = trimSettings({}, os.tmpdir());
  assert.deepEqual([s.coldTrim, s.coldTrimSeconds, s.coldTrimMinGainTokens, s.cacheTtlSplit], [false, 3600, 5000, 0]);
  const t = trimSettings({ CONTEXT_COLD_TRIM: "1", CONTEXT_COLD_TRIM_SECONDS: "600", CONTEXT_COLD_TRIM_MIN_GAIN_TOKENS: "100",
    CONTEXT_CACHE_TTL_SPLIT: "2" }, os.tmpdir());
  assert.deepEqual([t.coldTrim, t.coldTrimSeconds, t.coldTrimMinGainTokens, t.cacheTtlSplit], [true, 600, 100, 2]);
  const u = trimSettings({ CONTEXT_COLD_TRIM_SECONDS: "0", CONTEXT_CACHE_TTL_SPLIT: "-1" }, os.tmpdir());
  assert.deepEqual([u.coldTrimSeconds, u.cacheTtlSplit], [3600, 0]);
});

test("cold trim off gives the old bytes and records no clock", withHome(async (home) => {
  const messages = history(20);
  await at(home, 0, messages);
  assert.equal(await at(home, 100_000, messages), messages);
  assert.equal("last_request" in state(home), false);
  assert.deepEqual(events(home, "trim"), []);
}));

test("cold trim fires only after the configured lifetime, and the cut is sticky", withHome(async (home) => {
  const messages = history(20);
  await at(home, 0, messages, COLD);
  assert.equal(await at(home, 3600, messages, COLD), messages);
  assert.deepEqual(events(home, "trim"), []);
  const cold = await at(home, 3600 + 3601, messages, COLD);
  assert.ok(cold.length < messages.length);
  const [event] = events(home, "trim");
  assert.equal(event.location, "cold");
  assert.ok(event.tokens_after < 4000 && event.tokens_before > 4000);
  const again = await at(home, 3600 + 3611, messages, COLD);
  assert.equal(JSON.stringify(again), JSON.stringify(cold));
  assert.equal(events(home, "trim").length, 1);
  const [entry] = Object.values(state(home).last_request);
  assert.deepEqual(entry, { at: T0 + 3600 + 3611, long: 2 });
}));

test("cold trim honours CONTEXT_COLD_TRIM_SECONDS", withHome(async (home) => {
  const messages = history(20);
  const env = { ...COLD, CONTEXT_COLD_TRIM_SECONDS: "300" };
  await at(home, 0, messages, env);
  await at(home, 300, messages, env);
  assert.deepEqual(events(home, "trim"), []);
  await at(home, 601, messages, env);
  assert.deepEqual(events(home, "trim").map((e) => e.location), ["cold"]);
}));

test("cold trim skips the first request, small prompts and small gains", withHome(async (home) => {
  const messages = history(20);
  await at(home, 0, messages, COLD);
  assert.deepEqual(events(home, "trim"), []);
  await at(home, 100_000, messages, { ...COLD, CONTEXT_TRIM_TO_TOKENS: "39000" });
  await at(home, 200_000, messages, { ...COLD, CONTEXT_COLD_TRIM_MIN_GAIN_TOKENS: "1000000" });
  await at(home, 300_000, messages, { ...COLD, CONTEXT_TRIM_MIN_GAIN_TOKENS: "1000000" });
  assert.deepEqual(events(home, "trim"), []);
  await at(home, 400_000, messages, COLD);
  assert.equal(events(home, "trim").length, 1);
}));

test("a threshold trim is not marked cold", withHome(async (home) => {
  const messages = history(20);
  await at(home, 0, messages, COLD);
  await at(home, 100_000, messages, { ...COLD, CONTEXT_TRIM_AT_TOKENS: "5000" });
  const [event] = events(home, "trim");
  assert.equal(event.location, null);
}));

test("CONTEXT_CACHE_TTL_SPLIT is a no-op in the plugin", withHome(async (home) => {
  const messages = history(3);
  await at(home, 0, messages, { CONTEXT_CACHE_TTL_SPLIT: "1" });
  assert.equal(await at(home, 1000, messages, { CONTEXT_CACHE_TTL_SPLIT: "1" }), messages);
  assert.deepEqual(events(home, "trim"), []);
}));
