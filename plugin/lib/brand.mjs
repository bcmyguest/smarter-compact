/**
 * Product identity for the extracted trimmer. Everything the product writes or says carries its
 * own name; the spellings earlier releases emitted (`[openmem: …]`,
 * `OPENMEM_STATE_DIR`, `openmem:smarter-compact`) are still READ, because sessions, command files
 * and env set up before the rename exist in the wild. Tool names were always `smart_compact_*`.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { applyContextStoreConfigToEnv, applyTrimConfigToEnv } from "./context-config.mjs";

export const PRODUCT = "smarter-compact";
export const PRODUCT_NAME = "Smarter Compact";
export const DEFAULT_PORT = 4101;
export const STATE_ENV = "SMARTER_COMPACT_STATE_DIR";
export const ROOT_ENV = "SMARTER_COMPACT_ROOT";

/**
 * Keys in `proxy.env` applied when missing from the process environment (env wins): every non-secret
 * setting the proxy reads (proxy/smarter_compact/settings.py) except the port and the store location.
 */
export const PROXY_ENV_CONTEXT_KEYS = [
  "CONTEXT_PROXY_ENABLED",
  "CONTEXT_DEBUG_SHAPES",
  "CONTEXT_TRIM_AT_TOKENS",
  "CONTEXT_TRIM_AT_OVERRIDES",
  "CONTEXT_TRIM_TO_TOKENS",
  "CONTEXT_TRIM_MIN_GAIN_TOKENS",
  "CONTEXT_CLEAR_KEEP_RESULTS",
  "CONTEXT_CLEAR_INPUTS_OVER_CHARS",
  "CONTEXT_STRIP_OLD_REASONING",
  "CONTEXT_MASK_OBSERVATIONS",
  "CONTEXT_STRIP_PROSE",
  "CONTEXT_PROGRESS_NOTE",
  "CONTEXT_SCRATCHPAD",
  "CONTEXT_SCRATCHPAD_NUDGE_AT",
  "CONTEXT_CHARS_PER_TOKEN",
  "CONTEXT_COMPRESS_OVER_TOKENS",
  "CONTEXT_EVICTION",
  "CONTEXT_EVICTION_BATCH",
  "CONTEXT_EVICTION_KEEP",
  "CONTEXT_STANDING_INSTRUCTION",
  "CONTEXT_REUSE_RULES",
  "CONTEXT_DEDUP_RESULTS",
  "CONTEXT_SERVER_CLEAR",
  "CONTEXT_SERVER_CLEAR_TRIGGER_TOKENS",
  "CONTEXT_SERVER_CLEAR_KEEP_TOOL_USES",
  "CONTEXT_SERVER_CLEAR_AT_LEAST_TOKENS",
  "CONTEXT_STORE_TTL_DAYS",
  "CONTEXT_EMBEDDING_MODEL",
  "CONTEXT_EMBEDDING_URL",
  "CONTEXT_EMBEDDING_TIMEOUT_S",
  "CONTEXT_EMBEDDING_MAX_CHARS",
];
/** Secret `proxy.env` keys passed only to the proxy the CLI starts, never into an agent's environment. */
export const PROXY_ENV_SECRET_KEYS = ["CONTEXT_EMBEDDING_API_KEY"];

/** Parent of `context/`. The core appends `context` itself. */
export function stateParent(home = os.homedir(), env = process.env) {
  const trim = (v) => String(v || "").replace(/^[ \t\n\v\f\r]+|[ \t\n\v\f\r]+$/g, "");
  // OPENMEM_STATE_DIR is the pre-rename name of the same setting and still configures it.
  return trim(env[STATE_ENV]) || trim(env.OPENMEM_STATE_DIR) || path.join(home, ".local", "share", PRODUCT);
}

function envUnset(env, key) {
  const v = env[key];
  return v === undefined || v === null || String(v).trim() === "";
}

/** Fill process env the core already reads. Existing values win; then `proxy.env`; then config.toml. */
export function ensureProductEnv(env = process.env, home = os.homedir()) {
  if (!String(env[STATE_ENV] || env.OPENMEM_STATE_DIR || "").trim()) env[STATE_ENV] = stateParent(home, env);
  const file = readProxyEnv(home);
  if (envUnset(env, "CONTEXT_PROXY_PORT")) {
    env.CONTEXT_PROXY_PORT = file.CONTEXT_PROXY_PORT ? String(file.CONTEXT_PROXY_PORT) : String(DEFAULT_PORT);
  }
  fillFromProxyEnv(env, file, PROXY_ENV_CONTEXT_KEYS);
  applyTrimConfigToEnv(env, home);
  applyContextStoreConfigToEnv(env, home);
  return env;
}

/** A copy of env plus the secret `proxy.env` keys it leaves unset, for settings that must not reach process.env. */
export function withProxyEnvSecrets(env, home = os.homedir()) {
  return fillFromProxyEnv({ ...env }, readProxyEnv(home), PROXY_ENV_SECRET_KEYS);
}

/** Copy each key that env leaves unset and `proxy.env` sets. */
export function fillFromProxyEnv(env, file, keys) {
  for (const key of keys) {
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
