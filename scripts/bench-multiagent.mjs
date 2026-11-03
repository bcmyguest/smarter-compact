#!/usr/bin/env node
/**
 * Multi-agent long-session benchmark: one long OpenCode 2 coding session that
 * spawns subagents repeatedly, forces smarter-compact trims mid-session, and
 * verifies (a) the session completes, (b) trims happened, (c) trimmed content
 * is retrievable through the self-registered smart_compact_search tools.
 *
 * BENCH_PROVIDER / BENCH_MODEL select the endpoint (same as bench-opencode2.mjs).
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fileURLToPath } from "node:url";
import { parseJsonc } from "../plugin/lib/opencode-config.mjs";
import { contextRoot } from "../plugin/lib/context-store.mjs";
import { summarizeEvents } from "../plugin/lib/session-db.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const OC2 = process.env.OPENCODE2_BIN || path.join(os.homedir(), ".opencode", "bin", "opencode2");
const HOST_CONFIG = path.join(os.homedir(), ".config", "opencode", "opencode.json");
const TRIM_AT = Number(process.env.BENCH_TRIM_AT_TOKENS || 4000);
const BENCH_PROVIDER = process.env.BENCH_PROVIDER || "";
const BENCH_MODEL = process.env.BENCH_MODEL || "";
const N_BUGS = Number(process.env.BENCH_N_BUGS || 6);
const SEED = `SEED-${Math.random().toString(36).slice(2, 8).toUpperCase()}`;

function run(cmd, args, env, { timeoutMs = 900_000, cwd } = {}) {
  return spawnSync(cmd, args, { encoding: "utf8", env, timeout: timeoutMs, maxBuffer: 32 * 1024 * 1024, cwd });
}
function fail(msg) { console.error(`FAIL  ${msg}`); process.exit(1); }
function ok(msg) { console.log(`ok    ${msg}`); }

if (!BENCH_PROVIDER || !BENCH_MODEL) fail("set BENCH_PROVIDER and BENCH_MODEL");
const host = parseJsonc(fs.readFileSync(HOST_CONFIG, "utf8"));
const providerCfg = host?.provider?.[BENCH_PROVIDER];
if (!providerCfg?.options?.baseURL) fail(`host opencode.json has no provider.${BENCH_PROVIDER}`);

// --- scratch project: N modules, each with a planted off-by-one bug + a test runner ---
const project = fs.mkdtempSync(path.join(os.tmpdir(), "sc-ma-proj-"));
fs.mkdirSync(path.join(project, "app"), { recursive: true });
const bugs = [];
for (let i = 1; i <= N_BUGS; i++) {
  bugs.push({ mod: `mod${i}`, correct: 100 + i, wrong: 99 + i });
  fs.writeFileSync(path.join(project, "app", `mod${i}.py`),
    `"""Module ${i}: the ${"widget " + i + " "}calculator. Bug: the base constant is off by one."""\nBASE = ${99 + i}\n\n\ndef compute():\n    return BASE\n`);
  fs.writeFileSync(path.join(project, "app", `test_mod${i}.py`),
    `from app.mod${i} import compute\n\n\ndef test_compute():\n    assert compute() == ${100 + i}\n`);
}
fs.writeFileSync(path.join(project, "app", "__init__.py"), "");
fs.writeFileSync(path.join(project, "run_tests.sh"), "#!/bin/sh\ncd \"$(dirname \"$0\")\" && python3 -m pytest -q app\n");
fs.writeFileSync(path.join(project, "BUGS.md"), bugs.map((b, i) =>
  `## Bug ${i + 1} (${b.mod}.py)\nThe ${b.mod} widget must compute ${b.correct}. A subagent must locate it; you apply the fix; then run that test file only.`).join("\n") + "\n");

const git = (args) => spawnSync("git", args, { cwd: project, encoding: "utf8" });
git(["init", "-q"]); git(["add", "-A"]); git(["-c", "user.email=bench@local", "user.name=bench", "commit", "-qm", "seed"]);

// --- isolated HOME with the plugin (tools self-register via ctx.mcp.add) ---
const benchHome = fs.mkdtempSync(path.join(os.tmpdir(), "sc-ma-home-"));
const stateDir = path.join(benchHome, ".local", "share", "smarter-compact");
const configDir = path.join(benchHome, ".config", "opencode");
fs.mkdirSync(configDir, { recursive: true });
fs.writeFileSync(path.join(configDir, "opencode.json"), JSON.stringify({
  $schema: "https://opencode.ai/config.json",
  plugins: [ROOT],
  provider: { [BENCH_PROVIDER]: providerCfg },
}, null, 2) + "\n");

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

const runBase = ["run", "--standalone", "--print-logs", "--auto", "--format", "json", "--model", `${BENCH_PROVIDER}/${BENCH_MODEL}`];
// cwd MUST be the scratch project — without it the agent inherits the bench's own repo.
const turn = (prompt, args = []) => run(OC2, [...runBase, ...args, prompt], env, { cwd: project });
const continueTurn = (prompt) => turn(prompt, ["--continue"]);

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

console.log(`project ${project}`);
console.log(`home    ${benchHome}`);
console.log(`model   ${BENCH_PROVIDER}/${BENCH_MODEL} trimAt=${TRIM_AT} seed=${SEED}`);

// turn 1: plant the secret + the overall task
const t1 = turn(
  `Remember this session secret: ${SEED}. Later I will ask you to recall it. Now read BUGS.md in this repository. Fix ALL ${N_BUGS} bugs. For EACH bug you MUST first spawn a subagent (the "task" tool) to locate and describe the exact faulty line, and only then apply the fix yourself and run that module's test file with python3 -m pytest. Do them one by one. Start with bug 1 only; I will prompt you for each subsequent bug.`
, );
if (t1.status !== 0) { console.error(t1.stderr || t1.stdout); fail("turn 1 failed"); }
const sessionId = sessionIdFromJson(t1.stdout);
if (!sessionId) fail("no session id");
ok(`session ${sessionId}`);

// work turns: drive bug-by-bug so the session grows and trims fire
for (let i = 2; i <= N_BUGS; i++) {
  const t = continueTurn(`Good. Now handle bug ${i}: spawn a subagent to locate the faulty line in app/mod${i}.py, apply the fix, run python3 -m pytest -q app/test_mod${i}.py, and report the result. Keep going until that test passes.`);
  if (t.status !== 0) { console.error(t.stderr || t.stdout); fail(`turn ${i} failed`); }
  ok(`work turn ${i}/${N_BUGS} done`);
}

// verify turn: tests all pass
const verify = continueTurn("Run ./run_tests.sh and reply exactly VERIFIED if everything passes, otherwise say what failed.");
const verifyOut = `${verify.stdout}\n${verify.stderr}`;
const verified = /VERIFIED/i.test(verifyOut);
ok(`final tests verified by agent: ${verified}`);

// recall turn: the seed is many turns and trims back now
const recall = continueTurn("What was the session secret you were told to remember at the very start? If it is not in your context, search for it with smart_compact_search. Answer with the secret only.");
const recallOut = `${recall.stdout}\n${recall.stderr}`;
const recalled = recallOut.includes(SEED);
ok(`secret recalled after trims: ${recalled}`);

const storeDir = path.join(contextRoot(benchHome, env), "sessions");
const summary = summarizeEvents(storeDir);

// Subagent invocations live in the session DBs: a tool_use row whose input
// JSON carries {"agent":"<name>"...}. The JSON stream does not reliably show
// tool inputs, so count from the store, not stdout.
function countSubagentCalls(dir) {
  let total = 0
  for (const f of fs.readdirSync(dir).filter((n) => n.endsWith(".db"))) {
    let db
    try {
      db = new DatabaseSync(path.join(dir, f), { readOnly: true })
      total += db.prepare(
        "SELECT COUNT(*) AS c FROM messages WHERE kind = 'tool_use' AND text LIKE '%\"agent\":\"%'",
      ).get().c
    } catch { /* unreadable db */ } finally { db?.close() }
  }
  return total
}
const totalTaskCalls = countSubagentCalls(storeDir);
console.log("\n--- multi-agent benchmark summary ---");
console.log(`session:        ${sessionId}`);
console.log(`store:          sessions=${summary.sessions} messages=${summary.messages} trims=${summary.trims}`);
console.log(`subagent calls: ${totalTaskCalls} (from session DBs)`);
console.log(`tests verified: ${verified}`);
console.log(`recall after trim: ${recalled}`);

let bad = 0;
if (summary.trims < 1) { console.error("FAIL  no trims fired — session too short for the trim threshold"); bad++; }
if (totalTaskCalls < 2) { console.error("FAIL  subagents were not used (expected >= 2 task-tool calls)"); bad++; }
if (!recalled) { console.error("FAIL  session secret not recalled via search"); bad++; }
if (!verified) { console.error("NOTE  agent did not report all tests passing — checking repo");
  const pytest = spawnSync("sh", [path.join(project, "run_tests.sh")], { encoding: "utf8" });
  if (pytest.exitCode === 0) ok("run_tests.sh passes on the working tree (agent report was wrong)");
  else { console.error(pytest.stdout + pytest.stderr); bad++; }
}
if (bad) fail(`${bad} failing(s) — see above`);
console.log(`\nPASS  multi-agent long session with trims (${BENCH_PROVIDER}/${BENCH_MODEL})`);
console.log(`state: ${stateDir}\nproject: ${project}`);
