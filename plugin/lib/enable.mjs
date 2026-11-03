/**
 * Point one agent at the local proxy, or remove that pointing.
 * OpenCode 2 trims in its plugin and is never given a base URL.
 */
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { configDir, DEFAULT_PORT, PRODUCT, PRODUCT_NAME } from "./brand.mjs";
import { readJson, upsertEnvFile, writeJson } from "./file-io.mjs";
import { mergeOpencodeConfig, opencodeConfigPath, opencodeMajorVersion, parseJsonc } from "./opencode-config.mjs";
import { removeSpans, tomlSpansUnder, tomlStatements } from "./toml-edit.mjs";

export const HEADER_NAME = "x-litellm-api-key";
export const KEY_NAME = "LITELLM_MASTER_KEY";
export const PROVIDER_ID = PRODUCT;
const SECRET_MODE = 0o600;
const LOOPBACK = (port) => new RegExp(`^http://127\\.0\\.0\\.1:${port}(?:/v1)?$`);
const CODEX_LOOPBACK = (port) => new RegExp(`^http://127\\.0\\.0\\.1:${port}/backend-api/codex$`);

export const AGENTS = ["claude", "codex", "opencode"];

export function proxyEnvPath(home) {
  return path.join(configDir(home), "proxy.env");
}

export function backupPath(home) {
  return path.join(configDir(home), "proxy-backup.json");
}

export function claudeSettingsPath(home) {
  return path.join(home, ".claude", "settings.json");
}

export function codexConfigPath(home) {
  return path.join(home, ".codex", "config.toml");
}

function readBackup(home) {
  try {
    return readJson(backupPath(home));
  } catch {
    return {};
  }
}

function writeBackup(home, backup) {
  const file = backupPath(home);
  if (!Object.keys(backup).length) fs.rmSync(file, { force: true });
  else writeJson(file, backup, { mode: SECRET_MODE });
}

/** Master key from proxy.env, created mode 600 the first time. */
export function ensureKey(home) {
  const file = proxyEnvPath(home);
  const previous = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
  const line = previous.split(/\r?\n/).find((l) => l.startsWith(`${KEY_NAME}=`));
  const existing = line?.slice(KEY_NAME.length + 1).trim();
  if (existing) {
    fs.chmodSync(file, SECRET_MODE);
    return existing;
  }
  const key = crypto.randomBytes(32).toString("hex");
  upsertEnvFile(file, { [KEY_NAME]: key }, { mode: SECRET_MODE });
  return key;
}

function otherHeaderLines(value) {
  return String(value || "").split("\n").filter((l) => l.trim() && !l.trim().toLowerCase().startsWith(`${HEADER_NAME}:`));
}

function setOrDelete(obj, key, value) {
  if (value === undefined || value === "") delete obj[key];
  else obj[key] = value;
}

function wireClaude(home, { enabled, port, key }) {
  const file = claudeSettingsPath(home);
  const settings = readJson(file);
  const env = { ...(settings.env || {}) };
  const backup = readBackup(home);
  const pointed = LOOPBACK(port).test(String(env.ANTHROPIC_BASE_URL || ""));
  if (enabled) {
    const foreign = !pointed && (env.ANTHROPIC_BASE_URL !== undefined || env.ANTHROPIC_CUSTOM_HEADERS !== undefined);
    if (foreign && !backup.claude) {
      backup.claude = { ANTHROPIC_BASE_URL: env.ANTHROPIC_BASE_URL, ANTHROPIC_CUSTOM_HEADERS: env.ANTHROPIC_CUSTOM_HEADERS };
      writeBackup(home, backup);
    }
    env.ANTHROPIC_BASE_URL = `http://127.0.0.1:${port}`;
    env.ANTHROPIC_CUSTOM_HEADERS = [...otherHeaderLines(env.ANTHROPIC_CUSTOM_HEADERS), `${HEADER_NAME}: Bearer ${key}`].join("\n");
  } else {
    const hasOurs = String(env.ANTHROPIC_CUSTOM_HEADERS || "").split("\n").some((l) => l.toLowerCase().includes(HEADER_NAME) && l.includes("Bearer "));
    if (!pointed || !hasOurs) return false;
    const saved = backup.claude;
    if (saved) {
      setOrDelete(env, "ANTHROPIC_BASE_URL", saved.ANTHROPIC_BASE_URL);
      setOrDelete(env, "ANTHROPIC_CUSTOM_HEADERS", saved.ANTHROPIC_CUSTOM_HEADERS);
      delete backup.claude;
      writeBackup(home, backup);
    } else {
      delete env.ANTHROPIC_BASE_URL;
      setOrDelete(env, "ANTHROPIC_CUSTOM_HEADERS", otherHeaderLines(env.ANTHROPIC_CUSTOM_HEADERS).join("\n"));
    }
  }
  if (Object.keys(env).length) settings.env = env;
  else delete settings.env;
  writeJson(file, settings, enabled ? { mode: SECRET_MODE } : {});
  return true;
}

function quotedToml(raw) {
  const match = /=\s*("(?:\\.|[^"\\])*"|'(?:[^']*)'|[A-Za-z0-9_-]+)/.exec(raw);
  if (!match) return undefined;
  const value = match[1];
  if (value.startsWith('"')) return JSON.parse(value);
  if (value.startsWith("'")) return value.slice(1, -1);
  return value;
}

function kvRaw(text, parts) {
  const statement = tomlStatements(text).find((st) => st.kind === "kv" && st.path.length === parts.length && st.path.every((p, i) => p === parts[i]));
  return statement ? text.slice(statement.start, statement.end) : undefined;
}

function codexPointed(text, port) {
  const provider = kvRaw(text, ["model_provider"]);
  if (provider && quotedToml(provider) === PROVIDER_ID) return true;
  const base = kvRaw(text, ["model_providers", PROVIDER_ID, "base_url"]);
  return Boolean(base && CODEX_LOOPBACK(port).test(String(quotedToml(base) || "")));
}

function withTopLevelLine(text, line) {
  const statements = tomlStatements(text);
  const firstTable = statements.findIndex((st) => st.kind !== "kv");
  const topLevel = firstTable < 0 ? statements : statements.slice(0, firstTable);
  const at = topLevel.length ? topLevel.at(-1).end : 0;
  const head = text.slice(0, at);
  return `${head}${head && !head.endsWith("\n") ? "\n" : ""}${line}\n${text.slice(at)}`;
}

function withoutCodexWiring(text) {
  const spans = [
    ...tomlSpansUnder(text, ["model_providers", PROVIDER_ID]),
    ...tomlStatements(text).filter((st) => st.kind === "kv" && st.path.length === 1 && st.path[0] === "model_provider"),
  ];
  return removeSpans(text, spans);
}

function wireCodex(home, { enabled, port, key }) {
  const file = codexConfigPath(home);
  const text = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
  let pointed = false;
  try {
    pointed = codexPointed(text, port);
  } catch (err) {
    return { ok: false, message: `codex: left ${file} unchanged (${err.message})` };
  }
  if (!enabled && !pointed) return false;
  const backup = readBackup(home);
  const rest = withoutCodexWiring(text).trimEnd();
  const base = rest ? `${rest}\n` : "";
  let next;
  if (enabled) {
    const current = kvRaw(text, ["model_provider"]);
    const currentId = current ? quotedToml(current) : undefined;
    if (!pointed && currentId && currentId !== PROVIDER_ID && !backup.codex) {
      backup.codex = { model_provider: currentId };
      writeBackup(home, backup);
    }
    const section = [
      `[model_providers.${PROVIDER_ID}]`,
      `name = "${PRODUCT_NAME} context proxy"`,
      `base_url = ${JSON.stringify(`http://127.0.0.1:${port}/backend-api/codex`)}`,
      'wire_api = "responses"',
      "requires_openai_auth = true",
      "supports_websockets = false",
      `http_headers = { ${JSON.stringify(HEADER_NAME)} = ${JSON.stringify(`Bearer ${key}`)} }`,
    ].join("\n");
    next = `${withTopLevelLine(base, `model_provider = ${JSON.stringify(PROVIDER_ID)}`)}\n${section}\n`;
  } else {
    const saved = backup.codex?.model_provider;
    next = typeof saved === "string" && saved ? withTopLevelLine(base, `model_provider = ${JSON.stringify(saved)}`) : base;
    if (backup.codex) {
      delete backup.codex;
      writeBackup(home, backup);
    }
  }
  tomlStatements(next);
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  fs.writeFileSync(file, next.endsWith("\n") ? next : `${next}\n`, { mode: enabled ? SECRET_MODE : 0o644 });
  return true;
}

function wireOpencode(home, { enabled, port, key }) {
  const file = opencodeConfigPath(home);
  let options = {};
  try {
    options = (fs.existsSync(file) ? parseJsonc(fs.readFileSync(file, "utf8")) : {})?.provider?.anthropic?.options || {};
  } catch (err) {
    return { ok: false, message: `opencode: left ${file} unchanged (${err.message})` };
  }
  const pointed = LOOPBACK(port).test(String(options.baseURL || ""));
  const backup = readBackup(home);
  if (!enabled && !(pointed && options.headers?.[HEADER_NAME])) return false;
  if (enabled && !pointed && !backup.opencode && (options.baseURL !== undefined || options.headers?.[HEADER_NAME] !== undefined)) {
    backup.opencode = { baseURL: options.baseURL, header: options.headers?.[HEADER_NAME] };
    writeBackup(home, backup);
  }
  const saved = enabled ? undefined : backup.opencode;
  mergeOpencodeConfig(home, "provider", (existing) => {
    const provider = { ...(existing || {}) };
    const anthropic = { ...(provider.anthropic || {}) };
    const next = { ...(anthropic.options || {}) };
    const headers = { ...(next.headers || {}) };
    if (enabled) {
      next.baseURL = `http://127.0.0.1:${port}/v1`;
      headers[HEADER_NAME] = `Bearer ${key}`;
    } else {
      setOrDelete(next, "baseURL", saved?.baseURL);
      setOrDelete(headers, HEADER_NAME, saved?.header);
    }
    if (Object.keys(headers).length) next.headers = headers;
    else delete next.headers;
    if (Object.keys(next).length) anthropic.options = next;
    else delete anthropic.options;
    if (Object.keys(anthropic).length) provider.anthropic = anthropic;
    else delete provider.anthropic;
    return provider;
  }, enabled ? { mode: SECRET_MODE } : {});
  if (saved) {
    delete backup.opencode;
    writeBackup(home, backup);
  }
  return true;
}

/**
 * @param {string} home
 * @param {"claude"|"codex"|"opencode"} agent
 * @param {{ enabled: boolean, port?: number, major?: number }} opts
 * @returns {{ ok: boolean, message: string }}
 */
export function configureAgent(home, agent, { enabled, port = DEFAULT_PORT, major } = {}) {
  if (!AGENTS.includes(agent)) return { ok: false, message: `unknown agent ${agent}` };
  const opencodeMajor = agent === "opencode" ? (major ?? opencodeMajorVersion()) : 1;
  if (agent === "opencode" && opencodeMajor >= 2 && enabled) {
    return {
      ok: false,
      message: "OpenCode 2 trims inside its plugin and is not pointed at the proxy. Run: opencode plugin smarter-compact (or opencode plugin -g smarter-compact).",
    };
  }
  const key = enabled ? ensureKey(home) : "";
  const wire = { claude: wireClaude, codex: wireCodex, opencode: wireOpencode }[agent];
  const done = wire(home, { enabled, port, key });
  if (done && typeof done === "object") return done;
  if (!done) return { ok: false, message: enabled ? `${agent}: nothing written` : `${agent}: not pointed at ${PRODUCT}` };
  const where = agent === "codex"
    ? `http://127.0.0.1:${port}/backend-api/codex`
    : `http://127.0.0.1:${port}${agent === "opencode" ? "/v1" : ""}`;
  return { ok: true, message: enabled ? `${agent}: ${where}` : `${agent}: proxy settings removed` };
}
