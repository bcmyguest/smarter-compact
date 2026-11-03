#!/usr/bin/env node
/**
 * smarter-compact status | compact | enable | disable | proxy
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ensureProductEnv, PRODUCT, PRODUCT_NAME, ROOT_ENV, resolveProxyPort, stateParent } from "../lib/brand.mjs";
import { contextRoot, writeForceFlag } from "../lib/context-store.mjs";
import { AGENTS, configureAgent, ensureKey } from "../lib/enable.mjs";
import { ensureContextProxyRunning, packageRoot } from "../lib/proxy-daemon.mjs";
import { summarizeEvents } from "../lib/session-db.mjs";

ensureProductEnv();

const HERE = path.dirname(fileURLToPath(import.meta.url));

function repoRoot() {
  return packageRoot(import.meta.url);
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

async function enable(argv, enabled) {
  const agent = argv[0];
  if (!AGENTS.includes(agent)) {
    console.error(usage());
    process.exit(2);
  }
  const home = os.homedir();
  const port = resolveProxyPort(home, process.env);
  const result = configureAgent(home, agent, { enabled, port });
  if (!result.ok) {
    console.log(result.message);
    process.exit(1);
  }
  if (enabled) {
    const proxy = await ensureContextProxyRunning({ importMetaUrl: import.meta.url, waitMs: 12_000 });
    if (!proxy.ok) {
      console.log(result.message);
      console.error(`${PRODUCT}: proxy did not start (${proxy.status}${proxy.reason ? `: ${proxy.reason}` : ""}${proxy.log ? `; see ${proxy.log}` : ""})`);
      process.exit(1);
    }
    if (proxy.status === "started") console.error(`${PRODUCT}: proxy listening on http://127.0.0.1:${port}`);
  }
  console.log(result.message);
  process.exit(0);
}

function proxy() {
  const root = repoRoot();
  if (!root) {
    console.error(`${PRODUCT} proxy runs from the installed package or a clone. Set ${ROOT_ENV} if the proxy files moved.`);
    process.exit(1);
  }
  const home = os.homedir();
  const port = String(resolveProxyPort(os.homedir(), process.env));
  const env = {
    ...process.env,
    PYTHONPATH: path.join(root, "proxy"),
    LITELLM_MASTER_KEY: ensureKey(home),
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
else if (cmd === "enable") enable(rest, true).catch((err) => { console.error(err?.message || err); process.exit(1); });
else if (cmd === "disable") enable(rest, false).catch((err) => { console.error(err?.message || err); process.exit(1); });
else if (cmd === "proxy") proxy();
else {
  console.error(usage());
  process.exit(2);
}
