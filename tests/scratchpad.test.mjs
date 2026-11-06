import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import {
  SCRATCHPAD_MAX_CHARS, recallToolNames, scratchpadNudge, trimContext, trimSettings,
} from "../plugin/lib/context-trim.mjs";
import {
  CONTEXT_STORE_MCP_TOOLS, callContextStoreTool, contextStoreTools,
} from "../plugin/lib/mcp/context-store.mjs";

// CONTEXT_SCRATCHPAD on the OpenCode path and the MCP tool; same cases as proxy/tests/test_scratchpad.py.

const SID = "sess-1";
const NUDGE_TAG = "<compact-scratchpad-nudge>";
const BIG = "10000000";
// Any prompt passes this fraction of BIG, so the nudge fires without a trim.
const LOW = "0.000001";

const textMsg = (role, t) => ({ role, content: [{ type: "text", text: t }] });
const ask = (msgs, t) => [...msgs, textMsg("user", `next ${t}`)];
const reply = (msgs, t) => [...msgs,
  { role: "assistant", content: [{ type: "tool-call", id: `x${t}`, name: "bash", input: { command: `ls ${t}` } }] },
  { role: "tool", content: [{ type: "tool-result", id: `x${t}`, name: "bash", result: { type: "text", value: `res ${t}` } }] }];

function history(turns) {
  const out = [];
  for (let t = 0; t < turns; t++) {
    out.push(textMsg("user", `question ${t} ${"q".repeat(200)}`));
    out.push({ role: "assistant", content: [{ type: "tool-call", id: `t${t}`, name: "bash", input: { command: `echo ${t}` } }] });
    out.push({ role: "tool", content: [{ type: "tool-result", id: `t${t}`, name: "bash",
      result: { type: "text", value: `out ${t} ${"o".repeat(200)}` } }] });
    out.push(textMsg("assistant", `answer ${t}`));
  }
  return out;
}

function withHome(fn) {
  return async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-pad-"));
    try { await fn(home); } finally { fs.rmSync(home, { recursive: true, force: true }); }
  };
}

function run(home, messages, env = {}) {
  const settings = trimSettings({ CONTEXT_TRIM_AT_TOKENS: BIG, CONTEXT_TRIM_TO_FRACTION: "0.3", CONTEXT_CHARS_PER_TOKEN: "1",
    CONTEXT_COMPRESS_OVER_TOKENS: "0", CONTEXT_STANDING_INSTRUCTION: "0", CONTEXT_PROGRESS_NOTE: "0",
    CONTEXT_MASK_OBSERVATIONS: "0", SMARTER_COMPACT_STATE_DIR: home, ...env }, home);
  return trimContext({ sessionID: SID, messages: structuredClone(messages) }, settings);
}

const on = (home, messages) => run(home, messages, { CONTEXT_SCRATCHPAD: "1", CONTEXT_SCRATCHPAD_NUDGE_AT: LOW });
const ctx = (home) => path.join(home, "context");
const padFile = (home, name = `${SID}.md`) => path.join(ctx(home), "scratchpad", name);
const nudgedAt = (view) => view.flatMap((m, j) => (JSON.stringify(m).includes(NUDGE_TAG) ? [j] : []));

function force(home) {
  fs.mkdirSync(path.join(ctx(home), "sessions"), { recursive: true });
  fs.writeFileSync(path.join(ctx(home), "sessions", `${SID}.force`), "");
}

function writePad(home, text) {
  fs.mkdirSync(path.dirname(padFile(home)), { recursive: true });
  fs.writeFileSync(padFile(home), text);
}

function events(home, kind) {
  const db = new DatabaseSync(path.join(ctx(home), "sessions", `${SID}.db`));
  try { return db.prepare("SELECT * FROM events WHERE kind = ? ORDER BY id").all(kind); } finally { db.close(); }
}

test("the nudge goes on the newest item once and re-renders identically", withHome(async (home) => {
  const msgs = ask(history(3), 0);
  const view = await on(home, msgs);
  assert.deepEqual(nudgedAt(view), [msgs.length - 1]);
  const last = view.at(-1);
  assert.deepEqual(last.content.slice(0, -1), msgs.at(-1).content);
  assert.equal(last.content.at(-1).text, scratchpadNudge(SID, recallToolNames([])));
  const later = reply(msgs, 1);
  const again = await on(home, later);
  assert.deepEqual(nudgedAt(again), [msgs.length - 1]);
  assert.equal(JSON.stringify(again[msgs.length - 1]), JSON.stringify(last));
  assert.deepEqual(again.slice(msgs.length), later.slice(msgs.length));
  assert.equal(events(home, "nudge").length, 1);
}));

test("a tool message carries the nudge on its last text result", withHome(async (home) => {
  const msgs = reply(ask(history(3), 0), 1);
  const view = await on(home, msgs);
  assert.deepEqual(nudgedAt(view), [msgs.length - 1]);
  assert.match(view.at(-1).content[0].result.value, /^res 1\n\n<compact-scratchpad-nudge>/);
}));

test("no nudge below the threshold or on an assistant item", withHome(async (home) => {
  let view = await run(home, ask(history(3), 0), { CONTEXT_SCRATCHPAD: "1" });
  assert.deepEqual(nudgedAt(view), []);
  view = await on(home, history(3));
  assert.deepEqual(nudgedAt(view), []);
}));

test("the pad is injected capped, archived, and the nudge re-arms", withHome(async (home) => {
  const msgs = ask(history(4), 0);
  await on(home, msgs);
  writePad(home, `goal: X\n${"p".repeat(SCRATCHPAD_MAX_CHARS + 50)}`);
  force(home);
  const trimmed = reply(msgs, 1);
  const dumped = JSON.stringify(await on(home, trimmed));
  const kept = SCRATCHPAD_MAX_CHARS - "goal: X\n".length;
  assert.ok(dumped.includes("<compact-scratchpad>\\ngoal: X\\n"));
  assert.ok(dumped.includes(`${"p".repeat(kept)}\\n</compact-scratchpad>`));
  assert.ok(!dumped.includes("p".repeat(kept + 1)));
  assert.ok(!dumped.includes(NUDGE_TAG));
  assert.ok(!fs.existsSync(padFile(home)));
  assert.ok(fs.existsSync(padFile(home, `${SID}.1.md`)));
  assert.deepEqual(events(home, "trim").map((e) => e.scratchpad_chars), [SCRATCHPAD_MAX_CHARS]);
  // The note persists with the pad; the nudge re-arms on the next request's newest item.
  const next = await on(home, ask(trimmed, 2));
  assert.ok(JSON.stringify(next).includes("<compact-scratchpad>"));
  assert.deepEqual(nudgedAt(next), [next.length - 1]);
  assert.equal(events(home, "nudge").length, 2);
  writePad(home, "second");
  force(home);
  await on(home, reply(ask(trimmed, 2), 3));
  assert.ok(fs.existsSync(padFile(home, `${SID}.2.md`)));
}));

test("an empty or missing pad injects nothing", withHome(async (home) => {
  const msgs = ask(history(4), 0);
  force(home);
  let view = await on(home, msgs);
  assert.ok(!JSON.stringify(view).includes("<compact-scratchpad>"));
  writePad(home, "  \n");
  force(home);
  view = await on(home, reply(ask(reply(ask(reply(msgs, 1), 2), 3), 4), 5));
  assert.ok(!JSON.stringify(view).includes("<compact-scratchpad>"));
  assert.ok(fs.existsSync(padFile(home)));
  assert.deepEqual(events(home, "trim").map((e) => e.scratchpad_chars), [0, 0]);
}));

test("flag off is byte-identical to the default", async () => {
  const steps = [ask(history(4), 0)];
  steps.push(reply(steps[0], 1));
  steps.push(ask(steps[1], 2));
  const outs = {};
  for (const [name, env] of [["default", {}], ["off", { CONTEXT_SCRATCHPAD: "0", CONTEXT_SCRATCHPAD_NUDGE_AT: LOW }]]) {
    await withHome(async (home) => {
      if (name === "off") writePad(home, "state");
      const got = [];
      for (const [j, msgs] of steps.entries()) {
        if (j === 1) force(home);
        got.push(JSON.stringify(await run(home, msgs, env)));
      }
      outs[name] = got;
      if (name === "off") {
        assert.ok(fs.existsSync(padFile(home)));
        assert.equal(events(home, "trim")[0].scratchpad_chars, null);
        assert.deepEqual(events(home, "nudge"), []);
      }
    })();
  }
  assert.deepEqual(outs.off, outs.default);
  assert.ok(!outs.off.join("").includes("scratchpad"));
});

test("tool names resolve like the recall tools", () => {
  assert.equal(recallToolNames([]).scratchpad, "smart_compact_scratchpad");
  const offered = recallToolNames([{ name: "mcp__smarter-compact__smart_compact_scratchpad" }]);
  assert.deepEqual([offered.scratchpad, offered.scratchpadLoad], ["mcp__smarter-compact__smart_compact_scratchpad", ""]);
  const deferred = recallToolNames([{ name: "ToolSearch" }]);
  assert.equal(deferred.scratchpadLoad, 'ToolSearch with query "select:mcp__smarter-compact__smart_compact_scratchpad"');
  assert.match(scratchpadNudge(SID, deferred), /load it first by calling ToolSearch/);
  assert.equal(recallToolNames({ "smarter-compact.smart_compact_scratchpad": {} }).scratchpad, "smarter-compact.smart_compact_scratchpad");
  assert.equal(recallToolNames(["xsmart_compact_scratchpad"]).scratchpad, "smart_compact_scratchpad");
});

// The MCP tool.

const padEnv = (home, on = "1") => ({ HOME: home, SMARTER_COMPACT_STATE_DIR: home, CONTEXT_SCRATCHPAD: on });
const call = (home, args, on) => callContextStoreTool("smart_compact_scratchpad", { session_id: SID, ...args }, { env: padEnv(home, on) });

test("the tool is listed and callable only with CONTEXT_SCRATCHPAD on", withHome(async (home) => {
  assert.equal(contextStoreTools({}), CONTEXT_STORE_MCP_TOOLS);
  assert.equal(contextStoreTools({ CONTEXT_SCRATCHPAD: "0" }), CONTEXT_STORE_MCP_TOOLS);
  assert.deepEqual(contextStoreTools({ CONTEXT_SCRATCHPAD: "1" }).map((t) => t.name).at(-1), "smart_compact_scratchpad");
  await assert.rejects(() => call(home, { command: "view" }, "0"), /unknown tool: smart_compact_scratchpad/);
}));

test("create, view, str_replace and insert edit the pad the proxy reads", withHome(async (home) => {
  assert.deepEqual(await call(home, { command: "view" }), { session_id: SID, chars: 0, max_chars: SCRATCHPAD_MAX_CHARS, text: "" });
  await call(home, { command: "create", file_text: "goal: A\nnext: B" });
  assert.equal(fs.readFileSync(padFile(home), "utf8"), "goal: A\nnext: B");
  await call(home, { command: "str_replace", old_str: "next: B", new_str: "next: C" });
  await call(home, { command: "insert", insert_line: 1, insert_text: "fact: D" });
  await call(home, { command: "insert", insert_line: 0, insert_text: "# pad" });
  const viewed = await call(home, { command: "view" });
  assert.equal(viewed.text, "     1\t# pad\n     2\tgoal: A\n     3\tfact: D\n     4\tnext: C");
  assert.equal(fs.readdirSync(path.dirname(padFile(home))).length, 1);
}));

test("str_replace needs exactly one match and the cap leaves the pad unchanged", withHome(async (home) => {
  await call(home, { command: "create", file_text: "a a b" });
  await assert.rejects(() => call(home, { command: "str_replace", old_str: "a", new_str: "x" }), /occurs 2 times/);
  await assert.rejects(() => call(home, { command: "str_replace", old_str: "z", new_str: "x" }), /occurs 0 times/);
  await assert.rejects(() => call(home, { command: "create", file_text: "x".repeat(SCRATCHPAD_MAX_CHARS + 1) }),
    new RegExp(`would be ${SCRATCHPAD_MAX_CHARS + 1} characters, over the ${SCRATCHPAD_MAX_CHARS} cap \\(now 5\\)`));
  await assert.rejects(() => call(home, { command: "insert", insert_line: 9, insert_text: "x" }), /insert_line must be an integer 0-1/);
  await assert.rejects(() => call(home, { command: "delete" }), /command must be one of/);
  assert.equal(fs.readFileSync(padFile(home), "utf8"), "a a b");
  await call(home, { command: "create", file_text: "x".repeat(SCRATCHPAD_MAX_CHARS) });
}));

test("the tool sanitizes the session id", withHome(async (home) => {
  for (const bad of ["../x", "a/b", ".."]) {
    await assert.rejects(() => callContextStoreTool("smart_compact_scratchpad", { session_id: bad, command: "view" },
      { env: padEnv(home) }), /invalid session_id/);
  }
}));
