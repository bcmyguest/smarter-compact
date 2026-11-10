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
import { AGENTS, configureAgent, detectInstalledAgents } from "../lib/enable.mjs";
import { installCursorPlugin } from "../lib/install-cursor.mjs";
import { addPluginMarketplace, PLUGIN_AGENTS } from "../lib/plugins-cli.mjs";
import { runContextCommand } from "../lib/context-config.mjs";
import { ensureContextProxyRunning, litellmEnv, packageRoot, probeContextProxy, restartContextProxy } from "../lib/proxy-daemon.mjs";
import { ensureLitellm, litellmProblem, LITELLM_VERSION } from "../lib/litellm.mjs";
import { summarizeEvents } from "../lib/session-db.mjs";

ensureProductEnv();

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PROXY_STATUS = {
  "already-up": "proxy already running",
  stale: "proxy already running an older version",
  disabled: "proxy auto-start is off (SMARTER_COMPACT_AUTO_PROXY=0)",
  "not-needed": "no agent points at the proxy; not starting it",
};
const LITELLM_MISSING = `litellm is not on PATH. Run ${PRODUCT} enable, which installs litellm ${LITELLM_VERSION} with uv`;

function repoRoot() {
  return packageRoot(import.meta.url);
}

function usage() {
  return `${PRODUCT} status
${PRODUCT} compact --session <id>
${PRODUCT} enable [-g] [-q] [<claude|codex|opencode>]
${PRODUCT} disable [-g] [-q] [<claude|codex|opencode>]
${PRODUCT} install cursor [--copy]
${PRODUCT} path
${PRODUCT} plugins add <claude|codex|copilot>
${PRODUCT} context list|set|unset
${PRODUCT} proxy [restart]`;
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
  const quiet = argv.includes("-q") || argv.includes("--quiet") || /^(1|true|yes)$/i.test(String(process.env.SMARTER_COMPACT_QUIET || "").trim());
  const agents = argv.filter((a) => !a.startsWith("-"));
  return { global, quiet, agents };
}

async function enable(argv, enabled) {
  const { global, quiet, agents: named } = parseEnableArgv(argv);
  // Progress and info lines go to stderr; quiet keeps only errors and warnings.
  const info = (line) => { if (!quiet) console.error(`${PRODUCT}: ${line}`); };
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
    info(`${enabled ? "enabling" : "disabling"} ${targets.join(", ")}`);
  } else if (targets.length !== 1 || !AGENTS.includes(targets[0])) {
    console.error(usage());
    process.exit(2);
  }

  const port = resolveProxyPort(home, process.env);
  // Refuse before any config is written: an agent pointed at a proxy that cannot start loses every request.
  const autoProxy = !/^(0|false|no)$/i.test(String(process.env.SMARTER_COMPACT_AUTO_PROXY || "1").trim());
  if (enabled && autoProxy) info(`checking for a running proxy on 127.0.0.1:${port}`);
  if (enabled && autoProxy && !(await probeContextProxy(port))) {
    info("no proxy running; checking litellm (first run can take a few seconds)");
    const litellm = ensureLitellm({ log: info, quiet });
    if (!litellm.ok) {
      if (litellm.output) process.stderr.write(litellm.output);
      console.error(`${PRODUCT}: ${litellmProblem(litellm, PRODUCT)} No agent config changed.`);
      process.exit(1);
    }
    info(litellm.status === "installed" ? `installed litellm ${litellm.version}` : `litellm ${litellm.version} found`);
  }
  const results = targets.map((agent) => {
    info(`${enabled ? "writing" : "restoring"} ${agent} config`);
    return { agent, ...configureAgent(home, agent, { enabled, port }) };
  });
  const ok = results.filter((r) => r.ok);
  const bad = results.filter((r) => !r.ok);

  if (enabled && ok.length) {
    info("starting the proxy if needed (waits up to 30s for it to listen)");
    const proxy = await ensureContextProxyRunning({ importMetaUrl: import.meta.url });
    if (!proxy.ok) {
      for (const r of ok) console.log(r.message);
      console.error(proxy.reason === "litellm-missing"
        ? `${PRODUCT}: proxy did not start: ${LITELLM_MISSING}.`
        : `${PRODUCT}: proxy did not start (${proxy.status}${proxy.reason ? `: ${proxy.reason}` : ""}${proxy.log ? `; see ${proxy.log}` : ""})`);
      process.exit(1);
    }
    if (proxy.status === "started") info(`proxy listening on http://127.0.0.1:${port}`);
    else if (proxy.status === "restarted") info(`proxy restarted, ${proxy.from} -> ${proxy.to}`);
    else info(PROXY_STATUS[proxy.status] || `proxy ${proxy.status}`);
    if (proxy.warning) console.error(`${PRODUCT}: ${proxy.warning}`);
  }

  if (global && enabled && process.env.SMARTER_COMPACT_INSTALL_CURSOR === "1" && fs.existsSync(path.join(home, ".cursor"))) {
    const cursor = installCursorPlugin({ importMetaUrl: import.meta.url });
    if (cursor.ok) info(`cursor plugin ${cursor.mode} → ${cursor.dest} (enable in Cursor settings)`);
    else console.error(`${PRODUCT}: cursor install skipped (${cursor.message || "failed"})`);
  }

  if (!quiet) for (const r of ok) console.log(r.message);
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

async function proxyRestart() {
  const port = resolveProxyPort(os.homedir(), process.env);
  console.error(`${PRODUCT}: restarting the proxy on 127.0.0.1:${port}`);
  const r = await restartContextProxy({ importMetaUrl: import.meta.url });
  if (!r.ok) {
    console.error(r.reason === "litellm-missing" ? `${PRODUCT}: ${LITELLM_MISSING}.` : `${PRODUCT}: restart failed (${r.status}: ${r.reason})`);
    process.exit(1);
  }
  console.error(`${PRODUCT}: proxy listening on http://127.0.0.1:${port}${r.via ? ` (${r.via})` : ""}`);
}

function proxy() {
  const root = repoRoot();
  if (!root) {
    console.error(`${PRODUCT} proxy runs from the installed package or a clone. Set ${ROOT_ENV} if the proxy files moved.`);
    process.exit(1);
  }
  const home = os.homedir();
  const port = String(resolveProxyPort(os.homedir(), process.env));
  const env = litellmEnv(home, root, port, process.env);
  const config = path.join(root, "proxy", "config.yaml");
  const child = spawn("litellm", ["--config", config, "--host", "127.0.0.1", "--port", port], { env, stdio: "inherit", windowsHide: true });
  child.on("error", (err) => {
    console.error(err.code === "ENOENT"
      ? `${LITELLM_MISSING}, then run ${PRODUCT} proxy again.`
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
else if (cmd === "proxy" && rest[0] === "restart") proxyRestart().catch((err) => { console.error(err?.message || err); process.exit(1); });
else if (cmd === "proxy") proxy();
else {
  console.error(usage());
  process.exit(2);
}
