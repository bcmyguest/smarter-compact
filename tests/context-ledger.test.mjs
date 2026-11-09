import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import {
  LEDGER_HEAD, LEDGER_MAX_CHARS, LOG_TAIL_LINES, effects, isLog, ledgerBlock, readOnly, reduceLog, shlexTokens, walk,
} from "../plugin/lib/context-ledger.mjs";
import {
  DEDUP_PREFIX, KEEP_ERRORS_MAX_CHARS, MASK_PREFIX, blobId, dedupPlaceholder, isErrorResult, trimContext, trimSettings,
} from "../plugin/lib/context-trim.mjs";
import { sweepOrphanBlobs } from "../plugin/lib/session-db.mjs";

// Same contracts as proxy/tests/test_ledger.py, test_log_reduce.py, test_strip_batch.py and test_keep_errors.py.
const SID = "sess-1";
const FILE = Array.from({ length: 40 }, (_, k) => `line ${k + 1} of a.py with some text`).join("\n");
const NUMBERED = Array.from({ length: 11 }, (_, k) => `${String(k + 5).padStart(6)}\tline ${k + 5} of a.py with some text`).join("\n");
const CAT = ["bash", { command: "cat a.py" }, FILE];
const READ_RANGE = ["read", { filePath: "a.py", offset: 5, limit: 11 }, NUMBERED];
const EDIT = ["edit", { filePath: "a.py", oldString: "x", newString: "y" }, "The file a.py has been updated."];
const TEST = ["bash", { command: "pytest -q tests" }, "1 passed in 0.1s"];
const PASS = Array.from({ length: 300 }, (_, k) => `tests/test_mod.py::test_case_${k} PASSED`);
const LOG = ["============================= test session starts ==============================", ...PASS.slice(0, 150),
  "tests/test_x.py::test_boom FAILED", ...PASS.slice(150), "=================================== FAILURES ===",
  "", "    def test_boom():", ">       assert 1 == 2", "E       assert 1 == 2", "", "tests/test_x.py:12: AssertionError",
  "=========================== short test summary info ============================",
  "FAILED tests/test_x.py::test_boom - assert 1 == 2", "=== 1 failed, 300 passed in 2.10s ==="].join("\n");

function withHome(fn) {
  return async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-ledger-"));
    try { await fn(home); } finally { fs.rmSync(home, { recursive: true, force: true }); }
  };
}

/** The task, then per step one tool call and its result: steps = [[name, input, text, isError?]]. */
function turns(steps) {
  const messages = [{ role: "user", content: [{ type: "text", text: "the task" }] }];
  steps.forEach(([name, input, out, err], k) => {
    messages.push({ role: "assistant", content: [{ type: "tool-call", id: `p${k}`, name, input }] });
    messages.push({ role: "tool", content: [{ type: "tool-result", id: `p${k}`, name, result: { type: err ? "error" : "text", value: out } }] });
  });
  return messages;
}

const resultsOf = (view) => Object.fromEntries(view.flatMap((m) => (Array.isArray(m.content) ? m.content : [])
  .filter((p) => p?.type === "tool-result").map((p) => [p.id, p.result.value])));
const ledgerTexts = (view) => view.flatMap((m) => (Array.isArray(m.content) ? m.content : []))
  .flatMap((p) => (p?.type === "tool-result" ? [p.result.value] : p?.type === "text" ? [p.text] : []))
  .filter((t) => typeof t === "string" && t.includes(LEDGER_HEAD)).map((t) => t.slice(t.indexOf(LEDGER_HEAD)));
const stateOf = (home) => JSON.parse(fs.readFileSync(path.join(home, "context", "sessions", `${SID}.trim.json`), "utf8"));

async function run(home, messages, env = {}) {
  const settings = trimSettings({ CONTEXT_COMPRESS_OVER_TOKENS: "0", CONTEXT_STANDING_INSTRUCTION: "0", CONTEXT_PROGRESS_NOTE: "0",
    CONTEXT_MASK_OBSERVATIONS: "0", CONTEXT_SCRATCHPAD: "0", CONTEXT_CLEAR_KEEP_RESULTS: "0", CONTEXT_STRIP_OLD_REASONING: "0",
    CONTEXT_CHARS_PER_TOKEN: "1", CONTEXT_TRIM_AT_TOKENS: "10000000", SMARTER_COMPACT_STATE_DIR: home, ...env }, home);
  return trimContext({ sessionID: SID, messages: structuredClone(messages) }, settings);
}

const grow = async (home, steps, env) => {
  const out = [];
  for (let n = 1; n <= steps.length; n++) out.push(await run(home, turns(steps.slice(0, n)), env));
  return out;
};

test("shlexTokens and readOnly follow Python shlex and evict.read_only", () => {
  assert.deepEqual(shlexTokens(`cat "a b" 'c'd e\\ f`), ["cat", "a b", "cd", "e f"]);
  assert.deepEqual(shlexTokens("ls&&cat x|wc -l # c", { punctuation: "();<>|&", commenters: "#" }), ["ls", "&&", "cat", "x", "|", "wc", "-l"]);
  assert.throws(() => shlexTokens("cat 'x"));
  assert.ok(readOnly("git status && ls -la | grep x"));
  assert.ok(!readOnly("sed -i 's/a/b/' x"));
  assert.ok(!readOnly("make test"));
  assert.ok(!readOnly("cat x > y"));
});

test("shell writes are parsed paths only, as in the proxy (shared fixture)", () => {
  // Trial path-tracing-reverse__jv99Lub once recorded `Modified: out.txt, /tmp/dis.txt, :', I',len, IIBBBBB',w,h,8,2,0,0,0`.
  const cases = JSON.parse(fs.readFileSync(new URL("./fixtures/ledger-shell-writes.json", import.meta.url), "utf8"));
  for (const c of cases) {
    const edits = effects("Bash", { command: c.command }).filter((e) => e[0] === "edit").map((e) => e[1]);
    assert.deepEqual(edits, c.writes, c.source ?? c.command.slice(0, 60));
  }
});

test("ledger effects and walk match the proxy rules", () => {
  assert.deepEqual(effects("Read", { file_path: "./a.py" }), [["read", "a.py", 1, null, "read"]]);
  assert.deepEqual(effects("Bash", { command: "sed -n '10,20p' a.py" }), [["read", "a.py", 10, 20, "sed"]]);
  assert.deepEqual(effects("bash", { command: "head -n 5 a.py" }), [["read", "a.py", 1, 5, "head"]]);
  assert.deepEqual(effects("Bash", { command: "tail -20 a.py" }), [["read", "a.py", null, 20, "tail"]]);
  assert.deepEqual(effects("Bash", { command: "echo x > b.py" }), [["edit", "b.py"], ["global"]]);
  assert.deepEqual(effects("Bash", { command: "pytest -q" }), [["test", "pytest -q"]]);
  assert.deepEqual(effects("apply_patch", { input: "*** Begin Patch\n*** Update File: d.py\n@@" }), [["edit", "d.py"]]);
  assert.deepEqual(effects("shell", { command: ["bash", "-lc", "cat e.py"] }), [["read", "e.py", 1, null, "cat"]]);
  const steps = (calls) => calls.map(([name, args, out], k) => [k, `c${k}`, name, args, out, false]);
  assert.deepEqual(walk(steps([CAT, READ_RANGE]), 1).repeats.map((r) => [r.rid, r.covers]), [["c1", [[0, "c0"]]]]);
  assert.deepEqual(walk(steps([CAT, EDIT, READ_RANGE]), 2).repeats, []);
  assert.deepEqual(walk(steps([TEST, TEST]), 1).repeats, [{ rid: "c1", j: 1, test: "pytest -q tests" }]);
  assert.deepEqual(ledgerBlock(walk(steps([CAT, EDIT, READ_RANGE]), 3).state).split("\n"),
    [LEDGER_HEAD, "Modified (latest last): a.py", "Read (latest last):", "- a.py: whole stale; lines 5-15 current"]);
  const many = steps(Array.from({ length: 200 }, (_, k) => ["Read", { file_path: `src/module_${String(k).padStart(3, "0")}.py` }, "x"]));
  const text = ledgerBlock(walk(many, 200).state);
  assert.ok(text.length <= LEDGER_MAX_CHARS && text.includes("earlier files not listed") && text.endsWith("src/module_199.py: whole current"));
});

test("CONTEXT_LEDGER: blocks on new results, reuse pointers, nudges, byte-identical re-renders", withHome(async (home) => {
  assert.equal(trimSettings({}, os.tmpdir()).ledger, true);
  const on = { CONTEXT_LEDGER: "1" };
  const offEnv = { CONTEXT_LEDGER: "0" };
  // Off: old bytes, also with recorded state.
  const steps = [CAT, READ_RANGE];
  for (const [n, view] of (await grow(home, steps, offEnv)).entries()) assert.equal(JSON.stringify(view), JSON.stringify(turns(steps.slice(0, n + 1))));
  const views = await grow(home, steps, on);
  assert.equal(JSON.stringify(await run(home, turns(steps), offEnv)), JSON.stringify(turns(steps)));
  assert.ok(views[0][2].content[0].result.value.startsWith(`${FILE}\n\n<system-reminder>\n${LEDGER_HEAD}`));
  assert.equal(JSON.stringify(views[1].slice(0, views[0].length)), JSON.stringify(views[0]));
  // The range read is covered by the whole cat, but its output differs: it stays whole, with no nudge.
  const p1 = resultsOf(views[1]).p1;
  assert.ok(p1.startsWith(`${NUMBERED}\n\n<system-reminder>\n${LEDGER_HEAD}`) && !p1.includes("Repeats in this batch"));
  assert.deepEqual(sweepOrphanBlobs(path.join(home, "context")), []);
  const later = await run(home, turns([...steps, TEST]), on);
  assert.equal(JSON.stringify(later.slice(0, views[1].length)), JSON.stringify(views[1]));
  assert.deepEqual(stateOf(home).ledger.map((d) => d.at), [2, 4]);
  // A stale read stays; a test rerun with no edit is nudged.
  const other = fs.mkdtempSync(path.join(os.tmpdir(), "sc-ledger-"));
  try {
    const stale = await grow(other, [CAT, EDIT, READ_RANGE], on);
    assert.equal(resultsOf(stale.at(-1)).p2.split("\n\n")[0], NUMBERED);
    const rerun = await grow(other, [CAT, EDIT, READ_RANGE, TEST, TEST], on);
    assert.match(ledgerTexts(rerun.at(-1)).at(-1), /`pytest -q tests` already ran with no file change since/);
  } finally { fs.rmSync(other, { recursive: true, force: true }); }
}));

test("CONTEXT_LEDGER: only a byte-identical re-read becomes the dedup pointer; a changed one stays whole", withHome(async (home) => {
  const noDedup = { CONTEXT_LEDGER: "1", CONTEXT_DEDUP_RESULTS: "0" };
  const changed = `${FILE}\nline 41 appended`;
  const kept = await grow(home, [CAT, ["bash", { command: "cat a.py" }, changed]], noDedup);
  assert.equal(resultsOf(kept.at(-1)).p1, changed);
  assert.ok(!ledgerTexts(kept.at(-1)).some((t) => t.includes("Repeats in this batch")));
  const other = fs.mkdtempSync(path.join(os.tmpdir(), "sc-ledger-"));
  const third = fs.mkdtempSync(path.join(os.tmpdir(), "sc-ledger-"));
  try {
    // With CONTEXT_DEDUP_RESULTS off the ledger makes the pointer; on, dedup does: the same bytes either way.
    const views = await grow(other, [CAT, CAT], noDedup);
    assert.equal(resultsOf(views.at(-1)).p1, dedupPlaceholder(FILE));
    assert.ok(dedupPlaceholder(FILE).startsWith(DEDUP_PREFIX));
    assert.deepEqual(stateOf(other).dedup.map((d) => [d.kind, d.at, d.id, d.src, d.bid]), [["reuse", 4, "p1", "p0", blobId(FILE)]]);
    assert.equal(fs.readFileSync(path.join(other, "context", "blobs", `${blobId(FILE)}.txt`), "utf8"), FILE);
    assert.deepEqual(sweepOrphanBlobs(path.join(other, "context")), []);
    const later = await run(other, turns([CAT, CAT, TEST]), noDedup);
    assert.equal(JSON.stringify(later.slice(0, views.at(-1).length)), JSON.stringify(views.at(-1)));
    assert.equal(JSON.stringify(await grow(third, [CAT, CAT])), JSON.stringify(views));
  } finally {
    fs.rmSync(other, { recursive: true, force: true });
    fs.rmSync(third, { recursive: true, force: true });
  }
}));

test("CONTEXT_LEDGER: a read whose earlier copy was cleared is nudged; a trim keeps only the newest block", withHome(async (home) => {
  const on = { CONTEXT_LEDGER: "1" };
  const steps = [CAT, ["bash", { command: "ls" }, "b".repeat(400)], ["bash", { command: "ls -la" }, "c".repeat(400)], READ_RANGE];
  await grow(home, steps.slice(0, 3), on);
  const view = await run(home, turns(steps), { ...on, CONTEXT_CLEAR_KEEP_RESULTS: "2", CONTEXT_TRIM_AT_TOKENS: "2600", CONTEXT_TRIM_TO_TOKENS: "2600" });
  assert.ok(resultsOf(view).p0.startsWith(MASK_PREFIX));
  assert.equal(resultsOf(view).p3.split("\n\n")[0], NUMBERED);
  assert.match(ledgerTexts(view).at(-1), /a\.py \(lines 5-15\) was read before and is unchanged since/);
  // The plugin appends a block to the result text, so clearing that result clears its block too: only the new one is left.
  assert.equal(ledgerTexts(view).length, 1);
}));

test("CONTEXT_LOG_REDUCE_OVER_CHARS: a new test log keeps failure windows and its tail, recorded", withHome(async (home) => {
  assert.equal(trimSettings({}, os.tmpdir()).logReduceOverChars, 6000);
  const { reduced, id } = reduceLog(LOG, 6000);
  assert.equal(id, blobId(LOG));
  const body = reduced.split("\n");
  const at = body.indexOf("tests/test_x.py::test_boom FAILED");
  assert.deepEqual(body.slice(at - 3, at), PASS.slice(147, 150));
  assert.deepEqual(body.slice(at + 1, at + 9), PASS.slice(150, 158));
  assert.deepEqual(body.slice(-LOG_TAIL_LINES), LOG.split("\n").slice(-LOG_TAIL_LINES));
  assert.ok(!isLog("plain text\n".repeat(400)) && reduceLog("plain text\n".repeat(400), 1) === null);
  const steps = [["bash", { command: "pytest" }, LOG]];
  assert.equal(JSON.stringify(await run(path.join(home, "off"), turns(steps), { CONTEXT_LOG_REDUCE_OVER_CHARS: "0" })), JSON.stringify(turns(steps)));
  const view = await run(home, turns(steps), { CONTEXT_LOG_REDUCE_OVER_CHARS: "6000" });
  assert.equal(resultsOf(view).p0, reduced);
  assert.equal(fs.readFileSync(path.join(home, "context", "blobs", `${id}.txt`), "utf8"), LOG);
  assert.deepEqual(stateOf(home).logs.map((d) => [d.at, d.id, d.bid]), [[2, "p0", id]]);
  const later = await run(home, turns([...steps, TEST]), { CONTEXT_LOG_REDUCE_OVER_CHARS: "6000" });
  assert.equal(JSON.stringify(later.slice(0, view.length)), JSON.stringify(view));
  assert.deepEqual(sweepOrphanBlobs(path.join(home, "context")), []);
  const db = new DatabaseSync(path.join(home, "context", "sessions", `${SID}.db`));
  try {
    const row = db.prepare("SELECT text, blob_id FROM messages WHERE kind = 'tool_result' ORDER BY seq LIMIT 1").get();
    assert.deepEqual([row.text, row.blob_id], [LOG, id]);
  } finally { db.close(); }
}));

function loop(steps, sig = 4000) {
  const messages = [{ role: "user", content: [{ type: "text", text: "the task" }] }];
  for (let k = 0; k < steps; k++) {
    messages.push({ role: "assistant", content: [
      { type: "reasoning", text: "", providerMetadata: { anthropic: { signature: String(k).repeat(sig) } } },
      { type: "text", text: `step ${k}` }, { type: "tool-call", id: `t${k}`, name: "bash", input: { command: `echo ${k}` } }] });
    messages.push({ role: "tool", content: [{ type: "tool-result", id: `t${k}`, name: "bash", result: { type: "text", value: `out ${k}` } }] });
  }
  return messages;
}

const reasoningAt = (view) => view.flatMap((m, j) => (Array.isArray(m.content) && m.content.some((p) => p?.type === "reasoning") ? [j] : []));

test("CONTEXT_STRIP_REASONING_EVERY: the bound moves at batch boundaries, gated, never emptying a message", withHome(async (home) => {
  const s = trimSettings({}, os.tmpdir());
  assert.deepEqual([s.stripReasoningEvery, s.stripReasoningMinTokens], [0, 8000]);
  const env = { CONTEXT_STRIP_REASONING_EVERY: "4", CONTEXT_STRIP_REASONING_MIN_TOKENS: "0" };
  const views = {};
  for (let n = 1; n < 10; n++) views[n] = await run(home, loop(n), env);
  for (const n of [1, 2, 3]) assert.equal(JSON.stringify(views[n]), JSON.stringify(loop(n)));
  assert.equal(views[4].length, loop(4).length);
  assert.deepEqual(reasoningAt(views[4]), [7]);
  for (const n of [5, 6, 7]) assert.equal(JSON.stringify(views[n].slice(0, views[n - 1].length)), JSON.stringify(views[n - 1]));
  assert.deepEqual(reasoningAt(views[8]), [15]);
  const off = fs.mkdtempSync(path.join(os.tmpdir(), "sc-ledger-"));
  try {
    for (let n = 1; n < 10; n++) assert.equal(JSON.stringify(await run(off, loop(n))), JSON.stringify(loop(n)));
    // The gain gate: 3 blocks of 4,000 at step 4 miss a 20,000 floor; step 8 clears it.
    const gated = {};
    for (let n = 1; n < 10; n++) gated[n] = await run(off, loop(n), { ...env, CONTEXT_STRIP_REASONING_MIN_TOKENS: "20000" });
    assert.equal(JSON.stringify(gated[7]), JSON.stringify(loop(7)));
    assert.deepEqual(reasoningAt(gated[8]), [15]);
  } finally { fs.rmSync(off, { recursive: true, force: true }); }
  // A message holding only reasoning keeps it.
  const only = loop(4).map((m) => (m.role === "assistant" ? { ...m, content: [m.content[0]] } : m)).filter((m) => m.role !== "tool")
    .flatMap((m) => (m.role === "assistant" ? [m, { role: "user", content: [{ type: "text", text: "go" }] }] : [m]));
  const kept = fs.mkdtempSync(path.join(os.tmpdir(), "sc-ledger-"));
  try { assert.equal(JSON.stringify(await run(kept, only, env)), JSON.stringify(only)); } finally { fs.rmSync(kept, { recursive: true, force: true }); }
}));

const LONG = "y".repeat(3000);
const OK = ["ok ".repeat(200), false];
const ERR = [`Exit code 1\n${"boom ".repeat(200)}`, true];

function errTurns(outs) {
  return turns(outs.map(([out, err], k) => ["bash", { command: `run ${k} ${LONG}` }, out, err]));
}

test("CONTEXT_KEEP_ERRORS: the newest error results stay whole through clearing until the next trim", withHome(async (home) => {
  assert.equal(trimSettings({}, os.tmpdir()).keepErrors, 3);
  assert.ok(isErrorResult(false, "Traceback (most recent call last):\n  x") && !isErrorResult(false, "exit code 0"));
  const full = (outs) => String(Math.ceil(JSON.stringify(errTurns(outs)).length / 1.2));
  const env = (outs) => ({ CONTEXT_KEEP_ERRORS: "2", CONTEXT_CLEAR_KEEP_RESULTS: "1", CONTEXT_CHARS_PER_TOKEN: "2.5",
    CONTEXT_TRIM_AT_TOKENS: String(Math.floor(Number(full(outs)) / 2.5 * 0.9)), CONTEXT_TRIM_TO_TOKENS: full(outs) });
  const outs = [ERR, ERR, ERR, OK, OK];
  const view = await run(home, errTurns(outs), env(outs));
  const r = resultsOf(view);
  assert.ok(r.p0.startsWith(MASK_PREFIX));
  assert.deepEqual([r.p1, r.p2], [ERR[0], ERR[0]]);
  assert.deepEqual(Object.values(stateOf(home).threads)[0].keep_errors, ["p2", "p1"]);
  const again = await run(home, errTurns([...outs, OK]), { ...env(outs), CONTEXT_TRIM_AT_TOKENS: "10000000" });
  assert.equal(JSON.stringify(again.slice(0, view.length)), JSON.stringify(view));
  // Off: the same request clears every old result.
  const off = fs.mkdtempSync(path.join(os.tmpdir(), "sc-ledger-"));
  try {
    const plain = resultsOf(await run(off, errTurns(outs), { ...env(outs), CONTEXT_KEEP_ERRORS: "0" }));
    assert.ok(plain.p1.startsWith(MASK_PREFIX) && plain.p2.startsWith(MASK_PREFIX));
  } finally { fs.rmSync(off, { recursive: true, force: true }); }
}));

test("CONTEXT_KEEP_ERRORS: only an error result up to KEEP_ERRORS_MAX_CHARS stays whole", withHome(async (home) => {
  const atCap = `Exit code 1\n${"b".repeat(KEEP_ERRORS_MAX_CHARS - 12)}`;
  assert.equal(atCap.length, KEEP_ERRORS_MAX_CHARS);
  const outs = [[atCap, true], [`${atCap}b`, true], OK, OK];
  const full = String(Math.ceil(JSON.stringify(errTurns(outs)).length / 1.2));
  const r = resultsOf(await run(home, errTurns(outs), { CONTEXT_KEEP_ERRORS: "2", CONTEXT_CLEAR_KEEP_RESULTS: "1",
    CONTEXT_CHARS_PER_TOKEN: "2.5", CONTEXT_TRIM_AT_TOKENS: String(Math.floor(Number(full) / 2.5 * 0.9)), CONTEXT_TRIM_TO_TOKENS: full }));
  assert.equal(r.p0, atCap);
  assert.ok(r.p1.startsWith(MASK_PREFIX));
  assert.deepEqual(Object.values(stateOf(home).threads)[0].keep_errors, ["p0"]);
}));

test("a trailing system message leaves the ledger block on the newest result", withHome(async (home) => {
  const cat = ["bash", { command: "cat a.py" }, "print(1)"];
  const ls = ["bash", { command: "ls" }, "a.py\nb.py"];
  const sys = (n) => ({ role: "system", content: `<total_tokens>${n} tokens left</total_tokens>` });
  const first = [...turns([cat]), sys(1000)];
  const v1 = await run(home, first, { CONTEXT_LEDGER: "1" });
  assert.deepEqual(v1.at(-1), first.at(-1));
  assert.match(v1.at(-2).content.at(-1).result.value, /\[smarter-compact ledger/);
  const second = [...first, ...turns([cat, ls]).slice(-2), sys(999)];
  const v2 = await run(home, second, { CONTEXT_LEDGER: "1" });
  assert.equal(JSON.stringify(v2.slice(0, 4)), JSON.stringify(v1));
}));
