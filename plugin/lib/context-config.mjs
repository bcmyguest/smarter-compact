/**
 * ~/.config/smarter-compact/config.toml — trim thresholds and other context settings.
 * Per-model rows live under [context_proxy.trim_at]; optional global override is trim_at_tokens.
 */
import fs from "node:fs";
import TOML from "@iarna/toml";
import { configDir } from "./brand.mjs";
import { writeFileAtomic } from "./file-io.mjs";
import { contextRoot } from "./context-store.mjs";
import { forgetText } from "./forget.mjs";

const CONFIG_HEADER = "# Smarter Compact — see docs/context-proxy.md\n";
const TRIM_AT_TABLE = "trim_at";
/** `[context_store]` keys, the env vars storeSettings (session-db.mjs) reads, and the values it accepts. */
const CONTEXT_STORE_NUMBER_KEYS = [
  ["ttl_days", "CONTEXT_STORE_TTL_DAYS", "a whole number >= 0", (n) => Number.isInteger(n) && n >= 0],
  ["embedding_timeout_s", "CONTEXT_EMBEDDING_TIMEOUT_S", "a number > 0", (n) => Number.isFinite(n) && n > 0],
  ["embedding_max_chars", "CONTEXT_EMBEDDING_MAX_CHARS", "a whole number > 0", (n) => Number.isInteger(n) && n > 0],
];
const CONTEXT_STORE_STRING_KEYS = [
  ["embedding_model", "CONTEXT_EMBEDDING_MODEL"],
  ["embedding_url", "CONTEXT_EMBEDDING_URL"],
];

export function configTomlPath(home) {
  return `${configDir(home)}/config.toml`;
}

export function loadConfigDoc(home) {
  const file = configTomlPath(home);
  if (!fs.existsSync(file)) return {};
  try {
    return TOML.parse(fs.readFileSync(file, "utf8"));
  } catch (e) {
    throw new Error(`${file}: ${/** @type {Error} */ (e).message}`);
  }
}

function saveConfigDoc(home, doc) {
  fs.mkdirSync(configDir(home), { recursive: true, mode: 0o700 });
  writeFileAtomic(configTomlPath(home), CONFIG_HEADER + TOML.stringify(doc), { mode: 0o600 });
}

function contextProxySection(doc) {
  if (!doc.context_proxy || typeof doc.context_proxy !== "object") doc.context_proxy = {};
  return doc.context_proxy;
}

function trimAtSection(doc) {
  const proxy = contextProxySection(doc);
  if (!proxy[TRIM_AT_TABLE] || typeof proxy[TRIM_AT_TABLE] !== "object") proxy[TRIM_AT_TABLE] = {};
  return proxy[TRIM_AT_TABLE];
}

/** Positive integer trim threshold or null. */
function parseTokens(raw) {
  const n = Number(String(raw ?? "").trim().replace(/_/g, ""));
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : null;
}

/**
 * Apply config.toml trim settings when CONTEXT_TRIM_AT_TOKENS is not already set (env or proxy.env).
 * Sets CONTEXT_TRIM_AT_OVERRIDES JSON for per-model tables.
 */
export function applyTrimConfigToEnv(env, home) {
  let doc;
  try {
    doc = loadConfigDoc(home);
  } catch {
    return;
  }
  const proxy = doc.context_proxy ?? {};
  if (typeof proxy.standing_instruction === "boolean" && envUnset(env, "CONTEXT_STANDING_INSTRUCTION")) {
    env.CONTEXT_STANDING_INSTRUCTION = proxy.standing_instruction ? "1" : "0";
  }
  if (!envUnset(env, "CONTEXT_TRIM_AT_TOKENS")) return;
  const global = parseTokens(proxy.trim_at_tokens);
  if (global) {
    env.CONTEXT_TRIM_AT_TOKENS = String(global);
    return;
  }
  const table = proxy[TRIM_AT_TABLE];
  if (table && typeof table === "object" && Object.keys(table).length) {
    const out = {};
    for (const [k, v] of Object.entries(table)) {
      const tok = parseTokens(v);
      if (tok) out[String(k).toLowerCase()] = tok;
    }
    if (Object.keys(out).length) env.CONTEXT_TRIM_AT_OVERRIDES = JSON.stringify(out);
  }
}

function envUnset(env, key) {
  return String(env[key] ?? "").trim() === "";
}

/**
 * `[context_store]` keys as the env vars storeSettings reads. An invalid value is left out and reported on
 * stderr. Throws when config.toml exists but does not parse.
 */
export function contextStoreEnvFromConfig(home) {
  const store = loadConfigDoc(home).context_store ?? {};
  const out = {};
  const reject = (key, want) => {
    process.stderr.write(`smarter-compact: ignoring [context_store] ${key} = ${JSON.stringify(store[key])} in ${configTomlPath(home)}: expected ${want}\n`);
  };
  for (const [key, envName, want, valid] of CONTEXT_STORE_NUMBER_KEYS) {
    if (store[key] === undefined) continue;
    if (typeof store[key] === "number" && valid(store[key])) out[envName] = String(store[key]);
    else reject(key, want);
  }
  for (const [key, envName] of CONTEXT_STORE_STRING_KEYS) {
    if (store[key] === undefined) continue;
    if (typeof store[key] === "string") out[envName] = store[key].trim();
    else reject(key, "a string");
  }
  return out;
}

/** Apply config.toml `[context_store]` to each variable env leaves unset; an unparsable file changes nothing. */
export function applyContextStoreConfigToEnv(env, home) {
  let fromConfig;
  try {
    fromConfig = contextStoreEnvFromConfig(home);
  } catch {
    return env;
  }
  for (const [key, value] of Object.entries(fromConfig)) if (envUnset(env, key)) env[key] = value;
  return env;
}

export function listTrimConfig(home) {
  const doc = loadConfigDoc(home);
  const proxy = doc.context_proxy ?? {};
  const global = parseTokens(proxy.trim_at_tokens);
  const table = proxy[TRIM_AT_TABLE] ?? {};
  const families = {};
  for (const [k, v] of Object.entries(table)) {
    const tok = parseTokens(v);
    if (tok) families[String(k).toLowerCase()] = tok;
  }
  return { global, families };
}

/** `haiku=95000` or `trim=120000` (global override for every model). */
export function parseSetAssignment(raw) {
  const m = String(raw ?? "").trim().match(/^([A-Za-z0-9_-]+)\s*=\s*([0-9][0-9_,]*)$/);
  if (!m) return null;
  const tokens = parseTokens(m[2]);
  if (!tokens) return null;
  return { key: m[1].toLowerCase(), tokens };
}

export function setTrimAssignment(home, assignment) {
  const doc = loadConfigDoc(home);
  const proxy = contextProxySection(doc);
  if (assignment.key === "trim" || assignment.key === "global") {
    proxy.trim_at_tokens = assignment.tokens;
    saveConfigDoc(home, doc);
    return { kind: "global", tokens: assignment.tokens };
  }
  const table = trimAtSection(doc);
  table[assignment.key] = assignment.tokens;
  saveConfigDoc(home, doc);
  return { kind: "family", key: assignment.key, tokens: assignment.tokens };
}

export function unsetTrimKey(home, key) {
  const k = String(key ?? "").trim().toLowerCase();
  if (!k) return false;
  const doc = loadConfigDoc(home);
  const proxy = doc.context_proxy ?? {};
  if (k === "trim" || k === "global") {
    if (proxy.trim_at_tokens === undefined) return false;
    delete proxy.trim_at_tokens;
    saveConfigDoc(home, doc);
    return true;
  }
  const table = proxy[TRIM_AT_TABLE];
  if (!table || table[k] === undefined) return false;
  delete table[k];
  saveConfigDoc(home, doc);
  return true;
}

const HELP = `smarter-compact context — trim thresholds (estimated prompt tokens)

Usage:
  smarter-compact context list
  smarter-compact context set <family>=<tokens>   e.g. haiku=95000, default=125000
  smarter-compact context set trim=<tokens>        same threshold for every model
  smarter-compact context unset <family|trim>
  smarter-compact context forget [--session <id>] <text|->   redact text from the stored context
                                                              (- reads it from stdin, keeping it out of shell history)

With no global trim set, the proxy matches model ids against [context_proxy.trim_at] in
~/.config/smarter-compact/config.toml (built-in: haiku → 95000, else default 125000).
CONTEXT_TRIM_AT_TOKENS in the environment or proxy.env overrides the table for all models.`;

export function runContextCommand(argv, { home = process.env.HOME, log = console }) {
  const action = argv[0];
  if (!action || action === "--help" || action === "-h" || action === "help") {
    log.log(HELP);
    return 0;
  }
  if (action === "forget") {
    let sessionIds = null;
    const args = argv.slice(1);
    const at = args.indexOf("--session");
    if (at !== -1) {
      if (!args[at + 1]) {
        log.error("usage: smarter-compact context forget [--session <id>] <text|->");
        return 2;
      }
      sessionIds = [args[at + 1]];
      args.splice(at, 2);
    }
    let text = args[0];
    if (text === "-") text = fs.readFileSync(0, "utf8").replace(/\r?\n$/, "");
    if (!text) {
      log.error("usage: smarter-compact context forget [--session <id>] <text|->");
      return 2;
    }
    try {
      const r = forgetText(contextRoot(home), text, { sessionIds });
      const rows = r.sessions.reduce((n, s) => n + s.rows, 0);
      const changed = r.sessions.filter((s) => s.rows > 0).length;
      log.log(`forgot ${rows} row(s) in ${changed} of ${r.sessions.length} session(s), ${r.blobs} blob(s), ${r.scratchpads} scratchpad(s)`);
      log.log("Copies outside the store are untouched: the agent's live context and its own transcript files.");
      return 0;
    } catch (error) {
      log.error(`smarter-compact: ${error.message}`);
      return 1;
    }
  }
  if (action === "list") {
    const { global, families } = listTrimConfig(home);
    if (global) log.log(`global (all models): ${global}  (config.toml [context_proxy].trim_at_tokens)`);
    else log.log("global (all models): (unset — per-model table active)");
    const keys = Object.keys(families).sort();
    if (keys.length) {
      for (const k of keys) log.log(`  ${k}: ${families[k]}  (config)`);
    } else log.log("  (no [context_proxy.trim_at] entries in config.toml — built-in haiku=95000, default=125000)");
    log.log("Restart the proxy or start a new session after changes.");
    return 0;
  }
  if (action === "set") {
    const assignment = parseSetAssignment(argv[1]);
    if (!assignment) {
      log.error("usage: smarter-compact context set haiku=95000");
      return 2;
    }
    const result = setTrimAssignment(home, assignment);
    if (result.kind === "global") {
      log.log(`saved global trim_at_tokens=${result.tokens} in ${configTomlPath(home)}`);
    } else {
      log.log(`saved [context_proxy.trim_at] ${result.key} = ${result.tokens} in ${configTomlPath(home)}`);
    }
    log.log("Restart the proxy (or re-run enable) for the running LiteLLM process to pick this up.");
    return 0;
  }
  if (action === "unset") {
    const key = argv[1];
    if (!key || unsetTrimKey(home, key)) {
      if (key) log.log(`removed ${key} from ${configTomlPath(home)}`);
      else log.error("usage: smarter-compact context unset haiku");
      return key ? 0 : 2;
    }
    log.error(`no such key in config: ${key}`);
    return 1;
  }
  log.error(HELP);
  return 2;
}
