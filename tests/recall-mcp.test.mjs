import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { callContextStoreTool } from "../plugin/lib/mcp/context-store.mjs";
import { openSession } from "../plugin/lib/session-db.mjs";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const server = path.join(repo, "plugin", "lib", "mcp", "server.mjs");
const bench = path.join(repo, "bench", "swebench", "run_bench.py");
const v2 = path.join(repo, "src", "v2.mjs");

/**
 * A sweep where the recall tools were never connected scores exactly like a sweep where
 * the model ignored them, and nothing in the artifacts says which. These assert the wiring
 * itself, because every one of these has silently regressed at least once.
 */
test("recall MCP server exists and lists the session-store tools", async () => {
  assert.ok(fs.existsSync(server), `missing MCP server entrypoint: ${server}`);
  const child = spawn(process.execPath, [server], {
    env: { ...process.env, OPENMEM_STATE_DIR: fs.mkdtempSync(path.join(os.tmpdir(), "sc-recall-test-")) },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const lines = [];
  // The initialize reply also contains the string "tools" (capabilities.tools), so match
  // on parsed shape, never on a substring.
  const toolsReply = (line) => {
    try {
      const tools = JSON.parse(line)?.result?.tools;
      return Array.isArray(tools) ? tools : null;
    } catch { return null; }
  };
  const got = new Promise((resolve, reject) => {
    let buf = "";
    const timer = setTimeout(() => reject(new Error(`no tools/list reply; stderr: ${child.stderr.toString().slice(0, 300)}`)), 15000);
    child.stdout.on("data", (chunk) => {
      buf += chunk.toString();
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        lines.push(line);
        if (toolsReply(line)) { clearTimeout(timer); resolve(); }
      }
    });
    child.on("error", (e) => { clearTimeout(timer); reject(e); });
  });
  child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: {} })}\n`);
  child.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} })}\n`);
  try {
    await got;
    const tools = lines.map(toolsReply).find(Boolean);
    assert.deepEqual(
      tools.map((t) => t.name).sort(),
      ["context_status", "smart_compact_get", "smart_compact_search", "smart_compact_sql"],
    );
  } finally {
    child.kill();
  }
});

test("bench writes the recall MCP server into every smart trial's config", () => {
  const src = fs.readFileSync(bench, "utf8");
  const smart = src.slice(src.indexOf('if arm == "smart":'));
  assert.match(smart.slice(0, 1400), /cfg\["mcp"\]/, "smart arm must set an explicit mcp config, not rely on ctx.mcp.add");
  assert.match(smart, /"servers"/, "OpenCode 2 takes mcp.servers; the flat v1 form is deprecated");
  assert.match(smart, /plugin\/lib\/mcp\/server\.mjs/, "smart arm must point at the real server entrypoint");
});

test("plugin reports why recall registration did or did not happen", () => {
  const src = fs.readFileSync(v2, "utf8");
  assert.doesNotMatch(
    src,
    /if \(!enabled \|\| !ctx\.mcp\?\.add \|\| !directory\) return;/,
    "registerSearchTools must not return silently: an unregistered tool must be visible in the run log",
  );
  assert.match(src, /recall MCP NOT registered reason=/, "registration failure must name its reason");
});

/**
 * The recall tools are offered from the first request, but the session id only ever arrives with a trim
 * pointer. Before any trim an agent has nothing correct to pass, and a made-up id used to return
 * `total_matches: 0` from a store that never existed — which reads as "that is not in your history".
 */
function store({ cwd, sids }) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "sc-session-id-"));
  const state = path.join(root, "state");
  const dir = path.join(state, "context", "sessions");
  fs.mkdirSync(dir, { recursive: true });
  sids.forEach((sid, i) => {
    fs.writeFileSync(path.join(dir, `${sid}.json`), JSON.stringify({
      sid, cwd, registered_at: new Date(1700000000000 + i * 1000).toISOString(),
    }));
    const db = openSession(path.join(dir, `${sid}.db`));
    db.sync([{ fingerprint: `fp-${sid}`, thread: "t", role: "tool", kind: "tool", name: null,
      text: `COUPLED_WCS_HEADER defined in ${sid}`, blob_id: null, keep: true }], null, "2023-11-14T22:13:20Z");
    db.close();
  });
  return { opts: { env: { ...process.env, HOME: root, SMARTER_COMPACT_STATE_DIR: state }, cwd } };
}

test("search: a real session id searches only that session, and says nothing extra", async () => {
  const s = store({ cwd: "/work", sids: ["ses_one", "ses_two"] });
  const r = await callContextStoreTool("smart_compact_search", { session_id: "ses_one", pattern: "COUPLED_WCS_HEADER" }, s.opts);
  assert.deepEqual(r.sessions_searched, ["ses_one"]);
  assert.ok(r.total_matches >= 1, "must find the row");
  assert.equal(r.session_id_note, undefined, "no note when the id was honourable");
});

test("search: omitting session_id still searches the workspace", async () => {
  const s = store({ cwd: "/work", sids: ["ses_one", "ses_two"] });
  const r = await callContextStoreTool("smart_compact_search", { pattern: "COUPLED_WCS_HEADER" }, s.opts);
  assert.deepEqual(r.sessions_searched, ["ses_two", "ses_one"], "newest first");
  assert.ok(r.total_matches >= 2);
});

test("search: session_id=\"current\" resolves instead of returning an empty success", async () => {
  const s = store({ cwd: "/work", sids: ["ses_only"] });
  const r = await callContextStoreTool("smart_compact_search", { session_id: "current", pattern: "COUPLED_WCS_HEADER" }, s.opts);
  assert.deepEqual(r.sessions_searched, ["ses_only"], "must search the session that exists, not one named current");
  assert.ok(r.total_matches >= 1, "must find the row it was really asking for");
  assert.match(r.session_id_note, /no session store for "current"/, "and must say it substituted");
});

test("search: one session in the workspace resolves any guessed id to it", async () => {
  const s = store({ cwd: "/work", sids: ["ses_only"] });
  const r = await callContextStoreTool("smart_compact_search", { session_id: "ses_000000deadbeef", query: "COUPLED_WCS_HEADER" }, s.opts);
  assert.deepEqual(r.sessions_searched, ["ses_only"]);
  assert.ok(r.total_matches >= 1);
  assert.ok(r.session_id_note);
});

test("search: a wrong id where several sessions exist fails, naming the ids that exist", async () => {
  const s = store({ cwd: "/work", sids: ["ses_one", "ses_two"] });
  await assert.rejects(
    () => callContextStoreTool("smart_compact_search", { session_id: "ses_nope", pattern: "COUPLED_WCS_HEADER" }, s.opts),
    /no session database for "ses_nope"; this workspace has 2: ses_two, ses_one/,
  );
});

test("search: nothing registered answers empty with a reason, not a silent zero", async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "sc-session-id-"));
  const env = { ...process.env, HOME: root, SMARTER_COMPACT_STATE_DIR: path.join(root, "state") };
  const r = await callContextStoreTool("smart_compact_search", { session_id: "current", pattern: "x" }, { env, cwd: root });
  assert.equal(r.total_matches, 0);
  assert.match(r.session_id_note, /no session registered for this workspace/);
});

test("sql: session_id=\"current\" runs against the newest session and says so", async () => {
  const s = store({ cwd: "/work", sids: ["ses_older", "ses_newer"] });
  const r = await callContextStoreTool("smart_compact_sql",
    { session_id: "current", sql: "SELECT text FROM messages" }, s.opts);
  assert.equal(r.session_id, "ses_newer");
  assert.match(JSON.stringify(r.rows), /ses_newer/, "the row must come from the newest session");
  assert.match(r.session_id_note, /searched this workspace's 2 sessions instead, and the query ran against ses_newer/,
    "two sessions matched the placeholder, so it must say which one answered");
});

test("sql: a wrong id among several sessions fails the same way search does", async () => {
  const s = store({ cwd: "/work", sids: ["ses_one", "ses_two"] });
  await assert.rejects(
    () => callContextStoreTool("smart_compact_sql", { session_id: "ses_nope", sql: "SELECT 1" }, s.opts),
    /this workspace has 2/,
  );
});
