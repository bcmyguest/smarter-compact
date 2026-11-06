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
import { AGENTS, configureAgent, detectInstalledAgents, ensureKey } from "../lib/enable.mjs";
import { installCursorPlugin } from "../lib/install-cursor.mjs";
import { addPluginMarketplace, PLUGIN_AGENTS } from "../lib/plugins-cli.mjs";
import { runContextCommand } from "../lib/context-config.mjs";
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
${PRODUCT} enable [-g] [<claude|codex|opencode>]
${PRODUCT} disable [-g] [<claude|codex|opencode>]
${PRODUCT} install cursor [--copy]
${PRODUCT} path
${PRODUCT} plugins add <claude|codex|copilot>
${PRODUCT} context list|set|unset
${PRODUCT} proxy`;
}

function printPath() {
  const root = repoRoot();
  if (!root) {
    console.error(`${PRODUCT} path: package root not found (reinstall smarter-compact or set ${ROOT_ENV})`);
    process.exit(1);
  }
  console.log(root);
}

function plugins(argv) {
  const sub = argv[0];
  const agent = argv[1];
  if (sub !== "add" || !PLUGIN_AGENTS.includes(agent)) {
    console.error(usage());
    process.exit(2);
  }
  const root = repoRoot();
  if (!root) {
    console.error(`${PRODUCT}: package root not found`);
    process.exit(1);
  }
  const result = addPluginMarketplace(agent, root);
  if (!result.ok) {
    console.error(`${PRODUCT}: ${result.message}`);
    process.exit(1);
  }
  console.log(result.message);
}

function status() {
  const home = os.homedir();
  const sessions = path.join(contextRoot(home, process.env), "sessions");
  const summary = summarizeEvents(sessions);
  const lines = [
    `${PRODUCT_NAME}`,
    `store: ${stateParent()}`,
    `sessions: ${summary.sessions}  trims: ${summary.trims}  tokens saved: ${summary.tokensSaved} (estimate before, billed after; ${summary.unmeasured} unmeasured)`,
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

function parseEnableArgv(argv) {
  const global = argv.includes("-g") || argv.includes("--global");
  const agents = argv.filter((a) => !a.startsWith("-"));
  return { global, agents };
}

async function enable(argv, enabled) {
  const { global, agents: named } = parseEnableArgv(argv);
  const home = os.homedir();
  let targets = named;
  if (global) {
    if (named.length) {
      console.error(`${PRODUCT}: use either -g or agent names, not both`);
      process.exit(2);
    }
    targets = detectInstalledAgents(home);
    if (!targets.length) {
      console.error(
        `${PRODUCT}: no supported agents detected (~/.claude, ~/.codex, ~/.config/opencode, or opencode on PATH)`,
      );
      process.exit(1);
    }
    console.error(`${PRODUCT}: ${enabled ? "enabling" : "disabling"} ${targets.join(", ")}`);
  } else if (targets.length !== 1 || !AGENTS.includes(targets[0])) {
    console.error(usage());
    process.exit(2);
  }

  const port = resolveProxyPort(home, process.env);
  const results = targets.map((agent) => ({ agent, ...configureAgent(home, agent, { enabled, port }) }));
  const ok = results.filter((r) => r.ok);
  const bad = results.filter((r) => !r.ok);

  if (enabled && ok.length) {
    const proxy = await ensureContextProxyRunning({ importMetaUrl: import.meta.url, waitMs: 12_000 });
    if (!proxy.ok) {
      for (const r of ok) console.log(r.message);
      console.error(`${PRODUCT}: proxy did not start (${proxy.status}${proxy.reason ? `: ${proxy.reason}` : ""}${proxy.log ? `; see ${proxy.log}` : ""})`);
      process.exit(1);
    }
    if (proxy.status === "started") console.error(`${PRODUCT}: proxy listening on http://127.0.0.1:${port}`);
    if (proxy.status === "restarted") console.error(`${PRODUCT}: proxy restarted, ${proxy.from} -> ${proxy.to}`);
    if (proxy.warning) console.error(`${PRODUCT}: ${proxy.warning}`);
  }

  if (global && enabled && process.env.SMARTER_COMPACT_INSTALL_CURSOR === "1" && fs.existsSync(path.join(home, ".cursor"))) {
    const cursor = installCursorPlugin({ importMetaUrl: import.meta.url });
    if (cursor.ok) console.error(`${PRODUCT}: cursor plugin ${cursor.mode} → ${cursor.dest} (enable in Cursor settings)`);
    else console.error(`${PRODUCT}: cursor install skipped (${cursor.message || "failed"})`);
  }

  for (const r of ok) console.log(r.message);
  for (const r of bad) console.error(`${r.agent}: ${r.message}`);
  process.exit(bad.length && !ok.length ? 1 : bad.length ? 1 : 0);
}

function install(argv) {
  const target = argv[0];
  if (target !== "cursor") {
    console.error(usage());
    process.exit(2);
  }
  const copy = argv.includes("--copy");
  const result = installCursorPlugin({ importMetaUrl: import.meta.url, copy });
  if (!result.ok) {
    console.error(result.message || `${PRODUCT} install cursor failed`);
    process.exit(1);
  }
  const note = result.message ? ` (${result.message})` : "";
  console.log(`${result.mode} → ${result.dest}${note}`);
  console.log("Reload Cursor and enable the smarter-compact local plugin.");
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
    SMARTER_COMPACT_STATE_DIR: stateParent(home, process.env),
    CONTEXT_PROXY_PORT: port,
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
else if (cmd === "install") install(rest);
else if (cmd === "path") printPath();
else if (cmd === "plugins") plugins(rest);
else if (cmd === "context") process.exit(runContextCommand(rest, { home: os.homedir() }));
else if (cmd === "proxy") proxy();
else {
  console.error(usage());
  process.exit(2);
}
