import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";

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
