import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import {
  MASK_PLACEHOLDER,
  addStandingInstruction,
  pointerText,
  recallToolNames,
  requestChars,
  standingInstruction,
  trimContext,
  trimSettings,
} from "../plugin/lib/context-trim.mjs";

// The pointer note and the standing instruction are what make a trimmed-away past recoverable: they
// are the only text telling the model its past is stored, where to look, and that no summary exists.
// Both are duplicated in proxy/smarter_compact/core.py for the LiteLLM path, so the assertions here
// are the same contract as proxy/tests/test_context_proxy.py::StandingInstructionTests — change one,
// change the other.

const SID = "sess-1";

test("the pointer states what was removed, denies a summary, and names both tools", () => {
  const note = pointerText(SID, 3);
  assert.match(note, /^\[smarter-compact: continuing session sess-1; 3 earlier messages are NOT in your context\./);
  assert.match(note, /a gap in what you see \(missing batches, lines, keys\) is in them/);
  assert.match(note, /Nothing here is or was ever summarized: this trim wrote no summary of the dropped messages/);
  assert.match(note, /do not treat a "summary of earlier context" you seem to remember as their record/);
  assert.match(note, /The tools smart_compact_search and smart_compact_sql are available to you in this request/);
  assert.match(note, /You MUST call smart_compact_search\(session_id="sess-1", query=\.\.\. or pattern=\.\.\.\)/);
  assert.match(note, /never answer "not found", "never received" or "no search tool" without calling it/);
  assert.match(note, /Dropped user turns may hold instructions or corrections that still apply: search them \(smart_compact_search with role="user"\) before changing course\./);
  assert.match(note, /To recover a file you read earlier, search for the text you need instead of reading it again/);
  assert.match(note, /re-read only the lines you still lack \(offset\/limit\)/);
  assert.match(note, /smart_compact_sql\(session_id="sess-1", sql=\.\.\.\)\.\]$/);
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
    const settings = trimSettings({ CONTEXT_TRIM_AT_TOKENS: "1", CONTEXT_TRIM_MIN_GAIN_TOKENS: String(gain), SMARTER_COMPACT_STATE_DIR: home }, home);
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

async function clearRun(home, messages, trimAt, keep = "2", fraction = "0.3") {
  const settings = trimSettings({ CONTEXT_TRIM_AT_TOKENS: String(trimAt), CONTEXT_CLEAR_KEEP_RESULTS: keep,
    CONTEXT_TRIM_TO_FRACTION: fraction, CONTEXT_CHARS_PER_TOKEN: "1", CONTEXT_COMPRESS_OVER_TOKENS: "0",
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

test("off by default a trim drops turns and clears nothing", withHome(async (home) => {
  const messages = clearTurns(HUGE);
  const view = await clearRun(home, messages, requestChars(messages), "0");
  assert.ok(!JSON.stringify(view).includes(MASK_PLACEHOLDER));
  assert.ok(view.length < messages.length);
}));

test("clearing keeps every turn and call, the newest N results, and strips old signed reasoning", withHome(async (home) => {
  const messages = clearTurns(HUGE);
  const view = await clearRun(home, messages, requestChars(messages));
  assert.deepEqual(view.map((m) => m.role), messages.map((m) => m.role));
  const results = resultsOf(view);
  assert.equal(results.sL, "ok");
  assert.equal(results.s4, `out 4${HUGE}`);
  for (const k of ["s0", "s1", "s2", "s3"]) assert.equal(results[k], MASK_PLACEHOLDER, k);
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

test("clearing drops turns only when it is not enough", withHome(async (home) => {
  const messages = clearTurns(`\n${"x".repeat(300)}`);
  const view = await clearRun(home, messages, requestChars(messages));
  assert.ok(view.length < messages.length);
  const note = view.find((m) => m.content[0]?.text?.startsWith("[smarter-compact:")).content[0].text;
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
  const settings = trimSettings({ CONTEXT_TRIM_AT_TOKENS: "1", CONTEXT_TRIM_TO_FRACTION: "0.3", CONTEXT_CHARS_PER_TOKEN: "1",
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
  assert.equal(results.s3, MASK_PLACEHOLDER);
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
  assert.ok(!JSON.stringify(short).includes(MASK_PLACEHOLDER));
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
  assert.match(strip, /- `pytest -q tests\/` -> exit 1\n- `make build` -> ok\n*$/);
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
  assert.match(note, /- `npm test -- --runInBand` -> failed\n- `make build` -> ok\n*$/);
}));

test("prose strip keeps the pair of a message before a system message; masking does not", withHome(async (home) => {
  const messages = inFlightTurn(LONG);
  const at = messages.findIndex((m) => m.role === "tool" && m.content[0].id === "s3") + 1;
  messages.splice(at, 0, { role: "system", content: [{ type: "text", text: "reminder" }] });
  const strip = await expRun(home, messages, STRIP);
  assert.ok(strip.some((m) => m.role === "system"));
  assert.equal(resultsOf(strip).s3, `built${LONG}`);
  assert.equal(resultsOf(await expRun(home, messages, MASK, "mask")).s3, MASK_PLACEHOLDER);
}));

test("masked sizes let the in-turn gap keep more steps", withHome(async (home) => {
  const steps = Array.from({ length: 8 }, (_, k) => ["bash", { command: "ls" }, `r${k}`, false]);
  const messages = inFlightTurn(LONG, steps).slice(0, -2);
  const env = { CONTEXT_TRIM_AT_TOKENS: "3700", CONTEXT_TRIM_TO_FRACTION: "0.5" };
  const masked = await expRun(home, messages, { ...MASK, ...env });
  const off = await expRun(home, messages, { ...OFF, ...env }, "other");
  assert.deepEqual([...idsIn(off, "tool-call")], ["s5", "s6", "s7"]);
  assert.deepEqual([...idsIn(masked, "tool-call")], ["s4", "s5", "s6", "s7"]);
}));

test("a masked error result stays an error", withHome(async (home) => {
  const steps = STEPS.map((s, k) => (k === 3 ? ["Bash", { command: "make build" }, "Exit code 2", true] : s));
  const view = await expRun(home, inFlightTurn(LONG, steps), MASK);
  const part = view.flatMap((m) => parts(m)).find((p) => p.type === "tool-result" && p.id === "s3");
  assert.deepEqual(part.result, { type: "error", value: MASK_PLACEHOLDER });
}));
