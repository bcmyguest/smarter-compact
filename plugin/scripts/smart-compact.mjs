#!/usr/bin/env node
/**
 * smart-compact status | compact | enable | disable | proxy
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DEFAULT_PORT, ensureProductEnv, PRODUCT, PRODUCT_NAME, ROOT_ENV, stateParent } from "../lib/brand.mjs";
import { contextRoot, writeForceFlag } from "../lib/context-store.mjs";
import { AGENTS, configureAgent, ensureKey } from "../lib/enable.mjs";
import { summarizeEvents } from "../lib/session-db.mjs";

ensureProductEnv();

const HERE = path.dirname(fileURLToPath(import.meta.url));

function repoRoot() {
  const candidate = path.resolve(HERE, "../..");
  if (fs.existsSync(path.join(candidate, "proxy", "openmem_context_hook.py"))) return candidate;
  const fromEnv = process.env[ROOT_ENV];
  if (fromEnv && fs.existsSync(path.join(fromEnv, "proxy", "openmem_context_hook.py"))) return fromEnv;
  return null;
}

function usage() {
  return `${PRODUCT} status
${PRODUCT} compact --session <id>
${PRODUCT} enable <claude|codex|opencode>
${PRODUCT} disable <claude|codex|opencode>
${PRODUCT} proxy`;
}

function status() {
  const home = os.homedir();
  const sessions = path.join(contextRoot(home, process.env), "sessions");
  const summary = summarizeEvents(sessions);
  const lines = [
    `${PRODUCT_NAME}`,
    `store: ${stateParent()}`,
    `sessions: ${summary.sessions}  trims: ${summary.trims}  tokens saved (estimated): ${summary.tokensSaved}`,
    `messages: ${summary.messages}  out of context: ${summary.outOfContext}  fail-opens: ${summary.failOpens}`,
  ];
  if (summary.lastErrors.length) {
    lines.push("recent fail-opens:");
    for (const err of summary.lastErrors) lines.push(`  ${err.at} ${err.session} ${err.error} ${err.location}`);
  }
  console.log(lines.join("\n"));
}

function compact(argv) {
  const i = argv.indexOf("--session");
  const sid = i >= 0 ? argv[i + 1] : "";
  if (!sid) {
    console.error(`${PRODUCT} compact needs --session <id>`);
    process.exit(2);
  }
  const file = writeForceFlag({ home: os.homedir(), env: process.env, sessionId: sid });
  console.log(file);
}

function enable(argv, enabled) {
  const agent = argv[0];
  if (!AGENTS.includes(agent)) {
    console.error(usage());
    process.exit(2);
  }
  const result = configureAgent(os.homedir(), agent, { enabled, port: Number(process.env.CONTEXT_PROXY_PORT) || DEFAULT_PORT });
  console.log(result.message);
  process.exit(result.ok ? 0 : 1);
}

function proxy() {
  const root = repoRoot();
  if (!root) {
    console.error(`${PRODUCT} proxy runs from a clone of the repository. Leave the clone in place, or set ${ROOT_ENV}.`);
    process.exit(1);
  }
  const home = os.homedir();
  const key = ensureKey(home);
  const port = String(Number(process.env.CONTEXT_PROXY_PORT) || DEFAULT_PORT);
  const env = {
    ...process.env,
    PYTHONPATH: path.join(root, "proxy"),
    LITELLM_MASTER_KEY: key,
    OPENMEM_STATE_DIR: stateParent(home, process.env),
    CONTEXT_COMPACTION_RECALL: process.env.CONTEXT_COMPACTION_RECALL || "0",
  };
  const config = path.join(root, "proxy", "config.yaml");
  const child = spawn("litellm", ["--config", config, "--host", "127.0.0.1", "--port", port], { env, stdio: "inherit" });
  child.on("error", (err) => {
    console.error(err.code === "ENOENT"
      ? `litellm is not on PATH. Install the dev build that includes Codex pass-through (LiteLLM v1.109.0-dev.1), then run ${PRODUCT} proxy again.`
      : err.message);
    process.exit(1);
  });
  child.on("exit", (code) => process.exit(code ?? 1));
}

const [cmd, ...rest] = process.argv.slice(2);
if (cmd === "status" || !cmd) status();
else if (cmd === "compact") compact(rest);
else if (cmd === "enable") enable(rest, true);
else if (cmd === "disable") enable(rest, false);
else if (cmd === "proxy") proxy();
else {
  console.error(usage());
  process.exit(2);
}
