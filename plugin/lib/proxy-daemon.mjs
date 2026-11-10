/**
 * Detached LiteLLM context proxy (same process as `smarter-compact proxy`, started in the background).
 */
import { execFile, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  configDir, ensureProductEnv, PRODUCT, PRODUCT_NAME, resolveProxyPort, ROOT_ENV, stateParent, withProxyEnvSecrets,
} from "./brand.mjs";
import { contextRoot } from "./context-store.mjs";
import { agentsPointedAtProxy, ensureKey } from "./enable.mjs";
import { onPath } from "./litellm.mjs";
import { rotateLog } from "./log-rotate.mjs";

const PROBE_TIMEOUT_MS = 1500;
const DEFAULT_WAIT_MS = 30_000; // a cold litellm start took 14 s on Linux
// A stale proxy restarts only after this long without a request, so an open session's call is not cut off.
const IDLE_MS = 60_000;
const STOP_WAIT_MS = 5_000;
const DOCKER_TIMEOUT_MS = 15_000;

function dirFromImportMeta(importMetaUrl) {
  if (!importMetaUrl) return null;
  const filePath = importMetaUrl.startsWith("file:")
    ? fileURLToPath(importMetaUrl)
    : importMetaUrl;
  return path.dirname(filePath);
}

/** Directory that contains `proxy/smarter_compact_hook.py` (npm package or clone). */
export function packageRoot(importMetaUrl) {
  const candidates = [];
  const start = dirFromImportMeta(importMetaUrl);
  if (start) {
    let dir = start;
    for (let i = 0; i < 8; i++) {
      candidates.push(dir);
      const parent = path.dirname(dir);
      if (parent === dir) break;
      dir = parent;
    }
  }
  for (const key of [ROOT_ENV, "SMART_COMPACT_ROOT"]) {
    if (process.env[key]) candidates.push(process.env[key]);
  }
  for (const candidate of candidates) {
    if (fs.existsSync(path.join(candidate, "proxy", "smarter_compact_hook.py"))) return candidate;
  }
  return null;
}

export async function probeContextProxy(port, { fetch = globalThis.fetch, timeoutMs = PROBE_TIMEOUT_MS } = {}) {
  try {
    const res = await fetch(`http://127.0.0.1:${port}/health/liveliness`, {
      signal: AbortSignal.timeout(timeoutMs),
    });
    return Boolean(res?.ok);
  } catch {
    return false;
  }
}

function proxyLogPath(home) {
  return path.join(configDir(home), "proxy.log");
}

function proxyPidPath(home) {
  return path.join(configDir(home), "proxy.pid");
}

/** Environment for a proxy this package starts: env, then `proxy.env` settings and secrets, then fixed keys. */
/**
 * LiteLLM's environment. A proxy started from an agent routed through it (a session hook in Claude Code) inherits
 * ANTHROPIC_BASE_URL pointing at itself; LiteLLM would forward every call back to its own port, so drop it, and the
 * client's x-litellm-api-key header with it.
 */
export function litellmEnv(home, root, port, env) {
  const merged = withProxyEnvSecrets(env, home);
  ensureProductEnv(merged, home);
  if (pointsAtProxy(merged.ANTHROPIC_BASE_URL, port)) delete merged.ANTHROPIC_BASE_URL;
  if (/x-litellm-api-key/i.test(merged.ANTHROPIC_CUSTOM_HEADERS || "")) delete merged.ANTHROPIC_CUSTOM_HEADERS;
  return {
    ...merged,
    PYTHONPATH: path.join(root, "proxy"),
    LITELLM_MASTER_KEY: ensureKey(home),
    SMARTER_COMPACT_STATE_DIR: stateParent(home, env),
    CONTEXT_PROXY_PORT: port,
  };
}

function pointsAtProxy(url, port) {
  try {
    const u = new URL(url);
    return ["127.0.0.1", "localhost", "[::1]"].includes(u.hostname) && u.port === String(port);
  } catch {
    return false;
  }
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    return null;
  }
}

export function packageVersion(root) {
  return root ? readJson(path.join(root, "package.json"))?.version ?? null : null;
}

/** The newest proxy-<port>.json the proxy on port wrote (proxy/smarter_compact/status.py); null when none. */
export function readProxyStatus(home, env, port) {
  const dirs = new Set([
    contextRoot(home, env),
    path.join(home, ".local", "share", "smarter-compact", "context"),
    path.join(home, ".local", "share", "openmem", "context"),
  ]);
  let best = null;
  for (const dir of dirs) {
    const s = readJson(path.join(dir, `proxy-${port}.json`));
    if (s && (!best || seen(s) > seen(best))) best = s;
  }
  return best;
}

function seen(status) {
  return Math.max(Number(status.started) || 0, Number(status.last_request) || 0);
}

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (err) {
    return err.code === "EPERM";
  }
}

/** The pid in proxy.pid when that process runs and wrote status (a reused pid is never signalled); else null. */
function ownedPid(home, status) {
  let pid;
  try {
    pid = Number.parseInt(fs.readFileSync(proxyPidPath(home), "utf8"), 10);
  } catch {
    return null;
  }
  return pid > 0 && pid === status.pid && pidAlive(pid) ? pid : null;
}

function run(cmd, args, timeout) {
  return new Promise((resolve) => {
    execFile(cmd, args, { timeout, windowsHide: true }, (err, stdout) => resolve(err ? null : String(stdout)));
  });
}

/** The Docker container publishing port: its name and compose working dir; null without one or without docker. */
async function dockerProxy(port, exec) {
  const fmt = '{{.Names}}\t{{.Label "com.docker.compose.project.working_dir"}}';
  const out = await exec("docker", ["ps", "--filter", `publish=${port}`, "--format", fmt], 5_000);
  const [name, dir] = (out || "").split("\n")[0].split("\t");
  return name ? { name, dir: dir || null } : null;
}

/** Whether pid runs litellm; guards against signalling a pid the OS reused. */
async function isLitellm(pid, exec, platform = process.platform) {
  let cmdline = null;
  if (platform === "linux") {
    try { cmdline = fs.readFileSync(`/proc/${pid}/cmdline`, "utf8"); } catch { cmdline = null; }
  } else if (platform === "win32") {
    cmdline = await exec("tasklist", ["/FI", `PID eq ${pid}`, "/FO", "CSV", "/NH"], 5_000);
  } else {
    cmdline = await exec("ps", ["-p", String(pid), "-o", "command="], 5_000);
  }
  return /litellm/i.test(cmdline || "");
}

/** Stop pid and its children; on Windows litellm.exe is a launcher, so the tree must go. */
async function killTree(pid, exec, platform = process.platform) {
  if (platform === "win32") return await exec("taskkill", ["/PID", String(pid), "/T", "/F"], 10_000) !== null;
  try {
    process.kill(pid, "SIGTERM");
    return true;
  } catch {
    return false;
  }
}

async function waitFor(want, port, fetch, ms) {
  const deadline = Date.now() + ms;
  while (Date.now() < deadline) {
    if (await probeContextProxy(port, { fetch }) === want) return true;
    await new Promise((resolve) => { setTimeout(resolve, 300); });
  }
  return false;
}

/**
 * A proxy on port that runs another package version than the installed one: restart it when it is idle and a
 * restart picks up the installed code (one this package spawned, or a compose container whose checkout matches),
 * else return a warning. Null when the proxy is current.
 */
async function refreshStale({ home, env, port, importMetaUrl, fetch, exec, waitMs, now }) {
  const root = packageRoot(importMetaUrl);
  const installed = packageVersion(root);
  if (!installed) return null;
  const status = readProxyStatus(home, env, port);
  if (status?.version === installed) return null;
  const where = `the ${PRODUCT_NAME} proxy on port ${port}`;
  if (!status) {
    return { status: "stale", warning: `${where} reports no version; run \`${PRODUCT} proxy restart\` to load ${installed}.` };
  }
  const running = status.version ?? "an unknown version";
  const idleFor = now - seen(status) * 1000;
  const busy = idleFor < IDLE_MS;
  const pid = ownedPid(home, status);
  if (pid) {
    if (busy) return { status: "stale", warning: `${where} runs ${running}, installed is ${installed}; it restarts once idle for ${IDLE_MS / 1000} s.` };
    await killTree(pid, exec);
    await waitFor(false, port, fetch, STOP_WAIT_MS);
    const spawned = spawnContextProxyDaemon(home, importMetaUrl, env);
    if (spawned.ok && await waitFor(true, port, fetch, waitMs)) return { status: "restarted", from: running, to: installed };
    return { status: "stale", warning: `${where} did not restart; see ${proxyLogPath(home)}.` };
  }
  const docker = await dockerProxy(port, exec);
  if (docker) {
    const checkout = docker.dir ? packageVersion(docker.dir) : null;
    if (checkout !== installed) {
      return { status: "stale", warning: `${where} runs ${running} from ${docker.dir || docker.name}, which is at ${checkout ?? "an unknown version"}; update that checkout to ${installed}.` };
    }
    if (busy) return { status: "stale", warning: `${where} runs ${running}, installed is ${installed}; it restarts once idle for ${IDLE_MS / 1000} s.` };
    if (await exec("docker", ["restart", docker.name], DOCKER_TIMEOUT_MS) !== null && await waitFor(true, port, fetch, waitMs)) {
      return { status: "restarted", from: running, to: installed };
    }
    return { status: "stale", warning: `${where} did not restart; run docker restart ${docker.name}.` };
  }
  return { status: "stale", warning: `${where} runs ${running}, installed is ${installed}; run \`${PRODUCT} proxy restart\` to load it.` };
}

/** Start LiteLLM detached; logs append to `~/.config/smarter-compact/proxy.log`, rotated to `.1` at 5 MB on each start. */
export function spawnContextProxyDaemon(home, importMetaUrl, env = process.env) {
  const root = packageRoot(importMetaUrl);
  if (!root) return { ok: false, reason: "no-package-root" };
  if (!onPath("litellm", env)) return { ok: false, reason: "litellm-missing" };
  const port = String(resolveProxyPort(home, env));
  fs.mkdirSync(configDir(home), { recursive: true, mode: 0o700 });
  rotateLog(proxyLogPath(home));
  const logFd = fs.openSync(proxyLogPath(home), "a");
  const config = path.join(root, "proxy", "config.yaml");
  let child;
  try {
    child = spawn("litellm", ["--config", config, "--host", "127.0.0.1", "--port", port], {
      env: litellmEnv(home, root, port, env),
      detached: true,
      windowsHide: true,
      stdio: ["ignore", logFd, logFd],
    });
  } catch (err) {
    try { fs.closeSync(logFd); } catch { /* ignore */ }
    return { ok: false, reason: err.code === "ENOENT" ? "litellm-missing" : err.message };
  }
  child.on("error", () => {});
  child.unref();
  fs.writeFileSync(proxyPidPath(home), String(child.pid), { mode: 0o600 });
  try { fs.closeSync(logFd); } catch { /* ignore */ }
  return { ok: true, pid: child.pid };
}

/**
 * Stop the proxy on the configured port and start a fresh one. Stops only a litellm this package started
 * (proxy.pid) or a Docker container publishing the port; any other listener is left alone and reported.
 */
export async function restartContextProxy({
  home = os.homedir(),
  env = process.env,
  importMetaUrl,
  waitMs = DEFAULT_WAIT_MS,
  fetch = globalThis.fetch,
  exec = run,
} = {}) {
  const port = resolveProxyPort(home, env);
  if (await probeContextProxy(port, { fetch })) {
    let pid = null;
    try { pid = Number.parseInt(fs.readFileSync(proxyPidPath(home), "utf8"), 10); } catch { /* none */ }
    const owned = pid > 0 && pidAlive(pid) && await isLitellm(pid, exec);
    if (owned) {
      await killTree(pid, exec);
      if (!await waitFor(false, port, fetch, STOP_WAIT_MS)) {
        return { ok: false, status: "stop-failed", reason: `pid ${pid} still serves port ${port}` };
      }
    } else {
      const docker = await dockerProxy(port, exec);
      if (!docker) return { ok: false, status: "foreign", reason: `port ${port} is held by a process ${PRODUCT_NAME} did not start; stop it, then rerun` };
      const ok = await exec("docker", ["restart", docker.name], DOCKER_TIMEOUT_MS) !== null && await waitFor(true, port, fetch, waitMs);
      return ok ? { ok: true, status: "restarted", via: `docker ${docker.name}` } : { ok: false, status: "timeout", reason: `docker restart ${docker.name} did not come back` };
    }
  }
  const spawned = spawnContextProxyDaemon(home, importMetaUrl, env);
  if (!spawned.ok) return { ok: false, status: "spawn-failed", reason: spawned.reason };
  if (await waitFor(true, port, fetch, waitMs)) return { ok: true, status: "restarted", pid: spawned.pid };
  return { ok: false, status: "timeout", reason: `see ${proxyLogPath(home)}` };
}

/**
 * When an agent is pointed at the loopback proxy, ensure LiteLLM is listening.
 * Set `SMARTER_COMPACT_AUTO_PROXY=0` to skip auto-start.
 */
export async function ensureContextProxyRunning({
  home = os.homedir(),
  env = process.env,
  importMetaUrl,
  waitMs = DEFAULT_WAIT_MS,
  fetch = globalThis.fetch,
  exec = run,
  now = Date.now(),
} = {}) {
  if (/^(0|false|no)$/i.test(String(env.SMARTER_COMPACT_AUTO_PROXY || "1").trim())) {
    return { ok: true, status: "disabled" };
  }
  const port = resolveProxyPort(home, env);
  if (await probeContextProxy(port, { fetch })) {
    const stale = await refreshStale({ home, env, port, importMetaUrl, fetch, exec, waitMs, now });
    return { ok: true, ...(stale || { status: "already-up" }) };
  }
  if (!agentsPointedAtProxy(home, port).length) return { ok: true, status: "not-needed" };

  const spawned = spawnContextProxyDaemon(home, importMetaUrl, env);
  if (!spawned.ok) return { ok: false, status: "spawn-failed", reason: spawned.reason };

  const deadline = Date.now() + waitMs;
  while (Date.now() < deadline) {
    if (await probeContextProxy(port, { fetch })) {
      return { ok: true, status: "started", pid: spawned.pid };
    }
    await new Promise((resolve) => { setTimeout(resolve, 300); });
  }
  return { ok: false, status: "timeout", log: proxyLogPath(home) };
}
