/**
 * Detached LiteLLM context proxy (same process as `smarter-compact proxy`, started in the background).
 */
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { configDir, ensureProductEnv, resolveProxyPort, ROOT_ENV, stateParent } from "./brand.mjs";
import { agentsPointedAtProxy, ensureKey } from "./enable.mjs";

const PROBE_TIMEOUT_MS = 1500;
const DEFAULT_WAIT_MS = 12_000;

function dirFromImportMeta(importMetaUrl) {
  if (!importMetaUrl) return null;
  const filePath = importMetaUrl.startsWith("file:")
    ? fileURLToPath(importMetaUrl)
    : importMetaUrl;
  return path.dirname(filePath);
}

/** Directory that contains `proxy/openmem_context_hook.py` (npm package or clone). */
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
    if (fs.existsSync(path.join(candidate, "proxy", "openmem_context_hook.py"))) return candidate;
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

function litellmEnv(home, root, port, env) {
  const merged = { ...env };
  ensureProductEnv(merged, home);
  return {
    ...merged,
    PYTHONPATH: path.join(root, "proxy"),
    LITELLM_MASTER_KEY: ensureKey(home),
    OPENMEM_STATE_DIR: stateParent(home, env),
    CONTEXT_COMPACTION_RECALL: merged.CONTEXT_COMPACTION_RECALL || "0",
  };
}

/** Start LiteLLM detached; logs append to `~/.config/smarter-compact/proxy.log`. */
export function spawnContextProxyDaemon(home, importMetaUrl, env = process.env) {
  const root = packageRoot(importMetaUrl);
  if (!root) return { ok: false, reason: "no-package-root" };
  const port = String(resolveProxyPort(home, env));
  fs.mkdirSync(configDir(home), { recursive: true, mode: 0o700 });
  const logFd = fs.openSync(proxyLogPath(home), "a");
  const config = path.join(root, "proxy", "config.yaml");
  let child;
  try {
    child = spawn("litellm", ["--config", config, "--host", "127.0.0.1", "--port", port], {
      env: litellmEnv(home, root, port, env),
      detached: true,
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
 * When an agent is pointed at the loopback proxy, ensure LiteLLM is listening.
 * Set `SMARTER_COMPACT_AUTO_PROXY=0` to skip auto-start.
 */
export async function ensureContextProxyRunning({
  home = os.homedir(),
  env = process.env,
  importMetaUrl,
  waitMs = DEFAULT_WAIT_MS,
  fetch = globalThis.fetch,
} = {}) {
  if (/^(0|false|no)$/i.test(String(env.SMARTER_COMPACT_AUTO_PROXY || "1").trim())) {
    return { ok: true, status: "disabled" };
  }
  const port = resolveProxyPort(home, env);
  if (await probeContextProxy(port, { fetch })) return { ok: true, status: "already-up" };
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
