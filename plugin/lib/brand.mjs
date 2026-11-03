/**
 * Product identity for the extracted trimmer. The session-store wire format
 * (`[openmem: …]`, `smart_compact_*`, `OPENMEM_STATE_DIR`) stays the OpenMem contract.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export const PRODUCT = "smarter-compact";
export const PRODUCT_NAME = "Smarter Compact";
export const DEFAULT_PORT = 4101;
export const STATE_ENV = "OPENMEM_STATE_DIR";
export const ROOT_ENV = "SMARTER_COMPACT_ROOT";

/** Keys in `proxy.env` applied when missing from the process environment (env wins). */
export const PROXY_ENV_CONTEXT_KEYS = [
  "CONTEXT_TRIM_AT_TOKENS",
  "CONTEXT_TRIM_TO_FRACTION",
  "CONTEXT_COMPRESS_OVER_TOKENS",
  "CONTEXT_COMPACTION_MAX_CHARS",
  "CONTEXT_CHARS_PER_TOKEN",
  "CONTEXT_STANDING_INSTRUCTION",
  "CONTEXT_PROXY_ENABLED",
];

/** Parent of `context/`. The core appends `context` itself. */
export function stateParent(home = os.homedir(), env = process.env) {
  const set = String(env[STATE_ENV] || "").replace(/^[ \t\n\v\f\r]+|[ \t\n\v\f\r]+$/g, "");
  return set || path.join(home, ".local", "share", PRODUCT);
}

function envUnset(env, key) {
  const v = env[key];
  return v === undefined || v === null || String(v).trim() === "";
}

/** Fill process env the core already reads. Existing values win; then `proxy.env`. */
export function ensureProductEnv(env = process.env, home = os.homedir()) {
  if (!String(env[STATE_ENV] || "").trim()) env[STATE_ENV] = stateParent(home, env);
  if (env.CONTEXT_COMPACTION_RECALL === undefined) env.CONTEXT_COMPACTION_RECALL = "0";
  const file = readProxyEnv(home);
  if (envUnset(env, "CONTEXT_PROXY_PORT")) {
    env.CONTEXT_PROXY_PORT = file.CONTEXT_PROXY_PORT ? String(file.CONTEXT_PROXY_PORT) : String(DEFAULT_PORT);
  }
  for (const key of PROXY_ENV_CONTEXT_KEYS) {
    if (envUnset(env, key) && file[key] !== undefined && String(file[key]).trim() !== "") {
      env[key] = String(file[key]);
    }
  }
  return env;
}

export function configDir(home) {
  return path.join(home, ".config", PRODUCT);
}

export function proxyEnvPath(home = os.homedir()) {
  return path.join(configDir(home), "proxy.env");
}

/** Parsed `~/.config/smarter-compact/proxy.env` (KEY=value lines only). */
export function readProxyEnv(home = os.homedir()) {
  const file = proxyEnvPath(home);
  if (!fs.existsSync(file)) return {};
  const out = {};
  for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq <= 0) continue;
    out[trimmed.slice(0, eq)] = trimmed.slice(eq + 1);
  }
  return out;
}

/** Effective proxy port: `CONTEXT_PROXY_PORT` env wins, then proxy.env, then default. */
export function resolveProxyPort(home = os.homedir(), env = process.env) {
  const fromEnv = String(env.CONTEXT_PROXY_PORT ?? "").trim();
  if (fromEnv) {
    const n = Number(fromEnv);
    if (Number.isFinite(n) && n > 0 && n < 65536) return n;
  }
  const fromFile = String(readProxyEnv(home).CONTEXT_PROXY_PORT ?? "").trim();
  if (fromFile) {
    const n = Number(fromFile);
    if (Number.isFinite(n) && n > 0 && n < 65536) return n;
  }
  return DEFAULT_PORT;
}
