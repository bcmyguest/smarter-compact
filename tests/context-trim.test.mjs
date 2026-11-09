import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import {
  DEDUP_MIN_CHARS,
  DEDUP_PREFIX,
  INPUT_MASK_PREFIX,
  REUSE_RULES,
  dedupPlaceholder,
  MASK_PREFIX,
  blobId,
  inputPlaceholder,
  maskedInput,
  maskPlaceholder,
  addStandingInstruction,
  pointerText,
  reasoningText,
  recallToolNames,
  requestChars,
  signedChars,
  standingInstruction,
  trimContext,
  trimSettings,
  trimToFor,
  wrapNote,
} from "../plugin/lib/context-trim.mjs";
import { callContextStoreTool } from "../plugin/lib/mcp/context-store.mjs";
import { sweepOrphanBlobs } from "../plugin/lib/session-db.mjs";

// The pointer note and the standing instruction are what make a trimmed-away past recoverable: they
// are the only text telling the model its past is stored, where to look, and that no summary exists.
// Both are duplicated in proxy/smarter_compact/core.py for the LiteLLM path, so the assertions here
// are the same contract as proxy/tests/test_context_proxy.py::StandingInstructionTests — change one,
// change the other.

const SID = "sess-1";

test("requestChars counts signed reasoning by its text, not its encrypted payload", () => {
  const signed = { type: "reasoning", text: "plan", providerMetadata: { anthropic: { signature: "s".repeat(200000) } } };
  const redacted = { type: "reasoning", text: "", providerMetadata: { anthropic: { redactedData: "d".repeat(50000) } } };
  assert.equal(requestChars([signed, redacted]), 4);
  assert.equal(requestChars({ type: "reasoning", text: "plan" }), "typereasoningtextplan".length);
});

test("the pointer states what was removed, denies a summary, and names both tools", () => {
  const note = pointerText(SID, 3);
  assert.match(note, /^\[smarter-compact: continuing session sess-1; 3 earlier messages are NOT in your context\./);
  assert.match(note, /a gap in what you see \(missing batches, lines, keys\) is in them/);
  assert.match(note, /Nothing here is or was ever summarized: this trim wrote no summary of the dropped messages/);
  assert.match(note, /do not treat a "summary of earlier context" you seem to remember as their record/);
  assert.match(note, /The tools smart_compact_search and smart_compact_sql are available to you in this request/);
  assert.match(note, /You MUST call smart_compact_search\(session_id="sess-1", query=\.\.\. or pattern=\.\.\.\)/);
  assert.match(note, /never claim "not found", "never received" or "no search tool" without calling it/);
  assert.match(note, /Dropped user turns may hold instructions or corrections that still apply: search them \(smart_compact_search with role="user"\) before changing course\./);
  assert.match(note, /To recover a file you read earlier, search for the text you need instead of reading it again/);
  assert.match(note, /re-read only the lines you still lack \(offset\/limit\)/);
  assert.match(note, /smart_compact_sql\(session_id="sess-1", sql=\.\.\.\)\.\]$/);
});

// Same form as wrap_note in proxy adapters.py (proxy/tests/test_note_placement.py checks the bytes against this).
test("an inline note is wrapped the way Claude Code marks injected context", () => {
  assert.equal(wrapNote("[smarter-compact: x]"), "<system-reminder>\n[smarter-compact: x]\n</system-reminder>");
  assert.equal(wrapNote(""), "");
});

test("the standing instruction is the same string on every call", () => {
  assert.equal(standingInstruction(), standingInstruction());
  assert.match(standingInstruction(), /older turns may be removed and replaced by an \[smarter-compact: …\] pointer/);
  assert.match(standingInstruction(), /Removed content is saved, not forgotten/);
  assert.match(standingInstruction(), /This trim never summarizes removed content — a remembered "summary of earlier context" is not its record/);
});

// Harnesses (OpenCode, the SWE-bench eval harness) register MCP tools under a prefix. A note that
// names the bare `smart_compact_search` names a tool the model was never offered, which reads as a
// hallucination — the hard45 sweep trimmed 15/15 smart trials and recorded zero recall calls.
test("both texts name the tools the request actually offered", () => {
  const prefixed = [
    { name: "mcp__smarter-compact__smart_compact_search" },
    { name: "mcp__smarter-compact__smart_compact_sql" },
    { name: "mcp__codegraph__codegraph_explore" },
  ];
  const names = recallToolNames(prefixed);
  assert.deepEqual([names.search, names.sql],
    ["mcp__smarter-compact__smart_compact_search", "mcp__smarter-compact__smart_compact_sql"]);

  const note = pointerText(SID, 2, names);
  assert.match(note, /The tools mcp__smarter-compact__smart_compact_search and mcp__smarter-compact__smart_compact_sql are available/);
  assert.match(note, /You MUST call mcp__smarter-compact__smart_compact_search\(session_id="sess-1"/);
  assert.ok(!/The tools smart_compact_search and/.test(note), "the bare name must not survive a prefixed request");

  const standing = standingInstruction(names);
  assert.match(standing, /call mcp__smarter-compact__smart_compact_search \(use `pattern`/);
  assert.match(standing, /or mcp__smarter-compact__smart_compact_sql for counts/);
  assert.ok(!standing.includes("— call smart_compact_search ("), "the bare name must not survive a prefixed request");
});

test("a prefixed tool list resolves to bare names when no recall tool is offered", () => {
  const names = recallToolNames([{ name: "Bash" }, { name: "mcp__codegraph__codegraph_explore" }]);
  assert.deepEqual([names.search, names.sql], ["smart_compact_search", "smart_compact_sql"]);
  assert.deepEqual(names.offered, []);
});

// Claude Code defers MCP tools behind ToolSearch, so neither recall tool is in the request and the bare
// name fails "No such tool available" (TB2.1 15k probe). Both texts must name the real tools and the load.
test("deferred recall tools are named and loaded through ToolSearch", () => {
  const names = recallToolNames([{ name: "Bash" }, { name: "ToolSearch" }]);
  const load = 'ToolSearch with query "select:mcp__smarter-compact__smart_compact_search,mcp__smarter-compact__smart_compact_sql"';
  assert.equal(names.load, load);
  const note = pointerText(SID, 2, names);
  assert.ok(note.includes(`load them first by calling ${load}`));
  assert.match(note, /You MUST call mcp__smarter-compact__smart_compact_search\(session_id="sess-1"/);
  assert.ok(standingInstruction(names).includes(`load them first by calling ${load}`));
  const offered = recallToolNames([{ name: "ToolSearch" }, { name: "mcp__x__smart_compact_search" }]);
  assert.deepEqual([offered.search, offered.load], ["mcp__x__smart_compact_search", ""]);
});

const SETTINGS = { enabled: true, standingInstruction: true };

// The v2 OpenCode path calls this per request (src/v2.mjs) with the system array it is about to
// send, so idempotence across re-processing is what keeps the block single.
test("addStandingInstruction appends one block, then no-ops on the marker", () => {
  for (const shape of [[], [{ type: "text", text: "You are terse." }]]) {
    const system = shape.map((part) => ({ ...part }));
    assert.equal(addStandingInstruction(system, SETTINGS), true);
    assert.equal(system.at(-1).text, standingInstruction());
    assert.deepEqual(system.slice(0, -1), shape, "existing blocks are left alone");
    assert.equal(addStandingInstruction(system, SETTINGS), false, "the marker guard keeps it a single block");
    assert.equal(system.length, shape.length + 1);
  }
});

test("addStandingInstruction fail-opens when disabled or the system is not an array", () => {
  const system = [{ type: "text", text: "You are terse." }];
  assert.equal(addStandingInstruction(system, { enabled: false, standingInstruction: true }), false);
  assert.equal(addStandingInstruction(system, { enabled: true, standingInstruction: false }), false);
  assert.equal(addStandingInstruction("a string system", SETTINGS), false);
  assert.equal(addStandingInstruction(undefined, SETTINGS), false);
  assert.deepEqual(system, [{ type: "text", text: "You are terse." }], "nothing appended in any fail-open case");
});

// src/v2.mjs passes the request's own tool list, so an OpenCode agent sees the prefixed names it
// can actually call rather than a bare name its harness renamed away.
test("the injected block carries the request's tool names", () => {
  const names = recallToolNames([{ name: "smarter-compact_smart_compact_search" }, { name: "smarter-compact_smart_compact_sql" }]);
  const system = [{ type: "text", text: "You are terse." }];
  assert.equal(addStandingInstruction(system, SETTINGS, names), true);
  assert.match(system.at(-1).text, /call smarter-compact_smart_compact_search \(use `pattern`/);
});

// The separator is per harness: OpenCode uses a dot, Claude Code `mcp__<server>__<tool>`. A missed
// separator silently falls back to the bare name — the original bug — so each real spelling is
// pinned here.
test("every harness namespace spelling resolves", () => {
  for (const spelling of ["smarter-compact.smart_compact_search", "mcp__smarter-compact__smart_compact_search",
                          "smarter-compact_smart_compact_search", "smarter-compact:smart_compact_search"]) {
    const names = recallToolNames([{ name: spelling }, { name: "Bash" }]);
    assert.equal(names.search, spelling);
    assert.ok(pointerText(SID, 1, names).includes(spelling), spelling);
  }
  // Not a namespace, a different tool: must not be mistaken for recall.
  assert.deepEqual(recallToolNames([{ name: "xsmart_compact_search" }]).offered, []);
});

// OpenCode's `context` hook hands tools over keyed by id, not as a list — the shape that made the
// pointer order bare `smart_compact_search` while the model was offered `smarter-compact_smart_compact_search`
// (captured live in bench/swebench/results/namerun). All three delivery shapes must resolve.
test("tools arrive as a list, a keyed object, or a definitions wrapper", () => {
  const search = "smarter-compact_smart_compact_search";
  const sql = "smarter-compact_smart_compact_sql";
  const pair = [{ name: search }, { name: sql }, { name: "shell" }];
  const shapes = {
    list: pair,
    strings: [search, sql, "shell"],
    keyed: { [search]: { description: "search" }, [sql]: { description: "counts" }, shell: { description: "run" } },
    wrapper: { definitions: pair },
  };
  for (const [shape, tools] of Object.entries(shapes)) {
    const names = recallToolNames(tools);
    assert.equal(names.search, search, shape);
    assert.equal(names.sql, sql, shape);
    assert.equal(names.offered.length, 2, shape);
    assert.equal(names.seen, 3, shape);
    assert.ok(pointerText(SID, 1, names).includes(search), shape);
    assert.ok(standingInstruction(names).includes(`call ${search} (`), shape);
  }
  // Nothing offered: the bare fallback, and `seen` says so rather than hiding it.
  const none = recallToolNames({ shell: {}, read: {} });
  assert.deepEqual([none.search, none.sql, none.offered.length, none.seen],
    ["smart_compact_search", "smart_compact_sql", 0, 2]);
});

test("CONTEXT_TRIM_MIN_GAIN_TOKENS skips a trim that frees too little", async () => {
  const text = (role, t) => ({ role, content: [{ type: "text", text: t }] });
  const messages = [];
  for (let i = 0; i < 3; i++) messages.push(text("user", `question ${i} ${"q".repeat(200)}`), text("assistant", `answer ${i}`));
  messages.push(text("user", "now this"));
  assert.equal(trimSettings({}, os.tmpdir()).trimMinGainTokens, 0);
  const run = async (gain) => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-gain-"));
    const settings = trimSettings({ CONTEXT_TRIM_AT_TOKENS: "1", CONTEXT_TRIM_MIN_GAIN_TOKENS: String(gain), CONTEXT_SCRATCHPAD: "0", SMARTER_COMPACT_STATE_DIR: home }, home);
    try { return await trimContext({ sessionID: SID, messages }, settings); } finally { fs.rmSync(home, { recursive: true, force: true }); }
  };
  assert.equal(await run(1e9), messages);
  assert.ok((await run(1)).length < messages.length);
});

// CONTEXT_CLEAR_KEEP_RESULTS: same contract as proxy/tests/test_trim_experiments.py::ClearResultsTests.
const HUGE = `\n${"x".repeat(20000)}`;
const textMsg = (role, t) => ({ role, content: [{ type: "text", text: t }] });
const signed = { type: "reasoning", text: "go", providerMetadata: { anthropic: { signature: "SIG==" } } };

function clearTurns(pad) {
  const messages = [textMsg("user", `question ${"q".repeat(200)}`)];
  for (let k = 0; k < 5; k++) {
    messages.push({ role: "assistant", content: [signed, { type: "text", text: `step ${k}` },
      { type: "tool-call", id: `s${k}`, name: "bash", input: { command: `cmd ${k}` } }]});
    messages.push({ role: "tool", content: [{ type: "tool-result", id: `s${k}`, name: "bash", result: { type: "text", value: `out ${k}${pad}` } }] });
  }
  messages.push({ role: "assistant", content: [signed, { type: "tool-call", id: "sL", name: "bash", input: { command: "ls" } }] });
  messages.push({ role: "tool", content: [{ type: "tool-result", id: "sL", name: "bash", result: { type: "text", value: "ok" } }] });
  return messages;
}

const resultsOf = (view) => Object.fromEntries(view.flatMap((m) => m.content.filter((p) => p.type === "tool-result")
  .map((p) => [p.id, p.result.value])));

async function clearRun(home, messages, trimAt, keep = "2") {
  const settings = trimSettings({ CONTEXT_TRIM_AT_TOKENS: String(trimAt), CONTEXT_CLEAR_KEEP_RESULTS: keep,
    CONTEXT_TRIM_TO_TOKENS: String(Math.ceil(0.3 * trimAt)), CONTEXT_CHARS_PER_TOKEN: "1", CONTEXT_COMPRESS_OVER_TOKENS: "0",
    CONTEXT_STANDING_INSTRUCTION: "0", CONTEXT_PROGRESS_NOTE: "0", CONTEXT_MASK_OBSERVATIONS: "0", SMARTER_COMPACT_STATE_DIR: home }, home);
  return trimContext({ sessionID: SID, messages: structuredClone(messages) }, settings);
}

function withHome(fn) {
  return async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-clear-"));
    try { await fn(home); } finally { fs.rmSync(home, { recursive: true, force: true }); }
  };
}

test("clearing keeps 10 results by default and parses like the proxy", () => {
  assert.equal(trimSettings({}, os.tmpdir()).clearKeepResults, 10);
  assert.equal(trimSettings({ CONTEXT_CLEAR_KEEP_RESULTS: "10" }, os.tmpdir()).clearKeepResults, 10);
  assert.equal(trimSettings({ CONTEXT_CLEAR_KEEP_RESULTS: "-3" }, os.tmpdir()).clearKeepResults, 0);
});

test("trim target defaults by model and parses like the proxy", () => {
  const defaults = trimSettings({}, os.tmpdir());
  assert.deepEqual([trimToFor(defaults, "claude-sonnet-5"), trimToFor(defaults, "anthropic/claude-haiku-5-5"),
    trimToFor(defaults, null)], [66000, 50000, 66000]);
  const set = trimSettings({ CONTEXT_TRIM_TO_TOKENS: " 40000 " }, os.tmpdir());
  assert.deepEqual([trimToFor(set, "claude-haiku-5-5"), trimToFor(set, "claude-sonnet-5")], [40000, 40000]);
  for (const bad of ["0", "-5", "1.5", "x", ""]) {
    const s = trimSettings({ CONTEXT_TRIM_TO_TOKENS: bad }, os.tmpdir());
    assert.deepEqual([trimToFor(s, "claude-haiku-5"), trimToFor(s, "claude-sonnet-5")], [50000, 66000], bad);
  }
});

test("off by default a trim drops turns and clears nothing", withHome(async (home) => {
  const messages = clearTurns(HUGE);
  const view = await clearRun(home, messages, requestChars(messages), "0");
  assert.ok(!JSON.stringify(view).includes(MASK_PREFIX));
  assert.ok(view.length < messages.length);
}));

test("clearing keeps every turn and call, the newest N results, and strips old signed reasoning", withHome(async (home) => {
  const messages = clearTurns(HUGE);
  const view = await clearRun(home, messages, requestChars(messages));
  assert.deepEqual(view.map((m) => m.role), messages.map((m) => m.role));
  const results = resultsOf(view);
  assert.equal(results.sL, "ok");
  assert.equal(results.s4, `out 4${HUGE}`);
  for (const k of ["s0", "s1", "s2", "s3"]) assert.equal(results[k], maskPlaceholder(resultsOf(messages)[k]), k);
  const calls = view.flatMap((m) => m.content.filter((p) => p.type === "tool-call").map((p) => p.id));
  assert.deepEqual(calls, ["s0", "s1", "s2", "s3", "s4", "sL"]);
  // Signed reasoning before the latest run goes; the latest run keeps its own.
  assert.equal(view.flatMap((m) => m.content.filter((p) => p.type === "reasoning")).length, 1);
  assert.deepEqual(view.at(-2), messages.at(-2));
  assert.match(view[0].content[0].text, /Older tool outputs in your context read/);
  assert.ok(!/earlier messages are NOT/.test(view[0].content[0].text));
  // The store keeps every cleared output, out of context.
  const db = new DatabaseSync(path.join(home, "context", "sessions", `${SID}.db`));
  try {
    const out = db.prepare("SELECT text FROM messages WHERE in_context = 0 AND kind = 'tool_result'").all().map((r) => r.text);
    assert.ok(out.some((t) => t.startsWith("out 3")));
    assert.ok(!out.some((t) => t.startsWith("out 4")));
  } finally { db.close(); }
}));

test("a cleared result's placeholder id round-trips through smart_compact_get", withHome(async (home) => {
  const messages = clearTurns(HUGE);
  const view = await clearRun(home, messages, requestChars(messages));
  const original = resultsOf(messages);
  const cleared = Object.entries(resultsOf(view)).filter(([, v]) => v.startsWith(MASK_PREFIX));
  assert.deepEqual(cleared.map(([k]) => k), ["s0", "s1", "s2", "s3"]);
  const opts = { env: { ...process.env, HOME: home, SMARTER_COMPACT_STATE_DIR: home } };
  for (const [k, placeholder] of cleared) {
    const id = /smart_compact_get\(id="([0-9a-f]{16})"\)/.exec(placeholder)[1];
    const got = await callContextStoreTool("smart_compact_get", { id, max_chars: 100_000 }, opts);
    assert.deepEqual([got.found, got.text], [true, original[k]], k);
  }
  // The orphan sweep keeps them: each is blobId of a stored tool_result row.
  assert.deepEqual(sweepOrphanBlobs(path.join(home, "context")), []);
}));

test("clearing drops turns only when it is not enough", withHome(async (home) => {
  const messages = clearTurns(`\n${"x".repeat(300)}`);
  const view = await clearRun(home, messages, requestChars(messages));
  assert.ok(view.length < messages.length);
  const note = view.find((m) => m.content[0]?.text?.startsWith("<system-reminder>\n[smarter-compact:")).content[0].text;
  assert.match(note, /earlier messages are NOT in your context/);
  assert.match(note, /Older tool outputs/);
}));

test("the cleared prefix is byte-identical on the next request", withHome(async (home) => {
  const messages = clearTurns(HUGE);
  const first = await clearRun(home, messages, requestChars(messages));
  const more = [...messages,
    { role: "assistant", content: [{ type: "tool-call", id: "sM", name: "bash", input: { command: "pwd" } }] },
    { role: "tool", content: [{ type: "tool-result", id: "sM", name: "bash", result: { type: "text", value: `later${HUGE}` } }] }];
  const second = await clearRun(home, more, 1e9);
  assert.equal(JSON.stringify(second.slice(0, first.length)), JSON.stringify(first));
  assert.equal(resultsOf(second).sM, `later${HUGE}`);
}));

// Prose strip, observation masking and progress note: same contract as proxy/tests/test_trim_experiments.py.
// Step k of the in-flight turn: [tool name, input, result text, is error].
const STEPS = [
  ["Edit", { file_path: "/src/f0.py", old_string: "a", new_string: "b" }, "edited", false],
  ["Bash", { command: "pytest -q tests/" }, "Exit code 1\n1 failed", true],
  ["Write", { file_path: "/src/f2.py", content: "x" }, "written", false],
  ["Bash", { command: "make build" }, "built", false],
  ["Read", { file_path: "/src/f4.py" }, "contents", false],
];
const LONG = `\n${"x".repeat(300)}`;

/** Two answered turns, then a running turn: "now this", the steps as text + tool-call / tool-result, and a latest run. */
function inFlightTurn(pad = "", steps = STEPS) {
  const messages = [];
  for (let i = 0; i < 2; i++) messages.push(textMsg("user", `question ${i} ${"q".repeat(200)}`), textMsg("assistant", `answer ${i}`));
  messages.push(textMsg("user", "now this"));
  steps.forEach(([name, input, value, error], k) => {
    messages.push({ role: "assistant", content: [{ type: "text", text: `step ${k}` }, { type: "tool-call", id: `s${k}`, name, input }] });
    messages.push({ role: "tool", content: [{ type: "tool-result", id: `s${k}`, name,
      result: { type: error ? "error" : "text", value: value + pad } }] });
  });
  messages.push({ role: "assistant", content: [signed, { type: "tool-call", id: "sL", name: "bash", input: { command: "ls" } }] });
  messages.push({ role: "tool", content: [{ type: "tool-result", id: "sL", name: "bash", result: { type: "text", value: "ok" } }] });
  return messages;
}

/** Trim at 1 token with env on top of the test defaults; omitted CONTEXT_* keys keep their real defaults. */
async function expRun(home, messages, env = {}, sid = SID) {
  const settings = trimSettings({ CONTEXT_TRIM_AT_TOKENS: "1", CONTEXT_TRIM_TO_TOKENS: "1", CONTEXT_CHARS_PER_TOKEN: "1",
    CONTEXT_COMPRESS_OVER_TOKENS: "0", CONTEXT_STANDING_INSTRUCTION: "0",
    SMARTER_COMPACT_STATE_DIR: home, ...env }, home);
  return trimContext({ sessionID: sid, messages: structuredClone(messages) }, settings);
}

const OFF = { CONTEXT_STRIP_PROSE: "0", CONTEXT_MASK_OBSERVATIONS: "0", CONTEXT_PROGRESS_NOTE: "0" };
const STRIP = { ...OFF, CONTEXT_STRIP_PROSE: "1" };
const MASK = { ...OFF, CONTEXT_MASK_OBSERVATIONS: "1" };
const parts = (m) => (Array.isArray(m.content) ? m.content : []);
const idsIn = (view, type) => new Set(view.flatMap((m) => parts(m).filter((p) => p.type === type).map((p) => p.id)));
const noteOf = (view) => view[2].content[0].text;
const pointerCount = (view) => Number(/; (\d+) earlier messages are NOT/.exec(noteOf(view))[1]);

function dbRows(home, where, sid = SID) {
  const db = new DatabaseSync(path.join(home, "context", "sessions", `${sid}.db`));
  try { return db.prepare(`SELECT kind, text FROM messages WHERE ${where} ORDER BY seq`).all(); } finally { db.close(); }
}

test("experiment settings default like the proxy", () => {
  const s = trimSettings({}, os.tmpdir());
  assert.deepEqual([s.stripProse, s.maskObservations, s.progressNote], [false, true, true]);
});

test("a dropping trim keeps the signed reasoning of the latest run", withHome(async (home) => {
  const messages = inFlightTurn();
  for (const env of [{}, OFF, STRIP]) {
    const view = await expRun(home, messages, env, `sid-${Object.keys(env).length}-${env.CONTEXT_STRIP_PROSE}`);
    assert.ok(view.length < messages.length);
    assert.deepEqual(view.slice(-2), messages.slice(-2));
  }
}));

test("prose strip removes tool pairs before the latest run, merging same-role neighbours", withHome(async (home) => {
  const messages = inFlightTurn();
  const view = await expRun(home, messages, STRIP);
  assert.ok(!idsIn(view, "tool-call").has("s3"));
  assert.ok(!idsIn(view, "tool-result").has("s3"));
  assert.ok(idsIn(view, "tool-call").has("s4") && idsIn(view, "tool-result").has("s4"));
  assert.deepEqual(idsIn(view, "tool-call"), idsIn(view, "tool-result"));
  assert.deepEqual(view.slice(-2), messages.slice(-2));
  // Step 3's text merged into step 4's message, so roles still alternate.
  const roles = view.map((m) => m.role);
  assert.ok(roles.every((r, i) => i === 0 || r !== roles[i - 1]), roles.join());
  assert.deepEqual(view.at(-4).content.map((p) => p.type), ["text", "text", "tool-call"]);
  // The pointer counts every message absent from the view: more than the cut alone drops.
  const off = await expRun(home, messages, OFF, "other");
  assert.ok(pointerCount(view) > pointerCount(off));
  // The stripped call is stored out of context.
  assert.match(dbRows(home, "in_context = 0 AND kind = 'tool_use'").map((r) => r.text).join(" "), /make build/);
}));

test("masking runs under default settings: results masked, calls kept", withHome(async (home) => {
  const messages = inFlightTurn(LONG);
  const view = await expRun(home, messages);
  const off = await expRun(home, messages, OFF, "other");
  assert.deepEqual(idsIn(view, "tool-call"), idsIn(off, "tool-call"));
  const results = resultsOf(view);
  assert.equal(results.s3, maskPlaceholder(`built${LONG}`));
  assert.equal(results.s4, `contents${LONG}`);
  assert.equal(resultsOf(off).s3, `built${LONG}`);
  assert.deepEqual(view.map((m) => m.role), off.map((m) => m.role));
  assert.deepEqual(view.slice(-2), messages.slice(-2));
  // The masked result's text is stored out of context; its call stays in.
  const out = dbRows(home, "in_context = 0 AND kind = 'tool_result'").map((r) => r.text);
  assert.ok(out.includes(`built${LONG}`));
  assert.ok(!out.includes(`contents${LONG}`));
  assert.match(dbRows(home, "in_context = 1 AND kind = 'tool_use'").map((r) => r.text).join(" "), /make build/);
}));

test("a masking trim that drops turns names the masked-output recovery in its pointer", withHome(async (home) => {
  const messages = inFlightTurn(LONG);
  const view = await expRun(home, messages, MASK);
  const off = await expRun(home, messages, OFF, "other");
  assert.equal(pointerCount(view), pointerCount(off));
  assert.match(noteOf(view).split("Older tool outputs")[1] ?? "", /session_id="sess-1"/);
  assert.ok(!noteOf(off).includes("Older tool outputs"));
  // Short results mask nowhere, so the pointer has no recovery line.
  const short = await expRun(home, inFlightTurn(), MASK, "short");
  assert.ok(!JSON.stringify(short).includes(MASK_PREFIX));
  assert.ok(!noteOf(short).includes("Older tool outputs"));
}));

test("the progress note lists dropped then region steps in message order", withHome(async (home) => {
  const messages = inFlightTurn();
  const progress = { CONTEXT_PROGRESS_NOTE: "1" };
  const note = noteOf(await expRun(home, messages, { ...OFF, ...progress }));
  assert.match(note, /Files written or edited \(latest last\): \/src\/f0\.py, \/src\/f2\.py\n/);
  assert.match(note, /- `pytest -q tests\/` -> exit 1/);
  assert.ok(!note.includes("make build"));
  const strip = noteOf(await expRun(home, messages, { ...STRIP, ...progress }, "strip"));
  assert.match(strip, /- `pytest -q tests\/` -> exit 1\n- `make build` -> ok\n<\/system-reminder>\n*$/);
  // The default (masking) adds the masked steps too; the note goes before the recovery line.
  const masked = noteOf(await expRun(home, inFlightTurn(LONG), {}, "mask"));
  assert.match(masked, /- `make build` -> ok\nOlder tool outputs/);
}));

test("the progress note reads OpenCode and Pi tool names, trimming command whitespace", withHome(async (home) => {
  const steps = [
    ["edit", { path: "/src/a.ts", oldString: "a", newString: "b" }, "edited", false],
    ["shell", { command: "  npm test -- --runInBand\n" }, "failed", true],
    ["write", { filePath: "/src/b.ts", content: "x" }, "written", false],
    ["bash", { command: "make build" }, "built", false],
    ["read", { filePath: "/src/c.ts" }, "contents", false],
  ];
  const note = noteOf(await expRun(home, inFlightTurn("", steps), { ...STRIP, CONTEXT_PROGRESS_NOTE: "1" }));
  assert.match(note, /Files written or edited \(latest last\): \/src\/a\.ts, \/src\/b\.ts\n/);
  assert.match(note, /- `npm test -- --runInBand` -> failed\n- `make build` -> ok\n<\/system-reminder>\n*$/);
}));

test("prose strip keeps the pair of a message before a system message; masking does not", withHome(async (home) => {
  const messages = inFlightTurn(LONG);
  const at = messages.findIndex((m) => m.role === "tool" && m.content[0].id === "s3") + 1;
  messages.splice(at, 0, { role: "system", content: [{ type: "text", text: "reminder" }] });
  const strip = await expRun(home, messages, STRIP);
  assert.ok(strip.some((m) => m.role === "system"));
  assert.equal(resultsOf(strip).s3, `built${LONG}`);
  assert.equal(resultsOf(await expRun(home, messages, MASK, "mask")).s3, maskPlaceholder(`built${LONG}`));
}));

test("masked sizes let the in-turn gap keep more steps", withHome(async (home) => {
  const steps = Array.from({ length: 8 }, (_, k) => ["bash", { command: "ls" }, `r${k}`, false]);
  const messages = inFlightTurn(LONG, steps).slice(0, -2);
  const env = { CONTEXT_TRIM_AT_TOKENS: "3700", CONTEXT_TRIM_TO_TOKENS: "1850" };
  const masked = await expRun(home, messages, { ...MASK, ...env });
  const off = await expRun(home, messages, { ...OFF, ...env }, "other");
  assert.deepEqual([...idsIn(off, "tool-call")], ["s5", "s6", "s7"]);
  assert.deepEqual([...idsIn(masked, "tool-call")], ["s4", "s5", "s6", "s7"]);
}));

test("a trim target over the threshold trims as deep as one equal to it", withHome(async (home) => {
  const steps = Array.from({ length: 8 }, (_, k) => ["bash", { command: "ls" }, `r${k}`, false]);
  const messages = inFlightTurn(LONG, steps).slice(0, -2);
  const at = await expRun(home, messages, { ...OFF, CONTEXT_TRIM_AT_TOKENS: "2500", CONTEXT_TRIM_TO_TOKENS: "2500" });
  const over = await expRun(home, messages, { ...OFF, CONTEXT_TRIM_AT_TOKENS: "2500", CONTEXT_TRIM_TO_TOKENS: "10000000" },
    "other");
  assert.deepEqual([...idsIn(at, "tool-call")], ["s4", "s5", "s6", "s7"]);
  assert.deepEqual([...idsIn(over, "tool-call")], [...idsIn(at, "tool-call")]);
}));

test("an error result under KEEP_ERRORS_MAX_CHARS stays whole through masking, still an error", withHome(async (home) => {
  const steps = STEPS.map((s, k) => (k === 3 ? ["Bash", { command: "make build" }, "Exit code 2", true] : s));
  const view = await expRun(home, inFlightTurn(LONG, steps), MASK);
  const part = view.flatMap((m) => parts(m)).find((p) => p.type === "tool-result" && p.id === "s3");
  // CONTEXT_KEEP_ERRORS (3 by default) keeps the error result whole through masking.
  assert.deepEqual(part.result, { type: "error", value: `Exit code 2${LONG}` });
}));

// CONTEXT_CLEAR_INPUTS_OVER_CHARS: same contract as proxy/tests/test_clear_inputs.py.
const BAR = 2000;
const fileBody = (k, n = 6000) => `file ${k}\n${"w".repeat(n)}`;

function writeTurns(steps = 5, n = 6000) {
  const messages = [textMsg("user", "the task")];
  for (let k = 0; k < steps; k++) {
    const input = { file_path: `/app/f${k}.py`, content: fileBody(k, n), mode: 420, dry: false,
      meta: { notes: ["short", "n".repeat(n / 2)], tag: "t" } };
    messages.push({ role: "assistant", content: [{ type: "text", text: `step ${k}` }, { type: "tool-call", id: `w${k}`, name: "write", input }] });
    messages.push({ role: "tool", content: [{ type: "tool-result", id: `w${k}`, name: "write", result: { type: "text", value: "written" } }] });
  }
  messages.push({ role: "assistant", content: [{ type: "tool-call", id: "wL", name: "bash", input: { command: "ls" } }] });
  messages.push({ role: "tool", content: [{ type: "tool-result", id: "wL", name: "bash", result: { type: "text", value: "ok" } }] });
  return messages;
}

const inputsOf = (view) => Object.fromEntries(view.flatMap((m) => parts(m).filter((p) => p.type === "tool-call").map((p) => [p.id, p.input])));
const stringsOf = (v) => (typeof v === "string" ? [v] : Array.isArray(v) ? v.flatMap(stringsOf)
  : v && typeof v === "object" ? Object.values(v).flatMap(stringsOf) : []);

// Clearing results and inputs alone lands under this fraction of the request, clearing results alone does not.
const SETTLE = 0.5;

async function inputsRun(home, messages, env = {}, sid = SID) {
  const settings = trimSettings({ CONTEXT_TRIM_AT_TOKENS: String(requestChars(messages)), CONTEXT_CLEAR_KEEP_RESULTS: "2",
    CONTEXT_TRIM_TO_TOKENS: String(Math.floor(SETTLE * requestChars(messages))), CONTEXT_CHARS_PER_TOKEN: "1",
    CONTEXT_COMPRESS_OVER_TOKENS: "0", CONTEXT_STANDING_INSTRUCTION: "0", CONTEXT_PROGRESS_NOTE: "0",
    CONTEXT_MASK_OBSERVATIONS: "0", CONTEXT_SCRATCHPAD: "0", SMARTER_COMPACT_STATE_DIR: home, ...env }, home);
  return trimContext({ sessionID: sid, messages: structuredClone(messages) }, settings);
}

test("CONTEXT_CLEAR_INPUTS_OVER_CHARS defaults to 2000 and parses like the proxy", () => {
  assert.equal(trimSettings({}, os.tmpdir()).clearInputsOverChars, 2000);
  assert.equal(trimSettings({ CONTEXT_CLEAR_INPUTS_OVER_CHARS: "500" }, os.tmpdir()).clearInputsOverChars, 500);
  assert.equal(trimSettings({ CONTEXT_CLEAR_INPUTS_OVER_CHARS: "0" }, os.tmpdir()).clearInputsOverChars, 0);
  assert.equal(trimSettings({ CONTEXT_CLEAR_INPUTS_OVER_CHARS: "-1" }, os.tmpdir()).clearInputsOverChars, 0);
  assert.equal(trimSettings({ CONTEXT_CLEAR_INPUTS_OVER_CHARS: "x" }, os.tmpdir()).clearInputsOverChars, 2000);
});

test("an input string is cleared strictly over the bar, nested ones too, other fields untouched", () => {
  for (const [n, cleared] of [[BAR - 1, false], [BAR, false], [BAR + 1, true]]) {
    assert.equal(maskedInput("a".repeat(n), BAR) !== "a".repeat(n), cleared, String(n));
  }
  // Code points, as the proxy counts: 2000 astral characters are 4000 UTF-16 units and stay.
  assert.equal(maskedInput("😀".repeat(BAR), BAR), "😀".repeat(BAR));
  const astral = "😀".repeat(BAR + 1);
  assert.equal(maskedInput(astral, BAR), inputPlaceholder(astral));
  assert.ok(inputPlaceholder(astral).includes(` ${BAR + 1} chars; smart_compact_get(id="${blobId(astral)}")`));
  const big = "b".repeat(3000);
  const value = { path: "/x", n: 5, flag: true, none: null, list: ["s", big, { deep: big }], obj: { k: big } };
  const out = maskedInput(value, BAR);
  assert.deepEqual(Object.keys(out), Object.keys(value));
  assert.deepEqual(out, { path: "/x", n: 5, flag: true, none: null,
    list: ["s", inputPlaceholder(big), { deep: inputPlaceholder(big) }], obj: { k: inputPlaceholder(big) } });
  const small = { path: "/x", list: ["s"] };
  assert.equal(maskedInput(small, BAR), small);
  const placeholder = inputPlaceholder("c".repeat(5000));
  assert.ok(placeholder.startsWith(INPUT_MASK_PREFIX));
  assert.equal(maskedInput(placeholder, 1), placeholder);
  assert.equal(maskedInput("d".repeat(50), 1), "d".repeat(50));
});

test("a call's input is cleared with its result, and clearing alone now settles the trim", withHome(async (home) => {
  const messages = writeTurns();
  const view = await inputsRun(home, messages);
  assert.deepEqual(view.map((m) => m.role), messages.map((m) => m.role));
  assert.ok(requestChars(view) < 0.5 * requestChars(messages));
  assert.ok(!JSON.stringify(view).includes("earlier messages are NOT"));
  const [got, orig] = [inputsOf(view), inputsOf(messages)];
  assert.deepEqual([got.w4, got.wL], [orig.w4, orig.wL]);
  for (let k = 0; k < 4; k++) {
    const [a, b] = [got[`w${k}`], orig[`w${k}`]];
    assert.deepEqual(Object.keys(a), Object.keys(b));
    assert.deepEqual(a, { ...b, content: inputPlaceholder(b.content), meta: { notes: ["short", inputPlaceholder(b.meta.notes[1])], tag: "t" } });
  }
  // Off, clearing results alone frees almost nothing, so the trim drops turns.
  const other = fs.mkdtempSync(path.join(os.tmpdir(), "sc-clear-"));
  try {
    const off = await inputsRun(other, messages, { CONTEXT_CLEAR_INPUTS_OVER_CHARS: "0" });
    assert.ok(off.length < messages.length);
    assert.ok(JSON.stringify(off).includes("earlier messages are NOT"));
  } finally { fs.rmSync(other, { recursive: true, force: true }); }
}));

test("cleared inputs re-render byte-identical across requests", withHome(async (home) => {
  const messages = writeTurns();
  let prev = await inputsRun(home, messages);
  let more = messages;
  for (let k = 0; k < 2; k++) {
    more = [...more,
      { role: "assistant", content: [{ type: "tool-call", id: `n${k}`, name: "write", input: { file_path: `/n${k}`, content: fileBody(k) } }] },
      { role: "tool", content: [{ type: "tool-result", id: `n${k}`, name: "write", result: { type: "text", value: "written" } }] }];
    const view = await inputsRun(home, more, { CONTEXT_TRIM_AT_TOKENS: "1000000000" });
    assert.equal(JSON.stringify(view.slice(0, prev.length)), JSON.stringify(prev));
    assert.equal(inputsOf(view)[`n${k}`].content, fileBody(k));
    prev = view;
  }
}));

test("a cleared input's blob round-trips through smart_compact_get and survives the sweep", withHome(async (home) => {
  const view = await inputsRun(home, writeTurns());
  const texts = new Set([0, 1, 2, 3].flatMap((k) => [fileBody(k), "n".repeat(3000)]));
  const placeholders = Object.values(inputsOf(view)).flatMap(stringsOf).filter((s) => s.startsWith(INPUT_MASK_PREFIX));
  assert.equal(placeholders.length, 8);
  const opts = { env: { ...process.env, HOME: home, SMARTER_COMPACT_STATE_DIR: home } };
  for (const p of placeholders) {
    const id = /smart_compact_get\(id="([0-9a-f]{16})"\)/.exec(p)[1];
    const got = await callContextStoreTool("smart_compact_get", { id, max_chars: 100_000 }, opts);
    assert.ok(got.found && texts.has(got.text), id);
  }
  const blobs = path.join(home, "context", "blobs");
  fs.writeFileSync(path.join(blobs, "ffffffffffffffff.txt"), "orphan");
  assert.deepEqual(sweepOrphanBlobs(path.join(home, "context")), ["ffffffffffffffff"]);
  assert.equal(fs.readdirSync(blobs).length, 5); // 4 file bodies, 1 shared notes string
}));

test("CONTEXT_CLEAR_INPUTS_OVER_CHARS=0 gives the old bytes", async () => {
  const messages = writeTurns();
  const views = [];
  for (const bar of ["0", "1000000000"]) {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-clear-"));
    try { views.push(await inputsRun(home, messages, { CONTEXT_CLEAR_INPUTS_OVER_CHARS: bar })); } finally { fs.rmSync(home, { recursive: true, force: true }); }
  }
  assert.ok(!JSON.stringify(views[0]).includes(INPUT_MASK_PREFIX));
  assert.equal(JSON.stringify(views[0]), JSON.stringify(views[1]));
  const orig = inputsOf(messages);
  for (const [k, v] of Object.entries(inputsOf(views[0]))) assert.deepEqual(v, orig[k], k);
});

test("masked observations clear their calls' inputs too", withHome(async (home) => {
  const view = await expRun(home, writeTurns(), { ...MASK, CONTEXT_CLEAR_KEEP_RESULTS: "0" });
  assert.ok(Object.values(inputsOf(view)).some((v) => JSON.stringify(v).includes(INPUT_MASK_PREFIX)));
}));

// CONTEXT_STRIP_OLD_REASONING: same contract as proxy/tests/test_strip_reasoning.py.
const SIG = 4000;
const sigPart = (k, n = SIG) => ({ type: "reasoning", text: "", providerMetadata: { anthropic: { signature: String(k).repeat(n) } } });
const reasonStep = (k) => [
  { role: "assistant", content: [sigPart(k), { type: "text", text: `step ${k}` }, { type: "tool-call", id: `t${k}`, name: "bash", input: { command: `echo ${k}` } }] },
  { role: "tool", content: [{ type: "tool-result", id: `t${k}`, name: "bash", result: { type: "text", value: `out ${k}` } }] }];
const reasonLoop = (steps) => [textMsg("user", "the task"), ...Array.from({ length: steps }, (_, k) => reasonStep(k)).flat()];
const fullChars = (messages) => requestChars(messages) + signedChars(messages);
const isReasoning = (p) => p.type === "reasoning";

async function reasonRun(home, messages, trimAt, env = {}, sid = SID) {
  const settings = trimSettings({ CONTEXT_TRIM_AT_TOKENS: String(trimAt), CONTEXT_TRIM_TO_TOKENS: String(Math.ceil(0.5 * trimAt)),
    CONTEXT_CLEAR_KEEP_RESULTS: "0", CONTEXT_CHARS_PER_TOKEN: "1", CONTEXT_COMPRESS_OVER_TOKENS: "0",
    CONTEXT_STANDING_INSTRUCTION: "0", CONTEXT_PROGRESS_NOTE: "0", CONTEXT_MASK_OBSERVATIONS: "0", CONTEXT_SCRATCHPAD: "0",
    SMARTER_COMPACT_STATE_DIR: home, ...env }, home);
  return trimContext({ sessionID: sid, messages: structuredClone(messages) }, settings);
}

test("CONTEXT_STRIP_OLD_REASONING defaults on and parses like the proxy", () => {
  assert.equal(trimSettings({}, os.tmpdir()).stripOldReasoning, true);
  assert.equal(trimSettings({ CONTEXT_STRIP_OLD_REASONING: "0" }, os.tmpdir()).stripOldReasoning, false);
});

test("signedChars counts the signed payloads requestChars leaves out", () => {
  assert.equal(signedChars([sigPart(1, 10), { type: "reasoning", text: "", providerMetadata: { anthropic: { redactedData: "d".repeat(7) } } }]), 17);
  assert.equal(signedChars({ type: "reasoning", text: "plain" }), 0);
});

test("a single-turn loop strips reasoning from all but the latest message, dropping none", withHome(async (home) => {
  const messages = reasonLoop(5);
  const view = await reasonRun(home, messages, fullChars(messages) - 1);
  assert.deepEqual(view.map((m) => m.role), messages.map((m) => m.role));
  assert.ok(view[0].content[0].text.startsWith(wrapNote(reasoningText(SID))));
  assert.deepEqual(view[0].content.slice(1), messages[0].content);
  for (let j = 1; j < 9; j += 2) {
    assert.deepEqual(view[j].content, messages[j].content.filter((p) => !isReasoning(p)));
    assert.deepEqual(view[j + 1], messages[j + 1]);
  }
  assert.equal(JSON.stringify(view.slice(9)), JSON.stringify(messages.slice(9)));
}));

test("a message holding only reasoning keeps it", withHome(async (home) => {
  const messages = reasonLoop(2);
  messages.splice(3, 0, { role: "assistant", content: [sigPart("R", 100)] }, textMsg("user", "go on"));
  messages.push(...reasonStep(9));
  const view = await reasonRun(home, messages, fullChars(messages) - 1);
  assert.equal(view.length, messages.length);
  assert.deepEqual(view[3], messages[3]);
  assert.ok(!view[1].content.some(isReasoning));
  assert.equal(JSON.stringify(view.slice(-2)), JSON.stringify(messages.slice(-2)));
}));

test("the stripped view re-renders byte-identical and later reasoning stays", withHome(async (home) => {
  const messages = reasonLoop(5);
  const first = await reasonRun(home, messages, fullChars(messages) - 1);
  const more = [...messages, ...reasonStep(5), ...reasonStep(6)];
  const later = await reasonRun(home, more, 10 * fullChars(more));
  assert.equal(JSON.stringify(later.slice(0, first.length)), JSON.stringify(first));
  assert.equal(JSON.stringify(later.slice(first.length)), JSON.stringify(more.slice(first.length)));
}));

test("off keeps the old rule: signatures uncharged, a message left empty dropped", withHome(async (home) => {
  const messages = reasonLoop(5);
  const at = requestChars(messages) + Math.floor(signedChars(messages) / 2);
  assert.equal(JSON.stringify(await reasonRun(home, messages, at, { CONTEXT_STRIP_OLD_REASONING: "0" })), JSON.stringify(messages));
  assert.notEqual(JSON.stringify(await reasonRun(home, messages, at, {}, "sid-on")), JSON.stringify(messages));
  const empty = [textMsg("user", "task"), ...reasonStep(0), { role: "assistant", content: [sigPart("R", 2)] },
    textMsg("user", "now this"), ...reasonStep(1), ...reasonStep(2)];
  const off = await reasonRun(home, empty, 1, { CONTEXT_STRIP_OLD_REASONING: "0" }, "sid-off");
  assert.ok(!off.some((m) => JSON.stringify(m) === JSON.stringify(empty[3])));
  const on = await reasonRun(home, empty, 1, {}, "sid-on2");
  assert.ok(on.some((m) => JSON.stringify(m) === JSON.stringify(empty[3])));
}));

test("unsigned, OpenAI and Gemini reasoning parts stay where the provider needs them", withHome(async (home) => {
  const kept = [{ type: "reasoning", text: "deepseek thought" },
    { type: "reasoning", text: "o", providerMetadata: { openai: { reasoningEncryptedContent: "gAAAA" } } },
    { type: "reasoning", text: "g", providerMetadata: { google: { thoughtSignature: "TS" } } }];
  const messages = reasonLoop(4);
  for (const j of [1, 3, 5]) messages[j].content.unshift(kept[(j - 1) / 2]);
  const view = await reasonRun(home, messages, fullChars(messages) - 1);
  assert.equal(view.length, messages.length);
  for (const j of [1, 3, 5]) assert.deepEqual(view[j].content.filter(isReasoning), [kept[(j - 1) / 2]]);
  assert.equal(JSON.stringify(view.slice(-2)), JSON.stringify(messages.slice(-2)));
}));

// CONTEXT_REUSE_RULES: static don't-repeat-work rules end the standing instruction (Hu et al., arXiv 2609.30725).
// Same contract as proxy/tests/test_reuse_rules.py.
const OLD_STANDING = "Context management: to fit your context window, older turns may be removed and replaced by an "
  + "[smarter-compact: …] pointer. Removed content is saved, not forgotten. When you need earlier content you cannot see in your context — values, ids, keys, counts, decisions — call "
  + "smart_compact_search (use `pattern` for exact ids/keys, `context` for neighbouring rows; `line_matches` gives "
  + "exact counts) or smart_compact_sql for counts and aggregates over the session store; never guess "
  + "from memory you cannot see. This trim never summarizes removed content — a remembered \"summary of earlier "
  + "context\" is not its record; cite a search result instead.";

test("reuse rules end the standing instruction; CONTEXT_REUSE_RULES=0 gives the old bytes", () => {
  assert.equal(trimSettings({}, os.tmpdir()).reuseRules, true);
  assert.equal(trimSettings({ CONTEXT_REUSE_RULES: "0" }, os.tmpdir()).reuseRules, false);
  assert.ok(REUSE_RULES.split("\n").length <= 3);
  for (const name of ["Bash", "Read", "Grep", "Glob", "Edit", "Write", "Task", "mcp__", "Claude"]) assert.ok(!REUSE_RULES.includes(name), name);
  assert.equal(standingInstruction(undefined, false), OLD_STANDING);
  assert.equal(standingInstruction(), `${OLD_STANDING}\n${REUSE_RULES}`);
  const off = [];
  addStandingInstruction(off, { enabled: true, standingInstruction: true, reuseRules: false });
  assert.deepEqual(off, [{ type: "text", text: OLD_STANDING }]);
  const on = [];
  addStandingInstruction(on, trimSettings({}, os.tmpdir()));
  assert.deepEqual(on, [{ type: "text", text: `${OLD_STANDING}\n${REUSE_RULES}` }]);
});

// CONTEXT_DEDUP_RESULTS: same contract as proxy/tests/test_dedup_results.py.
const DUP = "line of output\n".repeat(40);

function dupTurns(outs) {
  const messages = [{ role: "user", content: [{ type: "text", text: "the task" }] }];
  outs.forEach((out, k) => {
    messages.push({ role: "assistant", content: [{ type: "tool-call", id: `p${k}`, name: "bash", input: { command: "cat log.txt" } }] });
    messages.push({ role: "tool", content: [{ type: "tool-result", id: `p${k}`, name: "bash", result: { type: "text", value: out } }] });
  });
  return messages;
}

async function dupRun(home, messages, env = {}) {
  const settings = trimSettings({ CONTEXT_COMPRESS_OVER_TOKENS: "0", CONTEXT_STANDING_INSTRUCTION: "0", CONTEXT_PROGRESS_NOTE: "0",
    CONTEXT_MASK_OBSERVATIONS: "0", CONTEXT_SCRATCHPAD: "0", CONTEXT_CLEAR_KEEP_RESULTS: "0", CONTEXT_STRIP_OLD_REASONING: "0",
    CONTEXT_CHARS_PER_TOKEN: "1", SMARTER_COMPACT_STATE_DIR: home, ...env }, home);
  return trimContext({ sessionID: SID, messages: structuredClone(messages) }, settings);
}

test("a new result identical to one still shown becomes a pointer; only the newer copy changes", withHome(async (home) => {
  const pointer = dedupPlaceholder(DUP);
  assert.ok(pointer.startsWith(DEDUP_PREFIX) && pointer.length < DEDUP_MIN_CHARS);
  assert.equal(trimSettings({}, os.tmpdir()).dedupResults, true);
  assert.deepEqual(await dupRun(home, dupTurns([DUP])), dupTurns([DUP]));
  const messages = dupTurns([DUP, DUP]);
  const view = await dupRun(home, messages);
  assert.deepEqual(view.slice(0, 4), messages.slice(0, 4));
  assert.deepEqual(resultsOf(view), { p0: DUP, p1: pointer });
  // Every later request renders the same bytes.
  const later = [...messages, { role: "assistant", content: [{ type: "text", text: "done" }] }, { role: "user", content: [{ type: "text", text: "next" }] }];
  assert.equal(JSON.stringify((await dupRun(home, later)).slice(0, view.length)), JSON.stringify(view));
  // The blob round-trips through smart_compact_get and survives the sweep.
  const opts = { env: { ...process.env, HOME: home, SMARTER_COMPACT_STATE_DIR: home } };
  assert.deepEqual(sweepOrphanBlobs(path.join(home, "context")), []);
  const got = await callContextStoreTool("smart_compact_get", { id: blobId(DUP), max_chars: 100_000 }, opts);
  assert.ok(got.found);
  assert.equal(got.text, DUP);
  const state = JSON.parse(fs.readFileSync(path.join(home, "context", "sessions", `${SID}.trim.json`), "utf8"));
  assert.deepEqual(state.dedup.map((d) => [d.at, d.id, d.src, d.bid]), [[4, "p1", "p0", blobId(DUP)]]);
}));

test("with the ledger on, [DUP] then [DUP, DUP] gives the proxy's pointer and dedup decision", withHome(async (home) => {
  // Same literal as proxy/tests/test_dedup_results.py PROXY_POINTER: both sides render these bytes.
  const literal = '[smarter-compact: output identical to an earlier tool result still shown above; smart_compact_get(id="646c9063dd68a784") returns it]';
  await dupRun(home, dupTurns([DUP]), { CONTEXT_LEDGER: "1" });
  const view = await dupRun(home, dupTurns([DUP, DUP]), { CONTEXT_LEDGER: "1" });
  assert.equal(resultsOf(view).p1, literal);
  const state = JSON.parse(fs.readFileSync(path.join(home, "context", "sessions", `${SID}.trim.json`), "utf8"));
  assert.deepEqual(state.dedup.map((d) => [d.kind ?? null, d.at, d.id, d.src, d.bid]), [[null, 4, "p1", "p0", blobId(DUP)]]);
}));

test("CONTEXT_DEDUP_RESULTS=0 gives the old bytes, and short or already-sent duplicates stay", withHome(async (home) => {
  const messages = dupTurns([DUP, DUP]);
  assert.equal(JSON.stringify(await dupRun(home, messages, { CONTEXT_DEDUP_RESULTS: "0" })), JSON.stringify(messages));
  await dupRun(home, messages);
  assert.equal(JSON.stringify(await dupRun(home, messages, { CONTEXT_DEDUP_RESULTS: "0" })), JSON.stringify(messages));
  const short = "x".repeat(DEDUP_MIN_CHARS - 1);
  assert.deepEqual(await dupRun(home, dupTurns([short, short])), dupTurns([short, short]));
  const sent = [...dupTurns([DUP, DUP]), { role: "assistant", content: [{ type: "text", text: "done" }] }];
  sent[2].content[0].id = sent[1].content[0].id = "q0";
  sent[4].content[0].id = sent[3].content[0].id = "q1";
  assert.deepEqual(await dupRun(home, sent), sent);
}));

test("the pointer stays when the earlier copy is later cleared or dropped", withHome(async (home) => {
  await dupRun(home, dupTurns([DUP, DUP]));
  const messages = dupTurns([DUP, DUP, `other ${"y".repeat(300)}`, `more ${"z".repeat(300)}`]);
  const env = { CONTEXT_CLEAR_KEEP_RESULTS: "1", CONTEXT_TRIM_AT_TOKENS: "1500", CONTEXT_TRIM_TO_TOKENS: "1450" };
  const cleared = await dupRun(home, messages, env);
  const results = resultsOf(cleared);
  assert.ok(results.p0.startsWith(MASK_PREFIX));
  assert.equal(results.p1, dedupPlaceholder(DUP));
  assert.equal(JSON.stringify(await dupRun(home, messages, env)), JSON.stringify(cleared));
  // A trim that drops the earlier copy's turn (a fresh session, so no earlier cut) still renders the same pointer.
  const other = fs.mkdtempSync(path.join(os.tmpdir(), "sc-dedup-"));
  let dropped;
  try {
    await dupRun(other, dupTurns([DUP, DUP]));
    dropped = await dupRun(other, messages, { ...env, CONTEXT_TRIM_AT_TOKENS: "1000", CONTEXT_TRIM_TO_TOKENS: "900" });
  } finally { fs.rmSync(other, { recursive: true, force: true }); }
  assert.ok(!("p0" in resultsOf(dropped)));
  assert.equal(resultsOf(dropped).p1, dedupPlaceholder(DUP));
}));

test("an earlier copy not shown in full gets no pointer", withHome(async (home) => {
  const messages = dupTurns([DUP, DUP]);
  const view = await dupRun(home, messages, { CONTEXT_CLEAR_KEEP_RESULTS: "1", CONTEXT_TRIM_AT_TOKENS: "1" });
  const results = resultsOf(view);
  assert.ok(!("p0" in results) || results.p0.startsWith(MASK_PREFIX));
  assert.equal(results.p1, DUP);
}));
