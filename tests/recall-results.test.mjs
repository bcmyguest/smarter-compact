import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DEDUP_PREFIX, MARKER_PREFIX, trimContext, trimSettings } from "../plugin/lib/context-trim.mjs";
import { callContextStoreTool, toolResultText } from "../plugin/lib/mcp/context-store.mjs";
import { rmHome } from "./rm-home.mjs";

// Results of our own recall tools reach the model whole on arrival: never compressed, log-reduced or pointered.
// Same contract as proxy/tests/test_recall_results.py. Trial path-tracing-reverse__ujsWcua: a 16 KB objdump result
// was compressed, its smart_compact_get result was compressed again, and the get of that returned escaped JSON.

const SID = "recall-sess";
const GET = "smarter-compact_smart_compact_get";
// 470 lines of disassembly, about the 16,344 chars of the trial's /tmp/main.s.
const OBJDUMP = `470 /tmp/main.s\n0000000000401740 <main>:\n${Array.from({ length: 468 },
  (_, k) => `  ${(0x401744 + 4 * k).toString(16)}:\tmovss  0x${(0x68 + k).toString(16)}(%rsp),%xmm${k % 16}`).join("\n")}`;
const LOG = ["============================= test session starts ==============================",
  ...Array.from({ length: 300 }, (_, k) => `tests/test_mod.py::test_case_${k} PASSED`),
  "FAILED tests/test_mod.py::test_last - AssertionError", "1 failed, 300 passed in 2.31s"].join("\n");

const call = (id, name, input) => ({ role: "assistant", content: [{ type: "tool-call", id, name, input }] });
const result = (id, name, value) => ({ role: "tool", content: [{ type: "tool-result", id, name, result: { type: "text", value } }] });
const resultsOf = (view) => Object.fromEntries(view.flatMap((m) => m.content.filter((p) => p.type === "tool-result")
  .map((p) => [p.id, p.result.value])));

function withHome(fn) {
  return async () => {
    const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-recall-"));
    try { await fn(home); } finally { rmHome(home); }
  };
}

async function run(home, messages, env = {}) {
  const settings = trimSettings({ CONTEXT_STANDING_INSTRUCTION: "0", CONTEXT_SCRATCHPAD: "0", CONTEXT_LEDGER: "0",
    SMARTER_COMPACT_STATE_DIR: home, ...env }, home);
  return trimContext({ sessionID: SID, messages: structuredClone(messages) }, settings);
}

/** The text the MCP server returns for smart_compact_get, as the agent sees it. */
async function getText(home, id) {
  const opts = { env: { ...process.env, HOME: home, SMARTER_COMPACT_STATE_DIR: home } };
  return toolResultText("smart_compact_get", await callContextStoreTool("smart_compact_get", { id, max_chars: 100_000 }, opts));
}

const blobs = (home) => fs.readdirSync(path.join(home, "context", "blobs")).sort();

test("a get of a compressed result stays whole and readable", withHome(async (home) => {
  const messages = [{ role: "user", content: [{ type: "text", text: "Reverse /app/mystery" }] },
    call("b0", "bash", { command: "cat /tmp/main.s" }), result("b0", "bash", OBJDUMP)];
  const compressed = resultsOf(await run(home, messages)).b0;
  assert.ok(compressed.startsWith(MARKER_PREFIX), "the bash result itself is still compressed");
  const id = /smart_compact_get\(id="([0-9a-f]{16})"\)/.exec(compressed)[1];
  assert.deepEqual(blobs(home), [`${id}.txt`]);

  const got = await getText(home, id);
  assert.equal(got, `[smart_compact_get id=${id}: characters 0-${OBJDUMP.length} of ${OBJDUMP.length}, complete]\n${OBJDUMP}`);
  messages.push(call("g1", GET, { id, max_chars: 100_000 }), result("g1", GET, got));
  assert.equal(resultsOf(await run(home, messages)).g1, got, "the get result goes out whole");
  assert.deepEqual(blobs(home), [`${id}.txt`], "no new blob for the get result");

  // The same get again (identical, over DEDUP_MIN_CHARS) gets no identical-result pointer either.
  messages.push(call("g2", GET, { id, max_chars: 100_000 }), result("g2", GET, got));
  const again = resultsOf(await run(home, messages, { CONTEXT_DEDUP_RESULTS: "1", CONTEXT_LEDGER: "1" }));
  assert.ok(again.g2.startsWith(got) && !again.g2.startsWith(DEDUP_PREFIX));
  assert.deepEqual(blobs(home), [`${id}.txt`]);
}));

test("a get of a log is not log-reduced", withHome(async (home) => {
  const messages = [{ role: "user", content: [{ type: "text", text: "Fix the test" }] },
    call("b0", "bash", { command: "pytest" }), result("b0", "bash", LOG)];
  const reduced = resultsOf(await run(home, messages, { CONTEXT_LOG_REDUCE_OVER_CHARS: "6000" })).b0;
  assert.ok(reduced.startsWith(MARKER_PREFIX), "the bash log itself is still reduced");
  const id = /smart_compact_get\(id="([0-9a-f]{16})"\)/.exec(reduced)[1];
  const got = await getText(home, id);
  messages.push(call("g1", "mcp__smarter-compact__smart_compact_get", { id }), result("g1", "mcp__smarter-compact__smart_compact_get", got));
  assert.equal(resultsOf(await run(home, messages, { CONTEXT_LOG_REDUCE_OVER_CHARS: "6000" })).g1, got);
}));

test("smart_compact_get text is one header line then the raw text, paged and not found", () => {
  const page = { id: "0123456789abcdef", found: true, total_chars: 30, offset: 10, page_chars: 5, text: "a\nb\nc", truncated: true, next_offset: 15 };
  assert.equal(toolResultText("smart_compact_get", page),
    "[smart_compact_get id=0123456789abcdef: characters 10-15 of 30; next page: offset=15]\na\nb\nc");
  assert.equal(toolResultText("smart_compact_get", { id: "0123456789abcdef", found: false, text: "" }),
    "[smart_compact_get id=0123456789abcdef: not found]");
  // Every other tool stays JSON.
  assert.equal(toolResultText("smart_compact_sql", { rows: [] }), '{"rows":[]}');
});
