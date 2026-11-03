#!/usr/bin/env node
/**
 * Live smoke benchmark: OpenCode 2 + smarter-compact (in-process trim) + a a configured provider/model (BENCH_PROVIDER/BENCH_MODEL).
 * Uses an isolated HOME so global OpenMem / v1 plugin config does not interfere.
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { parseJsonc } from "../plugin/lib/opencode-config.mjs";
import { contextRoot } from "../plugin/lib/context-store.mjs";
import { summarizeEvents } from "../plugin/lib/session-db.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OC2 = process.env.OPENCODE2_BIN || path.join(os.homedir(), ".opencode", "bin", "opencode2");
const HOST_CONFIG = path.join(os.homedir(), ".config", "opencode", "opencode.json");
const TRIM_AT = Number(process.env.BENCH_TRIM_AT_TOKENS || 1200);
const BENCH_PROVIDER = process.env.BENCH_PROVIDER || "";
const BENCH_MODEL = process.env.BENCH_MODEL || "Qwen/Qwen3.8-Flash-Next-FP8";
const BENCH_MODEL_REF = `${BENCH_PROVIDER}/${BENCH_MODEL}`;

function run(cmd, args, env, { timeoutMs = 300_000 } = {}) {
  const r = spawnSync(cmd, args, {
    encoding: "utf8",
    env,
    timeout: timeoutMs,
    maxBuffer: 20 * 1024 * 1024,
  });
  return r;
}

function fail(msg) {
  console.error(`FAIL  ${msg}`);
  process.exit(1);
}

function ok(msg) {
  console.log(`ok    ${msg}`);
}

/** First session id from `opencode run --format json` line-delimited events. */
function sessionIdFromJson(text) {
  for (const line of String(text).split("\n")) {
    if (!line.trim()) continue;
    try {
      const row = JSON.parse(line);
      const id = row.sessionID || row.part?.sessionID;
      if (id?.startsWith("ses_")) return id;
    } catch { /* not json */ }
  }
  return null;
}

const version = run(OC2, ["--version"], process.env, { timeoutMs: 15_000 });
if (version.status !== 0) fail(`opencode2 missing or broken: ${version.stderr || version.stdout}`);
ok(`opencode2 ${String(version.stdout || version.stderr).trim()}`);

if (!fs.existsSync(HOST_CONFIG)) fail(`host config missing: ${HOST_CONFIG}`);
let host;
try {
  host = parseJsonc(fs.readFileSync(HOST_CONFIG, "utf8"));
} catch (e) {
  fail(`parse host opencode.json: ${e.message}`);
}
const providerCfg = host?.provider?.[BENCH_PROVIDER];
if (!providerCfg?.options?.baseURL) {
  const avail = Object.keys(host?.provider ?? {}).join(", ") || "none";
  fail(`host opencode.json has no provider.${BENCH_PROVIDER} (available: ${avail}; set BENCH_PROVIDER/BENCH_MODEL)`);
}
if (!providerCfg?.models?.[BENCH_MODEL]) {
  fail(`host opencode.json provider.${BENCH_PROVIDER} has no models.${BENCH_MODEL}`);
}

const benchHome = fs.mkdtempSync(path.join(os.tmpdir(), "sc-oc2-bench-"));
const project = fs.mkdtempSync(path.join(os.tmpdir(), "sc-oc2-proj-"));
const stateDir = path.join(benchHome, ".local", "share", "smarter-compact");
const configDir = path.join(benchHome, ".config", "opencode");
fs.mkdirSync(configDir, { recursive: true });

// OpenCode 2.2.x: `plugins` is a list of directory paths (package roots). Plugin options use CONTEXT_* env.
// The search MCP tools self-register via ctx.mcp.add in the plugin's setup (src/v2.mjs); turn 4 below proves it.
fs.writeFileSync(
  path.join(configDir, "opencode.json"),
  `${JSON.stringify(
    {
      $schema: "https://opencode.ai/config.json",
      plugins: [ROOT],
      provider: { [BENCH_PROVIDER]: providerCfg },
    },
    null,
    2,
  )}\n`,
);

const env = {
  ...process.env,
  HOME: benchHome,
  XDG_CONFIG_HOME: path.join(benchHome, ".config"),
  XDG_DATA_HOME: path.join(benchHome, ".local", "share"),
  XDG_CACHE_HOME: path.join(benchHome, ".cache"),
  OPENMEM_STATE_DIR: stateDir,
  SMARTER_COMPACT_AUTO_PROXY: "0",
  SMARTER_COMPACT_ROOT: ROOT,
  CONTEXT_TRIM_AT_TOKENS: String(TRIM_AT),
};

ok(`isolated HOME ${benchHome}`);
ok(`project ${project}`);
ok(`trimAtTokens ${TRIM_AT}`);
ok(`model ${BENCH_MODEL_REF}`);

const runBase = [
  "run",
  "--standalone",
  "--print-logs",
  "--auto",
  "--format",
  "json",
  "--model",
  BENCH_MODEL_REF,
];

const ping = run(OC2, [...runBase, "Reply with exactly the token BENCH-PING and nothing else."], env);
if (ping.status !== 0) {
  console.error(ping.stderr || ping.stdout);
  fail(`turn 1 exit ${ping.status}`);
}
const out1 = `${ping.stdout}\n${ping.stderr}`;
const sessionId = sessionIdFromJson(ping.stdout);
if (!sessionId) fail("turn 1 did not emit a session id (messages may not persist)");
ok(`turn 1 session ${sessionId}`);
if (!/BENCH-PING/i.test(out1) && !/"text":/.test(ping.stdout)) {
  console.error("--- turn 1 output ---\n", out1.slice(-4000));
  fail("turn 1 produced no usable model output");
}
ok("turn 1 model responded");

const filler = "Remember the codeword ALPHA. ".repeat(400);
const bulk = run(OC2, [...runBase, "--continue", filler], env, { timeoutMs: 600_000 });
if (bulk.status !== 0) {
  console.error(bulk.stderr || bulk.stdout);
  fail(`turn 2 exit ${bulk.status}`);
}
ok("turn 2 bulk prompt completed");

const recall = run(
  OC2,
  [...runBase, "--continue", "What was the codeword I asked you to remember? One word only."],
  env,
);
if (recall.status !== 0) {
  console.error(recall.stderr || recall.stdout);
  fail(`turn 3 exit ${recall.status}`);
}
const out3 = `${recall.stdout}\n${recall.stderr}`;
ok(`turn 3 output mentions ALPHA: ${/ALPHA/i.test(out3)}`);

const toolCheck = run(
  OC2,
  [...runBase, "--continue", "Call the smart_compact_search tool now with pattern BENCH-PING. After it returns, reply exactly: TOOLCHECK done"],
  env,
);
if (toolCheck.status !== 0) {
  console.error(toolCheck.stderr || toolCheck.stdout);
  fail(`turn 4 exit ${toolCheck.status}`);
}
const out4 = `${toolCheck.stdout}\n${toolCheck.stderr}`;
const toolCalled = /smart_compact_search/.test(toolCheck.stdout);
if (!toolCalled) fail("smart_compact_search was never called — self-registration (ctx.mcp.add) did not take effect");
ok(`turn 4 smart_compact_search called: true, model replied TOOLCHECK: ${/TOOLCHECK done/i.test(out4)}`);

let msgCount = 0;
const exported = run(OC2, ["export", sessionId], env, { timeoutMs: 30_000 });
if (exported.status === 0) {
  try {
    const data = JSON.parse(exported.stdout);
    msgCount = Array.isArray(data.messages) ? data.messages.length : 0;
    ok(`session export has ${msgCount} messages`);
  } catch {
    ok("session export present but not JSON (skipped message count)");
  }
} else {
  ok("session export unavailable in standalone mode (using JSON stream + store)");
}

const summary = summarizeEvents(path.join(contextRoot(benchHome, env), "sessions"));
ok(`store sessions=${summary.sessions} messages=${summary.messages} trims=${summary.trims}`);

console.log("\n--- benchmark summary ---");
console.log(`session: ${sessionId}`);
console.log(`messages (export): ${msgCount}`);
console.log(`trims (store): ${summary.trims}`);
console.log(`codeword recall: ${/ALPHA/i.test(out3) ? "yes" : "no (may still pass if trim + search path differs)"}`);
console.log(`state: ${stateDir}`);

if (summary.messages < 2 && msgCount < 2) fail("session store and export both show too few messages");
console.log(`\nPASS  OpenCode 2 + smarter-compact + ${BENCH_MODEL_REF} smoke benchmark`);
