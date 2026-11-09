import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { clockText, trimContext, trimSettings } from "../plugin/lib/context-trim.mjs";

// Same contracts as proxy/tests/test_time_budget.py (CONTEXT_TIME_BUDGET_SECONDS).
const SID = "sess-1";
const T0 = 1_000_000_000;
const LS = ["bash", { command: "ls" }, "a.py\nb.py"];
const CAT = ["bash", { command: "cat a.py" }, "print(1)"];
const TEST = ["bash", { command: "pytest -q" }, "1 passed in 0.1s"];

function withHome(fn) {
  return async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-clock-"));
    try { await fn(home); } finally { fs.rmSync(home, { recursive: true, force: true }); }
  };
}

/** The task, then per step one tool call and its result: steps = [[name, input, text]]. */
function turns(steps) {
  const messages = [{ role: "user", content: [{ type: "text", text: "the task" }] }];
  steps.forEach(([name, input, out], k) => {
    messages.push({ role: "assistant", content: [{ type: "tool-call", id: `p${k}`, name, input }] });
    messages.push({ role: "tool", content: [{ type: "tool-result", id: `p${k}`, name, result: { type: "text", value: out } }] });
  });
  return messages;
}

const stateOf = (home) => JSON.parse(fs.readFileSync(path.join(home, "context", "sessions", `${SID}.trim.json`), "utf8"));
const lastResult = (view) => view.at(-1).content.at(-1).result.value;
const clockLines = (view) => view.flatMap((m) => (Array.isArray(m.content) ? m.content : []))
  .flatMap((p) => (p?.type === "tool-result" ? [p.result.value] : p?.type === "text" ? [p.text] : []))
  .flatMap((t) => (typeof t === "string" ? t.split("\n") : [])).filter((t) => t.startsWith("[TIME REMAINING:"));

async function run(home, messages, sec, env = {}) {
  const settings = trimSettings({ CONTEXT_COMPRESS_OVER_TOKENS: "0", CONTEXT_STANDING_INSTRUCTION: "0", CONTEXT_PROGRESS_NOTE: "0",
    CONTEXT_MASK_OBSERVATIONS: "0", CONTEXT_SCRATCHPAD: "0", CONTEXT_CLEAR_KEEP_RESULTS: "0", CONTEXT_STRIP_OLD_REASONING: "0",
    CONTEXT_CHARS_PER_TOKEN: "1", CONTEXT_TRIM_AT_TOKENS: "10000000", CONTEXT_TIME_BUDGET_SECONDS: "1800",
    SMARTER_COMPACT_STATE_DIR: home, ...env }, home);
  return trimContext({ sessionID: SID, messages: structuredClone(messages) }, settings, { now: () => new Date((T0 + sec) * 1000) });
}

test("clockText follows the paper's per-tool clock and matches the proxy", () => {
  assert.equal(clockText(900, 146), "[TIME REMAINING: 12m34s of 15m budget]");
  assert.equal(clockText(1800, 0), "[TIME REMAINING: 30m00s of 30m budget]");
  assert.equal(clockText(930, 0.5), "[TIME REMAINING: 15m29s of 15m30s budget]");
  assert.equal(clockText(900, 5000), "[TIME REMAINING: 0m00s of 15m budget]");
});

test("the setting defaults to off and ignores non-integers", () => {
  assert.equal(trimSettings({}, os.tmpdir()).timeBudgetSeconds, 0);
  assert.equal(trimSettings({ CONTEXT_TIME_BUDGET_SECONDS: "1800" }, os.tmpdir()).timeBudgetSeconds, 1800);
  assert.equal(trimSettings({ CONTEXT_TIME_BUDGET_SECONDS: "auto" }, os.tmpdir()).timeBudgetSeconds, 0);
});

test("off gives the old bytes, and recorded lines are ignored", withHome(async (home) => {
  for (const n of [1, 2]) {
    const msgs = turns([LS, CAT].slice(0, n));
    assert.equal(JSON.stringify(await run(home, msgs, 0, { CONTEXT_TIME_BUDGET_SECONDS: "0" })), JSON.stringify(msgs));
  }
  await run(home, turns([LS]), 0);
  const msgs = turns([LS, CAT]);
  assert.equal(JSON.stringify(await run(home, msgs, 5, { CONTEXT_TIME_BUDGET_SECONDS: "0" })), JSON.stringify(msgs));
}));

test("values come from the clock; the start is the first request and lives in the trim state", withHome(async (home) => {
  const first = await run(home, turns([LS]), 0.7);
  assert.deepEqual(clockLines(first), ["[TIME REMAINING: 29m59s of 30m budget]"]);
  assert.equal(stateOf(home).clock_start, T0);
  const second = await run(home, turns([LS, CAT]), 754.2);
  assert.deepEqual(clockLines(second), ["[TIME REMAINING: 29m59s of 30m budget]", "[TIME REMAINING: 17m25s of 30m budget]"]);
  const third = await run(home, turns([LS, CAT, TEST]), 4000);
  assert.equal(clockLines(third).at(-1), "[TIME REMAINING: 0m00s of 30m budget]");
  assert.equal(stateOf(home).clock_start, T0);
}));

test("the line goes on the newest result only and re-renders byte-identically", withHome(async (home) => {
  const first = await run(home, turns([LS]), 0);
  assert.equal(lastResult(first), "a.py\nb.py\n\n[TIME REMAINING: 30m00s of 30m budget]");
  assert.deepEqual(first.slice(0, 2), turns([LS]).slice(0, 2));
  const second = await run(home, turns([LS, CAT]), 61);
  assert.equal(JSON.stringify(second.slice(0, first.length)), JSON.stringify(first));
  assert.equal(lastResult(second), "print(1)\n\n[TIME REMAINING: 28m59s of 30m budget]");
  // A retry re-renders the recorded line, not a fresh value.
  assert.equal(JSON.stringify(await run(home, turns([LS, CAT]), 300)), JSON.stringify(second));
  assert.deepEqual(stateOf(home).clock.map((d) => d.at), [2, 4]);
}));

test("no line without a new tool result", withHome(async (home) => {
  const msgs = [...turns([LS]), { role: "assistant", content: [{ type: "text", text: "done" }] },
    { role: "user", content: [{ type: "text", text: "thanks" }] }];
  assert.equal(JSON.stringify(await run(home, msgs, 0)), JSON.stringify(msgs));
}));

test("the line survives a trim, stays out of its note, and the trimmed view re-renders", withHome(async (home) => {
  const steps = [LS, ["bash", { command: "ls -la" }, "x".repeat(400)], CAT];
  for (const n of [1, 2]) await run(home, turns(steps.slice(0, n)), n);
  const trimmed = await run(home, turns(steps), 30, { CONTEXT_TRIM_AT_TOKENS: "1", CONTEXT_CLEAR_KEEP_RESULTS: "1",
    CONTEXT_TRIM_TO_TOKENS: "10000000" });
  assert.ok(JSON.stringify(trimmed[0]).includes("[smarter-compact: session"));
  assert.equal(lastResult(trimmed), "print(1)\n\n[TIME REMAINING: 29m31s of 30m budget]");
  for (const m of trimmed) for (const p of m.content ?? []) {
    if (p?.type === "text" && p.text.startsWith("[smarter-compact")) assert.ok(!p.text.includes("TIME REMAINING"));
  }
  const again = await run(home, turns(steps), 90, { CONTEXT_CLEAR_KEEP_RESULTS: "1" });
  assert.equal(JSON.stringify(again), JSON.stringify(trimmed));
}));
