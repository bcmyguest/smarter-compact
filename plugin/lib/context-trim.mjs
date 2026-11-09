/**
 * Native context trimming for OpenCode 2: a port of the context proxy core (proxy/smarter_compact/)
 * for OpenCode's LLM message shape. File layout, database rows and inserted text follow docs/context-store.md.
 *
 * OpenCode 2 message: { id?, role: "system" | "user" | "assistant" | "tool", content: Part[] }.
 * Parts: text {text}, media, tool-call {id, name, input}, tool-result {id, name, result}, reasoning {text},
 * compaction, effort. A tool result is { type: "text" | "json" | "error", value } or
 * { type: "content", value: [{ type: "text", text } | { type: "file", ... }] }.
 */
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { withProxyEnvSecrets } from "./brand.mjs";
import {
  LEDGER_HEAD, LEDGER_MAX, REUSE_PREFIX, coreId, ledgerBlock, nudgeLine, reduceLog, walk,
} from "./context-ledger.mjs";
import { contextRoot, sanitizeSessionId } from "./context-store.mjs";
import { blobRef, chain, dropSession, openSession, scheduleEmbedding, storeSettings } from "./session-db.mjs";

const SURROGATE_RE = /[\uD800-\uDFFF]/;
// Python string semantics (code points), so lengths and slices match the proxy's.
export const cpLen = (s) => (SURROGATE_RE.test(s) ? Array.from(s).length : s.length);
export const cpSlice = (s, start, end) => (SURROGATE_RE.test(s) ? Array.from(s).slice(start, end).join("") : s.slice(start, end));

export const DEFAULT_TRIM_AT_TOKENS = 125000;
/**
 * Trim thresholds by model family (a substring of the lowercased model id), used while CONTEXT_TRIM_AT_TOKENS is
 * unset; same table as MODEL_TRIM_AT_TOKENS in proxy/smarter_compact/settings.py.
 */
export const MODEL_TRIM_AT_TOKENS = [["haiku", 95000]];

function parseTrimOverrides(env) {
  const raw = String(env.CONTEXT_TRIM_AT_OVERRIDES ?? "").trim();
  if (!raw) return { default: null, families: {} };
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return { default: null, families: {} };
    const families = {};
    let def = null;
    for (const [k, v] of Object.entries(parsed)) {
      const n = Number(v);
      if (!Number.isFinite(n) || n <= 0) continue;
      const key = String(k).toLowerCase();
      if (key === "default") def = Math.floor(n);
      else families[key] = Math.floor(n);
    }
    return { default: def, families };
  } catch {
    return { default: null, families: {} };
  }
}

function mergedTrimFamilies(overrideFamilies) {
  const map = new Map(MODEL_TRIM_AT_TOKENS);
  for (const [k, v] of Object.entries(overrideFamilies)) map.set(k, v);
  return map;
}
/**
 * A trim keeps older in-turn exchanges while the request stays under this many tokens, by model family like the
 * threshold, used while CONTEXT_TRIM_TO_TOKENS is unset; same as the proxy.
 */
export const DEFAULT_TRIM_TO_TOKENS = 66000;
export const MODEL_TRIM_TO_TOKENS = [["haiku", 50000]];
export const DEFAULT_COMPRESS_OVER_TOKENS = 4000;
/** Characters per prompt token before a session has reported usage (proxy/smarter_compact/tokens.py). */
export const DEFAULT_CHARS_PER_TOKEN = 2.5;
export const MARKER_PREFIX = "[smarter-compact: tool result compressed,";
/** Rows written before the rename begin with this; they are still recognized, never re-compressed. */
export const MARKER_PREFIX_LEGACY = "[openmem: tool result compressed,";
const markerAt = (text) => (text.startsWith(MARKER_PREFIX) ? MARKER_PREFIX
  : text.startsWith(MARKER_PREFIX_LEGACY) ? MARKER_PREFIX_LEGACY : null);
/**
 * Added to the system prompt of every request while trimming is on, before any trim; constant, so the
 * cached prefix never changes. Same text as STANDING_INSTRUCTION in proxy/smarter_compact/core.py.
 */
export const STANDING_MARKER = "Context management: to fit your context window";
/**
 * CONTEXT_REUSE_RULES: static don't-repeat-work rules ending the standing instruction, after Hu et al.,
 * arXiv 2609.30725 §4.3 / A.4.2 (DevSkills). Same text as REUSE_RULES in proxy/smarter_compact/core.py.
 */
export const REUSE_RULES = "Don't repeat work: before reading a file or running a command, check whether its output is already in your "
  + "context and reuse it; re-read only when it may have changed, and say why.\n"
  + "Rerun tests or builds only after the code changed or to get new evidence; save long output to a file and "
  + "filter that instead of rerunning.\n"
  + "Revise an existing script instead of writing a near-copy; when repeated actions bring no new evidence, switch"
  + " to a different approach.";
// Same as TOOL_SEARCH / CLAUDE_CODE_PREFIX in proxy/smarter_compact/core.py.
const TOOL_SEARCH = "ToolSearch";
const CLAUDE_CODE_PREFIX = "mcp__smarter-compact__";
// CONTEXT_SCRATCHPAD: the pad's cap and tool name; same as SCRATCHPAD_MAX_CHARS / SCRATCHPAD_TOOL in proxy core.py.
export const SCRATCHPAD_MAX_CHARS = 2500;
const SCRATCHPAD_TOOL = "smart_compact_scratchpad";
// Heads the pad in every trim note; same as SCRATCHPAD_LABEL in proxy core.py.
export const SCRATCHPAD_LABEL = "Your scratchpad, kept across trims (possibly stale; update it if the goal, plan or a key fact changed):";
const SCRATCHPAD_TOOL_RE = /(?:^|[^a-z0-9])smart_compact_scratchpad$/;
// The recall tools under any namespace separator; same as _RECALL_TOOL_RE in proxy core.py.
const RECALL_TOOL_RE = /(?:^|[^a-z0-9])(?:smart_compact_search|smart_compact_sql|smart_compact_get|context_status)$/;
// Start of the message in which Claude Code names the MCP tools it defers; same as DEFERRED_TOOLS_HEAD in proxy core.py.
const DEFERRED_TOOLS_HEAD = "deferred tools are now available via";
/** The names the recall tools actually have in a given request.
 *
 * Every harness namespaces MCP tools its own way, and the separator is not always `_`: OpenCode
 * uses a dot (`smarter-compact.smart_compact_search`; a trial run before the rename logged
 * `tools.openmem.…` in its own state db), Claude Code uses `mcp__<server>__<tool>`. Both the standing instruction
 * and the trim pointer used to order the model to call the bare name, i.e. a tool that was in
 * no request: every smart trial in every sweep then read "offered 4 tools, used 0", which is
 * not the model ignoring recall but recall being unaddressable. Match the offering instead of
 * assuming its spelling; the bare names stay the default for hosts that expose them that way.
 */
export function recallToolNames(tools) {
  // Three shapes, all real: the OpenCode `context` hook hands tools over keyed by id
  // (`q.tools.edit` in its own bundle), an Anthropic/OpenAI request carries `[{name}]` (or a
  // `{definitions: [...]}` wrapper), and some harnesses pass bare strings. Only the array-of-
  // objects form used to be read, so with a keyed object nothing matched and both texts fell
  // back to the bare names — proven on a live trim: the pointer ordered `smart_compact_search`
  // while that request offered `smarter-compact_smart_compact_search`.
  let entries = Array.isArray(tools) ? tools : [];
  if (!Array.isArray(tools) && tools && typeof tools === "object") {
    entries = Array.isArray(tools.definitions)
      ? tools.definitions
      : Object.entries(tools).map(([key, def]) => ({ name: (def && def.name) || key }));
  }
  const offered = [];
  for (const t of entries) {
    const name = typeof t === "string" ? t : t?.name;
    if (typeof name === "string" && RECALL_TOOL_RE.test(name)) {
      offered.push(name);
    }
  }
  // Claude Code defers MCP tools behind ToolSearch: neither recall tool is in the request, and the bare name fails.
  const deferred = entries.some((t) => (typeof t === "string" ? t : t?.name) === TOOL_SEARCH);
  const offeredPad = entries.map((t) => (typeof t === "string" ? t : t?.name))
    .find((n) => typeof n === "string" && SCRATCHPAD_TOOL_RE.test(n));
  const pad = offeredPad || (deferred ? `${CLAUDE_CODE_PREFIX}${SCRATCHPAD_TOOL}` : SCRATCHPAD_TOOL);
  // The scratchpad tool, whether the request offers it, and how to load it when deferred ("" when offered or never loadable).
  const scratchpad = {
    scratchpad: pad,
    scratchpadOffered: Boolean(offeredPad),
    scratchpadLoad: deferred && !offeredPad ? `${TOOL_SEARCH} with query "select:${pad}"` : "",
  };
  if (!offered.length && deferred) {
    const search = `${CLAUDE_CODE_PREFIX}smart_compact_search`;
    const sql = `${CLAUDE_CODE_PREFIX}smart_compact_sql`;
    return { search, sql, offered, seen: entries.length, load: `${TOOL_SEARCH} with query "select:${search},${sql}"`, ...scratchpad };
  }
  const pick = (suffix) => offered.find((n) => n.endsWith(suffix)) || suffix;
  // `seen` tells a caller when a non-empty tool list yielded nothing: that is a shape or
  // separator we do not read, and it degrades to bare names — the same symptom as recall
  // never being installed at all, which is otherwise indistinguishable in the field.
  return { search: pick("smart_compact_search"), sql: pick("smart_compact_sql"), offered, seen: entries.length, load: "", ...scratchpad };
}

function availability(names) {
  if (names.load) return `The tools ${names.search} and ${names.sql} are deferred: load them first by calling ${names.load}. `;
  return `The tools ${names.search} and ${names.sql} are available to you in this request. `;
}

export function standingInstruction(names = recallToolNames([]), reuse = true) {
  return STANDING_MARKER + ", older turns may be removed and replaced by an "
  + "[smarter-compact: …] pointer. Removed content is saved, not forgotten. When you need earlier content you cannot see in your context — values, ids, keys, counts, decisions — call "
  + `${names.search} (use \`pattern\` for exact ids/keys, \`context\` for neighbouring rows; \`line_matches\` gives `
  + `exact counts) or ${names.sql} for counts and aggregates over the session store; never guess `
  + "from memory you cannot see. This trim never summarizes removed content — a remembered \"summary of earlier "
  + "context\" is not its record; cite a search result instead."
  + (names.load ? ` The recall tools are deferred: load them first by calling ${names.load}.` : "")
  + (reuse ? `\n${REUSE_RULES}` : "");
}
export const STANDING_INSTRUCTION = standingInstruction();
const HEAD_LINES = 40;
const TAIL_LINES = 20;
const HEAD_CHARS = 4000;
const TAIL_CHARS = 2000;
const MAX_THREADS = 32;
const TRIM_STATE_VERSION = 1;
const FORMAT = "opencode2";
// events.trimmed on a usage row (core.TRIM_*): the request went up whole, carried an earlier cut, or made a new cut.
export const TRIM_NONE = 0;
export const TRIM_KEPT = 1;
export const TRIM_NEW = 2;
// The nudge fires once the prompt passes this fraction of the trim threshold; same as proxy settings.py.
export const DEFAULT_SCRATCHPAD_NUDGE_AT = 0.75;
// A cleared tool call's input strings over this many characters become placeholders; same as proxy settings.py.
export const DEFAULT_CLEAR_INPUTS_OVER_CHARS = 2000;
// CONTEXT_STRIP_REASONING_MIN_TOKENS: the least reasoning a batched strip must take off; same as proxy settings.py.
export const DEFAULT_STRIP_REASONING_MIN_TOKENS = 8000;
// CONTEXT_LOG_REDUCE_OVER_CHARS decisions kept per session (trim state "logs"); same as core.LOG_MAX.
const LOG_MAX = 512;
// CONTEXT_COLD_TRIM: the cache lifetime assumed (OpenCode adds cache_control in its provider layer, unseen here), and
// the least a cold trim must free; settings.DEFAULT_COLD_TRIM_SECONDS / DEFAULT_COLD_TRIM_MIN_GAIN_TOKENS.
export const DEFAULT_COLD_TRIM_SECONDS = 3600;
export const DEFAULT_COLD_TRIM_MIN_GAIN_TOKENS = 5000;
// An idle gap longer than Anthropic's default 5-minute cache lifetime (adapters.SHORT_TTL_SECONDS).
const SHORT_TTL_SECONDS = 300;
// Per thread root, the last request's arrival (epoch seconds) and idle gaps over 5 minutes; core.LAST_REQUEST_KEY.
const LAST_REQUEST_KEY = "last_request";
// Thread keys of CONTEXT_STRIP_REASONING_EVERY, and keys a thread entry holds without a cut (core.py).
const REASON_KEYS = ["reason", "reason_fp", "reason_batch"];
const SIDE_KEYS = ["nudge_fp", "reason_batch"];
// CONTEXT_KEEP_ERRORS keeps an error result whole only up to this many characters; same as core.KEEP_ERRORS_MAX_CHARS.
export const KEEP_ERRORS_MAX_CHARS = 2000;
// A tool result reporting a failure (CONTEXT_KEEP_ERRORS; no ledger observation); same as core._ERROR_RE, per line.
const ERROR_RE = new RegExp("^Exit code [1-9]|Traceback \\(most recent call last\\)|^[A-Za-z_.]*(?:Error|Exception): "
  + "|command not found|No such file or directory|^(?:FAILED|FAIL|ERROR)(?![A-Za-z0-9_])|^error(?:\\[E[0-9]+\\])?: ");
/** Part types whose payload is binary; each is charged a flat 6400 characters (about 1600 tokens). */
const BINARY_BLOCK_TYPES = new Set(["image", "document", "input_image", "input_file", "image_url", "file", "media"]);
const BINARY_BLOCK_CHARS = 6400;
/** Bounds on an observed chars-per-token ratio, and usage samples kept per session; same as the proxy. */
const MIN_CHARS_PER_TOKEN = 1.0;
const MAX_CHARS_PER_TOKEN = 8.0;
const MAX_USAGE_THREADS = 32;
const BLOB_REF_RE = /smart_compact_get\(id="([0-9a-f]{16})"\)/;
const REMINDER_RE = /<system-reminder>[\s\S]*?<\/system-reminder>/g;
const locks = new Map();

/** Settings from the same environment variables as the proxy (proxy/smarter_compact/settings.py). */
export function trimSettings(env = process.env, home = env.HOME) {
  const int = (name, fallback) => {
    const raw = String(env[name] ?? "").trim();
    return /^[+-]?\d+$/.test(raw) ? Number.parseInt(raw, 10) : fallback;
  };
  const float = (name, fallback) => {
    const raw = String(env[name] ?? "").trim();
    const value = raw ? Number(raw) : Number.NaN;
    return Number.isFinite(value) ? value : fallback;
  };
  const positive = (name, fallback) => {
    const value = float(name, fallback);
    return value > 0 ? value : fallback;
  };
  const positiveInt = (name, fallback) => {
    const value = int(name, fallback);
    return value > 0 ? value : fallback;
  };
  const fraction = (name, fallback) => {
    const value = float(name, fallback);
    return value > 0 && value < 1 ? value : fallback;
  };
  const flag = (name, defaultVal = "1") => !["0", "false", "no", "off"].includes(String(env[name] ?? defaultVal).trim().toLowerCase());
  const overrides = parseTrimOverrides(env);
  return {
    enabled: flag("CONTEXT_PROXY_ENABLED"),
    standingInstruction: flag("CONTEXT_STANDING_INSTRUCTION"),
    // The standing instruction ends with REUSE_RULES (CONTEXT_REUSE_RULES=0 drops them).
    reuseRules: flag("CONTEXT_REUSE_RULES"),
    // A new tool result identical to one still shown in full becomes a pointer to it (CONTEXT_DEDUP_RESULTS=0 off).
    dedupResults: flag("CONTEXT_DEDUP_RESULTS"),
    // Execution ledger on new tool results, repeated reads pointed at or nudged (CONTEXT_LEDGER=1 turns it on).
    ledger: flag("CONTEXT_LEDGER", "0"),
    // Every N assistant steps, strip signed reasoning before the latest assistant message; 0 is off.
    stripReasoningEvery: Math.max(int("CONTEXT_STRIP_REASONING_EVERY", 0), 0),
    stripReasoningMinTokens: Math.max(int("CONTEXT_STRIP_REASONING_MIN_TOKENS", DEFAULT_STRIP_REASONING_MIN_TOKENS), 0),
    // A new test/build tool result over N chars keeps its failure lines and tail; 0 is off.
    logReduceOverChars: Math.max(int("CONTEXT_LOG_REDUCE_OVER_CHARS", 6000), 0),
    // The K newest error tool results stay whole through trim clearing and masking; 0 is off.
    keepErrors: Math.max(int("CONTEXT_KEEP_ERRORS", 3), 0),
    trimAtTokens: int("CONTEXT_TRIM_AT_TOKENS", DEFAULT_TRIM_AT_TOKENS),
    trimAtByModel: int("CONTEXT_TRIM_AT_TOKENS", null) === null,
    trimDefault: overrides.default ?? DEFAULT_TRIM_AT_TOKENS,
    trimFamilies: overrides.families,
    trimToTokens: positiveInt("CONTEXT_TRIM_TO_TOKENS", DEFAULT_TRIM_TO_TOKENS),
    trimToByModel: positiveInt("CONTEXT_TRIM_TO_TOKENS", null) === null,
    trimMinGainTokens: int("CONTEXT_TRIM_MIN_GAIN_TOKENS", 0),
    // After an idle gap over coldTrimSeconds the prompt cache is cold: trim to the target below the threshold too.
    coldTrim: flag("CONTEXT_COLD_TRIM", "0"),
    coldTrimSeconds: positiveInt("CONTEXT_COLD_TRIM_SECONDS", DEFAULT_COLD_TRIM_SECONDS),
    coldTrimMinGainTokens: Math.max(int("CONTEXT_COLD_TRIM_MIN_GAIN_TOKENS", DEFAULT_COLD_TRIM_MIN_GAIN_TOKENS), 0),
    // Proxy Anthropic route only (message cache_control ttl "1h" to "5m"); the plugin never sees cache_control.
    cacheTtlSplit: Math.max(int("CONTEXT_CACHE_TTL_SPLIT", 0), 0),
    // Strip prose before the cut, masking wins (CONTEXT_STRIP_PROSE=1, default off).
    stripProse: flag("CONTEXT_STRIP_PROSE", "0"),
    // Mask tool results in the prose region, wins over stripProse (CONTEXT_MASK_OBSERVATIONS=0, default on).
    maskObservations: flag("CONTEXT_MASK_OBSERVATIONS", "1"),
    // Add a progress note listing edited files and build commands (CONTEXT_PROGRESS_NOTE=0, default on).
    progressNote: flag("CONTEXT_PROGRESS_NOTE"),
    // Agent scratchpad: one nudge near the threshold, the pad in every trim note (CONTEXT_SCRATCHPAD, default on).
    scratchpad: flag("CONTEXT_SCRATCHPAD"),
    scratchpadNudgeAt: fraction("CONTEXT_SCRATCHPAD_NUDGE_AT", DEFAULT_SCRATCHPAD_NUDGE_AT),
    // Each trim first clears every tool result but the newest N, keeping all turns and calls; 0 is off.
    clearKeepResults: Math.max(int("CONTEXT_CLEAR_KEEP_RESULTS", 10), 0),
    // Wherever a tool result is cleared or masked, its call's input strings over N chars become placeholders; 0 is off.
    clearInputsOverChars: Math.max(int("CONTEXT_CLEAR_INPUTS_OVER_CHARS", DEFAULT_CLEAR_INPUTS_OVER_CHARS), 0),
    // Strip signed reasoning before the latest assistant message, never emptying one, and charge signatures (CONTEXT_STRIP_OLD_REASONING).
    stripOldReasoning: flag("CONTEXT_STRIP_OLD_REASONING"),
    compressOverTokens: int("CONTEXT_COMPRESS_OVER_TOKENS", DEFAULT_COMPRESS_OVER_TOKENS),
    charsPerToken: positive("CONTEXT_CHARS_PER_TOKEN", DEFAULT_CHARS_PER_TOKEN),
    contextDir: contextRoot(home, env),
    ...storeSettings(withProxyEnvSecrets(env, home)),
  };
}

/** The trim threshold for a request to model (an OpenCode model object, an id, or null); Settings.trim_at_for. */
export function trimAtFor(settings, model) {
  if (!settings.trimAtByModel) return settings.trimAtTokens;
  const name = modelName(model);
  if (!name) return settings.trimDefault ?? settings.trimAtTokens;
  const id = name.toLowerCase();
  for (const [family, tokens] of mergedTrimFamilies(settings.trimFamilies ?? {})) {
    if (id.includes(family)) return tokens;
  }
  return settings.trimDefault ?? settings.trimAtTokens;
}

/** The token count a trim of a request to model lands under, before the threshold caps it; Settings.trim_to_for. */
export function trimToFor(settings, model) {
  const id = settings.trimToByModel ? modelName(model)?.toLowerCase() : null;
  for (const [family, tokens] of id ? MODEL_TRIM_TO_TOKENS : []) {
    if (id.includes(family)) return tokens;
  }
  return settings.trimToTokens ?? DEFAULT_TRIM_TO_TOKENS;
}

export function textTokens(text) {
  return Math.floor((cpLen(text) + 3) / 4);
}

function chars(value) {
  if (typeof value === "string") return cpLen(value);
  if (value === null || value === undefined) return 0;
  if (Array.isArray(value)) return value.reduce((sum, v) => sum + chars(v), 0);
  if (ArrayBuffer.isView(value)) return BINARY_BLOCK_CHARS;
  if (typeof value === "object") {
    if (BINARY_BLOCK_TYPES.has(value.type)) return BINARY_BLOCK_CHARS;
    // Signed reasoning: its encrypted payload is often 100k+ characters, billed as little or no prompt text.
    if (isSignedReasoning(value)) return cpLen(typeof value.text === "string" ? value.text : "");
    return Object.entries(value).reduce((sum, [k, v]) => sum + k.length + chars(v), 0);
  }
  return String(value).length;
}

/** Characters in the values, as the trim measures a request (proxy request_chars). */
export function requestChars(...values) {
  return values.reduce((sum, v) => sum + chars(v), 0);
}

/** Characters of the signed reasoning payloads requestChars leaves out (proxy signed_chars). */
export function signedChars(value) {
  if (Array.isArray(value)) return value.reduce((sum, v) => sum + signedChars(v), 0);
  if (!isObject(value)) return 0;
  if (!isSignedReasoning(value)) return Object.values(value).reduce((sum, v) => sum + signedChars(v), 0);
  const metas = Object.values(isObject(value.providerMetadata) ? value.providerMetadata : {}).filter(isObject);
  const payload = (v) => (typeof v === "string" ? cpLen(v) : 0);
  return payload(value.encrypted) + metas.reduce((sum, m) => sum + payload(m.signature) + payload(m.redactedData), 0);
}

/** requestChars, plus signed reasoning payloads with `old` (CONTEXT_STRIP_OLD_REASONING); proxy _size. */
export function sizeChars(old, ...values) {
  return requestChars(...values) + (old ? values.reduce((sum, v) => sum + signedChars(v), 0) : 0);
}

const isObject = (value) => Boolean(value) && typeof value === "object" && !Array.isArray(value);

/** [chars, tokens] of a stored usage sample, or null when it is missing or malformed. */
function usageSample(value) {
  if (!isObject(value)) return null;
  const { chars: c, tokens: t } = value;
  return Number.isSafeInteger(c) && Number.isSafeInteger(t) && c > 0 && t > 0 ? [c, t] : null;
}

const sampleRatio = (sample, fallback) => (sample
  ? Math.min(MAX_CHARS_PER_TOKEN, Math.max(MIN_CHARS_PER_TOKEN, sample[0] / sample[1])) : fallback);
const usageThreads = (usage) => (isObject(usage) && isObject(usage.threads) ? usage.threads : {});

/**
 * Prompt tokens of a request of `size` characters in conversation thread `root`, as the proxy's
 * estimate_prompt_tokens: with a usage sample for this thread, its reported tokens plus the characters
 * added since at that sample's ratio; otherwise size at the session's latest sample's ratio, else defaultRatio.
 */
export function estimatePromptTokens(size, usage, root, defaultRatio) {
  const own = usageSample(usageThreads(usage)[root]);
  if (own) return Math.max(0, own[1] + Math.ceil((size - own[0]) / sampleRatio(own, defaultRatio)));
  return Math.ceil(size / sampleRatio(isObject(usage) ? usageSample(usage.last) : null, defaultRatio));
}

/** Store a reported prompt size into state.usage (proxy record_usage). False, state untouched, on bad values. */
export function recordUsage(state, root, size, tokens) {
  if (!usageSample({ chars: size, tokens })) return false;
  if (!isObject(state.usage)) state.usage = {};
  const threads = usageThreads(state.usage);
  delete threads[root];
  threads[root] = { chars: size, tokens };
  const keys = Object.keys(threads);
  for (const key of keys.slice(0, Math.max(0, keys.length - MAX_USAGE_THREADS))) delete threads[key];
  state.usage.threads = threads;
  state.usage.last = { chars: size, tokens };
  return true;
}

/**
 * Prompt tokens in an OpenCode 2 TokenUsage.Info ({input, output, reasoning, cache: {read, write}}), or null.
 * `input` excludes cache reads and writes (OpenCode fills it from nonCachedInputTokens), so they are added.
 */
export function promptTokensFromOpenCode(tokens) {
  if (!isObject(tokens)) return null;
  const n = (v) => (Number.isFinite(v) && v > 0 ? Math.floor(v) : 0);
  const total = n(tokens.input) + n(tokens.cache?.read) + n(tokens.cache?.write);
  return total > 0 ? total : null;
}

/**
 * Usage event counts from an OpenCode 2 TokenUsage.Info (core.usage_counts): input without cache, cache.read,
 * cache.write, output. A field that is not a non-negative number stays null. OpenCode itself reports a count its
 * provider omitted as 0, so 0 here can mean "not reported".
 */
export function usageCountsFromOpenCode(tokens) {
  const n = (v) => (Number.isFinite(v) && v >= 0 ? Math.floor(v) : null);
  const t = isObject(tokens) ? tokens : {};
  return { input_tokens: n(t.input), cache_read_tokens: n(t.cache?.read), cache_creation_tokens: n(t.cache?.write),
    output_tokens: n(t.output) };
}

function canonical(value) {
  if (value === null || value === undefined) return value ?? null;
  if (ArrayBuffer.isView(value)) {
    const bytes = Buffer.from(value.buffer, value.byteOffset, value.byteLength);
    return { $bytes: crypto.createHash("sha256").update(bytes).digest("hex") };
  }
  if (Array.isArray(value)) return value.map(canonical);
  if (typeof value === "object") {
    if (typeof value.toJSON === "function") return canonical(value.toJSON());
    const out = {};
    for (const key of Object.keys(value).sort()) {
      if (value[key] !== undefined) out[key] = canonical(value[key]);
    }
    return out;
  }
  return value;
}

/** Stable hash of a message, ignoring key order and `cache` hints on the message and its parts. */
export function fingerprint(message) {
  const { cache: _c, ...rest } = message || {};
  const content = Array.isArray(rest.content)
    ? rest.content.map((part) => {
      if (!part || typeof part !== "object") return part;
      const { cache: _p, ...kept } = part;
      return kept;
    })
    : rest.content;
  const blob = JSON.stringify(canonical({ ...rest, content }));
  return crypto.createHash("sha256").update(blob, "utf8").digest("hex").slice(0, 32);
}

export function blobId(text) {
  return crypto.createHash("sha256").update(text, "utf8").digest("hex").slice(0, 16);
}

/** `{compressed, id}` when text exceeds overTokens, else null. Same bytes as proxy compress_text. */
export function compressText(text, overTokens) {
  if (overTokens <= 0 || markerAt(text)) return null;
  const tokens = textTokens(text);
  if (tokens <= overTokens) return null;
  const lines = text.split("\n");
  let head;
  let tail;
  if (lines.length > HEAD_LINES + TAIL_LINES) {
    head = cpSlice(lines.slice(0, HEAD_LINES).join("\n"), 0, HEAD_CHARS);
    tail = cpSlice(lines.slice(-TAIL_LINES).join("\n"), -TAIL_CHARS);
  } else {
    head = cpSlice(text, 0, HEAD_CHARS);
    tail = cpSlice(text, Math.max(HEAD_CHARS, cpLen(text) - TAIL_CHARS));
  }
  const id = blobId(text);
  const out = `${MARKER_PREFIX} ${tokens} tokens — smart_compact_get(id="${id}") returns the full text]\n${head}\n…\n${tail}`;
  if (cpLen(out) >= cpLen(text)) return null;
  return { compressed: out, id };
}

function compressedBlobId(text) {
  if (!markerAt(text)) return null;
  return BLOB_REF_RE.exec(text.split("\n", 1)[0])?.[1] || null;
}

function logResultText(text) {
  const id = compressedBlobId(text);
  if (!id) return text;
  const newline = text.indexOf("\n");
  return newline >= 0 ? `[blob:${id}]\n${text.slice(newline + 1)}` : `[blob:${id}]`;
}

function compactJson(value) {
  if (typeof value === "string") {
    try { value = JSON.parse(value); } catch { return value; }
  }
  return JSON.stringify(value ?? null);
}

/**
 * A note shown inside a user or tool message, marked as Claude Code marks injected context; "" stays "".
 * Same as wrap_note in proxy adapters.py. OpenCode 2 drops mid-conversation system messages on some provider
 * paths, so the plugin always shows notes inline this way.
 */
export const wrapNote = (text) => (text ? `<system-reminder>\n${text}\n</system-reminder>` : text);

export function stripReminders(text) {
  return text.replace(REMINDER_RE, "").trim();
}

// OpenCode 2 message rules (the proxy's adapter interface).
const parts = (message) => (Array.isArray(message?.content) ? message.content : []);
export const isPinned = (message) => message?.role === "system";
export function isTurnStart(message) {
  if (message?.role !== "user") return false;
  const content = parts(message);
  return content.length > 0 && !content.some((p) => p?.type === "tool-result");
}
/** A user message that is not only tool results; the first one is pinned through every trim. */
export function isFirstUser(message) {
  return message?.role === "user" && parts(message).some((p) => p?.type !== "tool-result");
}
/** Index of the first real user turn, or null. */
export function firstUserIndex(items) {
  const i = items.findIndex(isFirstUser);
  return i >= 0 ? i : null;
}
const callIds = (message) => new Set(parts(message).filter((p) => p?.type === "tool-call").map((p) => p.id).filter((id) => id != null));
const resultIds = (message) => new Set(parts(message).filter((p) => p?.type === "tool-result").map((p) => p.id).filter((id) => id != null));
/** An agent message with no tool call: the reply that closes a turn. */
const isReply = (message) => message?.role === "assistant" && !callIds(message).size;
/** Where a tool exchange begins; a cut inside a turn never splits one. */
const isStepStart = (message) => message?.role === "assistant";

export function userText(message) {
  return parts(message).map((p) => (p?.type === "text" ? String(p.text ?? "") : `[${p?.type || "part"}]`)).join("\n");
}

function resultText(result) {
  if (!result || typeof result !== "object") return "";
  if (result.type === "content") {
    return (Array.isArray(result.value) ? result.value : [])
      .map((item) => (item?.type === "text" ? String(item.text ?? "") : `[${item?.type || "part"}]`)).join("\n");
  }
  return typeof result.value === "string" ? result.value : compactJson(result.value);
}

/** Compressed copy of a message's tool results; calls onBlob(id, fullText) for each. Returns the input when unchanged. */
export function compressMessage(message, overTokens, onBlob, skip = new Set()) {
  const content = parts(message);
  let next = null;
  content.forEach((part, i) => {
    if (part?.type !== "tool-result" || skip.has(part.id)) return;
    const result = part.result;
    let text = null;
    if (result?.type === "text" && typeof result.value === "string") text = result.value;
    else if (result?.type === "content" && Array.isArray(result.value) && result.value.length
      && result.value.every((item) => item?.type === "text")) text = result.value.map((item) => String(item.text ?? "")).join("\n");
    if (text === null) return;
    const res = compressText(text, overTokens);
    if (!res) return;
    onBlob(res.id, text);
    next ??= [...content];
    next[i] = {
      ...part,
      result: result.type === "text" ? { ...result, value: res.compressed } : { type: "content", value: [{ type: "text", text: res.compressed }] },
    };
  });
  return next ? { ...message, content: next } : message;
}

/**
 * The note becomes its own leading text part. It ends in a blank line because OpenAI-compatible
 * lowering joins a message's text parts with no separator (observed with OpenCode 2.2.1); the
 * proxy's Chat adapter uses the same "\n\n" for string content.
 */
export function prependNote(message, note) {
  return { ...message, content: [{ type: "text", text: `${wrapNote(note)}\n\n` }, ...parts(message)] };
}

function logRows(message, names) {
  const role = message.role === "assistant" ? "assistant" : message.role === "tool" ? "tool" : "user";
  const out = [];
  for (const part of parts(message)) {
    if (!part || typeof part !== "object") continue;
    switch (part.type) {
      case "text": out.push([role, "text", null, String(part.text ?? "")]); break;
      case "tool-call": out.push(["assistant", "tool_use", part.name ?? null, compactJson(part.input ?? {})]); break;
      case "tool-result":
        out.push(["tool", "tool_result", part.name ?? names.get(part.id) ?? null, logResultText(resultText(part.result))]);
        break;
      case "reasoning": if (part.text) out.push(["assistant", "thinking", null, String(part.text)]); break;
      case "compaction": out.push([role, "text", null, typeof part.text === "string" ? part.text : "[compaction]"]); break;
      default: out.push([role, "text", null, `[${part.type}]`]);
    }
  }
  return out;
}

function toolNames(items) {
  const names = new Map();
  for (const item of items) for (const p of parts(item)) if (p?.type === "tool-call") names.set(p.id, p.name);
  return names;
}

/** Call ids of our own recall and scratchpad tools: their results reach the model whole on arrival (never compressed, log-reduced or pointered); only a trim may clear them later. Proxy recall_call_ids. */
export function recallCallIds(items) {
  const out = new Set();
  for (const [id, name] of toolNames(items)) {
    if (typeof name === "string" && (RECALL_TOOL_RE.test(name) || SCRATCHPAD_TOOL_RE.test(name))) out.add(id);
  }
  return out;
}

/**
 * Database rows for every non-pinned message, chained (session-db chain); the proxy's stored_rows. Text comes
 * from the original messages, so a compressed tool result is stored in full with its blob_id. keep is false
 * for rows the view does not carry: `view` maps each index that reaches the model to its sent form (render's
 * shown), so a dropped message, a prose-stripped tool part and a masked result get false (rowsKept).
 * Returns { rows, cutRow }, cutRow being the index of the cut turn's first row or null.
 */
export function storedRows(original, items, cut, view) {
  const names = toolNames(items);
  const rows = [];
  let cutRow = null;
  original.forEach((item, j) => {
    if (isPinned(item)) return;
    const base = logRows(item, names);
    const shown = items[j] === item ? base : logRows(items[j], names);
    if (cut !== null && j === cut && base.length) cutRow = rows.length;
    const keep = rowsKept(shown, view.get(j) ?? null, items[j], names);
    base.forEach(([role, kind, name, text], k) => {
      const blob = shown !== base && k < shown.length ? blobRef(shown[k][3]) : null;
      rows.push({ role, kind, name, text, blob_id: blob, keep: k < keep.length && keep[k] });
    });
  });
  return { rows: rows.length ? chain(rows) : rows, cutRow };
}

const sameRow = (a, b) => a.length === b.length && a.every((v, i) => v === b[i]);

/**
 * Per row of item (shown = its rows), whether sent, the form the view carries (null: absent), still has it; reasoning
 * rows of a sent message count as kept, so only dropping a message or its tool parts, or masking a result (same row
 * count, changed text), clears the flag. Proxy _rows_kept.
 */
function rowsKept(shown, sent, item, names) {
  if (sent === item) return shown.map(() => true);
  if (sent === null) return shown.map(() => false);
  const left = logRows(sent, names);
  if (left.length === shown.length) return shown.map((row, k) => sameRow(left[k], row) || row[1] === "thinking");
  const out = [];
  let p = 0;
  for (const row of shown) {
    const hit = p < left.length && sameRow(left[p], row);
    out.push(hit || row[1] === "thinking");
    if (hit) p += 1;
  }
  return out;
}

/** Write the request's messages to sessions/<sid>.db; never throws (a database error is logged). */
function storeMessages(store, sid, settings, original, items, cut, view, ts, deps) {
  let file = null;
  try {
    file = store.dbPath(sid);
    const { rows, cutRow } = storedRows(original, items, cut, view);
    if (!rows.length) return;
    const added = openSession(file).sync(rows, cutRow, ts);
    if (added && settings.embeddingModel) {
      void scheduleEmbedding(file, { model: settings.embeddingModel, url: settings.embeddingUrl, apiKey: settings.embeddingApiKey,
        timeoutS: settings.embeddingTimeoutS, maxChars: settings.embeddingMaxChars },
        { fetchFn: deps.fetch, onError: logFailOpen });
    }
  } catch (error) {
    if (file) dropSession(file);
    logFailOpen(error);
  }
}

// Tool exchanges a cut inside the in-flight turn always keeps; older ones stay while the request fits under
// the trim target (trimToFor, capped at the trim threshold).
const MIN_TAIL_STEPS = 3;

/** Index of the agent reply just before the cut turn (kept with it, so the turn reads in context), or null. */
function prevReplyIndex(items, cut, first) {
  const j = cut - 1;
  return j < 0 || j === first || isPinned(items[j]) || !isReply(items[j]) ? null : j;
}

/**
 * Indices the view keeps: pinned messages, the first user turn, the reply before the cut turn, the cut turn,
 * and every later message except the in-turn gap [cut + 1, gap). Everything when cut is null.
 */
export function keptIndices(items, cut, gap = null, first = null) {
  if (cut === null) return new Set(items.keys());
  const head = new Set([first, prevReplyIndex(items, cut, first), cut]);
  const keep = new Set();
  items.forEach((x, j) => {
    if (head.has(j) || isPinned(x) || (j > cut && (gap === null || j >= gap))) keep.add(j);
  });
  return keep;
}

const droppable = (items, cut, gap, first) => {
  const keep = keptIndices(items, cut, gap, first);
  return items.filter((_, j) => !keep.has(j));
};

/** Whether the kept and dropped messages share no call/result pair. */
function pairsIntact(items, keep) {
  const ids = (fn, inside) => new Set(items.flatMap((x, j) => (keep.has(j) === inside ? [...fn(x)] : [])));
  const [dCalls, dResults] = [ids(callIds, false), ids(resultIds, false)];
  const [kCalls, kResults] = [ids(callIds, true), ids(resultIds, true)];
  return ![...dCalls].some((id) => kResults.has(id)) && ![...kCalls].some((id) => dResults.has(id));
}

/** Index of the last real user turn > after whose kept part shares no call/result pair with the dropped part. */
export function chooseCut(items, after, first = null) {
  for (let i = items.length - 1; i > after; i--) {
    if (!isTurnStart(items[i])) continue;
    const keep = keptIndices(items, i, null, first);
    if (keep.size === items.length) return null;
    if (pairsIntact(items, keep)) return i;
  }
  return null;
}

function lastTurnIndex(items) {
  for (let i = items.length - 1; i >= 0; i--) if (isTurnStart(items[i])) return i;
  return null;
}

/**
 * Start of the kept tail for a cut inside the turn at `cut`, > after; null when no gap is possible. The tail
 * keeps the newest MIN_TAIL_STEPS tool exchanges, then older ones while fits(gap) holds (the request stays
 * under the trim target: trimToFor, capped at the trim threshold). An exchange is never split: the tail starts
 * at a step start.
 */
export function chooseGap(items, cut, after, first, fits) {
  const starts = [];
  for (let j = cut + 2; j < items.length; j++) if (isStepStart(items[j])) starts.push(j);
  let best = null;
  let n = 0;
  for (const g of starts.reverse()) {
    n += 1;
    if (g <= after) break;
    if (n < MIN_TAIL_STEPS) continue;
    if (n > MIN_TAIL_STEPS && !fits(g)) break;
    if (pairsIntact(items, keptIndices(items, cut, g, first))) best = g;
  }
  return best;
}

/**
 * The kept messages (keptIndices) in order, the cut turn carrying the note, with signed reasoning stripped from
 * kept assistant messages before `strip` (default and cap: stripTop, with `old` the latest assistant message, else the
 * latest assistant run; see withoutReasoning and bareOf). `decorate(message)`
 * adds per-message parts at render time only; trimming, fingerprints and log rows always see the undecorated messages.
 * `mode` (regionMode) also strips the proseIds tool pairs (true; adjacent stripped messages of one role merge) or
 * masks their results ("mask"); every tool result before `clear` is masked too (cleared). Wherever a result is
 * masked so are its call's input strings over `inputs` chars (maskInputs). `shown`, a Map when given,
 * gets each index that reaches the view mapped to its sent form (no note, undecorated, unmerged). Proxy render.
 */
export function render(items, cut, note, first = null, gap = null, decorate = (m) => m, strip = null, clear = null,
  mode = false, shown = null, inputs = 0, old = false, exempt = null) {
  if (cut === null) {
    if (shown) items.forEach((x, j) => shown.set(j, x));
    return items.map(decorate);
  }
  const keep = keptIndices(items, cut, gap, first);
  const top = stripTop(items, old);
  const bound = strip === null ? top : Math.min(strip, top);
  // CONTEXT_KEEP_ERRORS: no result or input of a call in `exempt` is masked (proxy _Exempt).
  const ids = without(mode ? proseIds(items, keep, bound, cut, mode) : new Set(), exempt);
  const calls = without(inputs > 0 ? clearedCalls(items, clear, first) : new Set(), exempt);
  const out = [];
  let mergeable = false;
  for (let j = 0; j < items.length; j++) {
    if (!keep.has(j)) continue;
    let x = items[j];
    let region = false;
    if (j !== cut && j < bound) {
      x = bareOf(x, old);
      if (x === null) continue;
      if (ids.size && !isPinned(x) && mode === "mask") x = maskInputs(maskResults(x, ids), ids, inputs);
      else if (ids.size && !isPinned(x)) {
        x = withoutToolCalls(x, ids);
        if (x === null) continue;
        region = true;
      }
    }
    x = cleared(x, j, clear, first, inputs, calls, exempt);
    shown?.set(j, x);
    if (j === cut) x = prependNote(decorate(x), note);
    else {
      x = decorate(x);
      if (region && mergeable && out[out.length - 1]?.role === x.role) {
        out[out.length - 1] = mergeMessages(out[out.length - 1], x);
        continue;
      }
    }
    out.push(x);
    mergeable = region;
  }
  return out;
}

/** ids without the members of exempt; ids itself when exempt is empty. */
function without(ids, exempt) {
  return exempt?.size && ids?.size ? new Set([...ids].filter((id) => !exempt.has(id))) : ids;
}

/** The message with each tool result whose id is in ids (every one when ids is null) and not in exempt, and longer than its maskPlaceholder replaced by it; itself when none changes. */
function maskResults(message, ids, exempt = null) {
  const content = parts(message);
  let next = null;
  content.forEach((p, i) => {
    if (p?.type !== "tool-result" || (ids && !ids.has(p.id)) || exempt?.has(p.id)) return;
    const text = resultText(p.result);
    const placeholder = maskPlaceholder(text);
    if (cpLen(text) <= cpLen(placeholder) || text.startsWith(DEDUP_PREFIX) || text.startsWith(REUSE_PREFIX)) return;
    next ??= [...content];
    next[i] = { ...p, result: { type: p.result?.type === "error" ? "error" : "text", value: placeholder } };
  });
  return next ? { ...message, content: next } : message;
}

/** The message with each tool call whose id is in ids carrying maskedInput(input, over); itself when off or none changes. Proxy masked_inputs. */
function maskInputs(message, ids, over) {
  if (!(over > 0) || !ids?.size) return message;
  const content = parts(message);
  let next = null;
  content.forEach((p, i) => {
    if (p?.type !== "tool-call" || !ids.has(p.id) || !("input" in p)) return;
    const input = maskedInput(p.input, over);
    if (input === p.input) return;
    next ??= [...content];
    next[i] = { ...p, input };
  });
  return next ? { ...message, content: next } : message;
}

/**
 * Write blobs/<id>.txt for each result the view shows as its maskPlaceholder, and each tool-input string it shows as
 * its inputPlaceholder, before the request leaves. Proxy _write_masked_blobs.
 */
function writeMaskedBlobs(store, sent, shown) {
  for (const [j, x] of shown) {
    if (x === sent[j]) continue;
    const now = new Map(parts(x).filter((p) => p?.type === "tool-result").map((p) => [p.id, resultText(p.result)]));
    for (const p of parts(sent[j])) {
      if (p?.type !== "tool-result" || !now.has(p.id)) continue;
      const text = resultText(p.result);
      if (now.get(p.id) === maskPlaceholder(text) && !compressedBlobId(text)) store.writeBlob(blobId(text), text);
    }
    const inputs = new Map(parts(x).filter((p) => p?.type === "tool-call").map((p) => [p.id, p.input]));
    for (const p of parts(sent[j])) {
      if (p?.type !== "tool-call" || !inputs.has(p.id)) continue;
      for (const text of clearedInputs(p.input, inputs.get(p.id))) store.writeBlob(blobId(text), text);
    }
  }
}

/** The message without its tool-call and tool-result parts whose id is in ids: itself when it has none, null when nothing is left. */
function withoutToolCalls(message, ids) {
  if (!Array.isArray(message.content)) return message;
  const kept = message.content.filter((p) => !((p?.type === "tool-call" || p?.type === "tool-result") && ids.has(p.id)));
  if (kept.length === message.content.length) return message;
  return kept.length ? { ...message, content: kept } : null;
}

/** One message holding a's parts then b's (same role). */
function mergeMessages(a, b) {
  const blocks = (m) => (typeof m.content === "string" ? [{ type: "text", text: m.content }] : parts(m));
  return { ...a, content: [...blocks(a), ...blocks(b)] };
}

/**
 * The message with every tool result longer than its maskPlaceholder replaced by it when message j sits before the
 * clear bound (the first user turn and pinned messages aside), and the inputs of its calls in `calls` (clearedCalls)
 * over `inputs` chars; the message itself otherwise. Proxy _cleared.
 */
export function cleared(message, j, clear, first, inputs = 0, calls = null, exempt = null) {
  if (clear === null || j >= clear || j === first || isPinned(message)) return message;
  return maskInputs(maskResults(message, null, exempt), without(calls, exempt), inputs);
}

/** Call ids whose result the clear bound clears: every result of a message before it (first and pinned aside). Proxy _cleared_calls. */
export function clearedCalls(items, clear, first) {
  const ids = new Set();
  if (clear === null) return ids;
  for (let j = 0; j < Math.min(clear, items.length); j++) {
    if (j !== first && !isPinned(items[j])) for (const id of resultIds(items[j])) ids.add(id);
  }
  return ids;
}

/**
 * Index of the message holding the keepResults-th newest tool result: it and every later message keep their
 * results, earlier ones are cleared. null when no earlier message holds a result to clear. Proxy clear_bound.
 */
export function clearBound(items, keepResults) {
  let seen = 0;
  for (let j = items.length - 1; j >= 0; j--) {
    seen += resultIds(items[j]).size;
    if (seen >= keepResults) return items.slice(0, j).some((x) => resultIds(x).size) ? j : null;
  }
  return null;
}

/** The clear bound a trim now records: the newer of the current one and clearBound; null when clearing is off. */
function nextClear(items, keepResults, clear) {
  if (!(keepResults > 0)) return null;
  const bounds = [clear, clearBound(items, keepResults)].filter((b) => b !== null);
  return bounds.length ? Math.max(...bounds) : null;
}

/** Whether bound next masks a result (or an input) of a kept message that bound old leaves whole. */
function clearsMore(items, keep, old, next, first, inputs = 0, exempt = null) {
  if (next === null) return false;
  const calls = inputs > 0 ? clearedCalls(items, next, first) : null;
  return [...keep].some((j) => (old === null || j >= old) && cleared(items[j], j, next, first, inputs, calls, exempt) !== items[j]);
}

/** Index where the trailing run of assistant messages ending at the last one starts; items.length if none. */
export function latestRunStart(items) {
  let last = -1;
  for (let j = items.length - 1; j >= 0; j--) if (items[j]?.role === "assistant") { last = j; break; }
  if (last < 0) return items.length;
  while (last > 0 && items[last - 1]?.role === "assistant") last--;
  return last;
}

/** Index of the latest assistant message; items.length if none (proxy latest_message_start). */
export function latestMessageStart(items) {
  for (let j = items.length - 1; j >= 0; j--) if (items[j]?.role === "assistant") return j;
  return items.length;
}

/** Where a trim's reasoning strip ends: the latest assistant message with `old`, else the latest run; proxy strip_top. */
export function stripTop(items, old) {
  return old ? latestMessageStart(items) : latestRunStart(items);
}

/** x without its signed reasoning; null when nothing is left, except with `old` it comes back whole; proxy _bare. */
export function bareOf(x, old) {
  const bare = withoutReasoning(x);
  return bare === null && old ? x : bare;
}

/**
 * Whether a reasoning part is signed against the conversation before it: Anthropic thinking (`encrypted` or a
 * provider `signature`) or redacted thinking (provider `redactedData`). OpenAI reasoning
 * (`reasoningEncryptedContent`) is not tied to the prefix and stays, as in the proxy's Responses adapter.
 */
export function isSignedReasoning(part) {
  if (part?.type !== "reasoning") return false;
  if (typeof part.encrypted === "string" && part.encrypted) return true;
  return Object.values(isObject(part.providerMetadata) ? part.providerMetadata : {}).some((meta) => isObject(meta)
    && ((typeof meta.signature === "string" && meta.signature) || (typeof meta.redactedData === "string" && meta.redactedData)));
}

/**
 * The message without its signed reasoning parts when it is an assistant message: itself when it has none, null when
 * nothing is left. render applies it before the strip bound, which the trim records as the start of the latest
 * assistant run at that moment, so reasoning produced after the trim stays and the stripped prefix is fixed until the
 * next trim. Same rule as strip_reasoning in proxy/smarter_compact/core.py.
 */
export function withoutReasoning(x) {
  if (x?.role !== "assistant" || !Array.isArray(x.content) || !x.content.some(isSignedReasoning)) return x;
  const content = x.content.filter((p) => !isSignedReasoning(p));
  return content.length ? { ...x, content } : null;
}

/** Index of the message with fingerprint `want` (idx when it still matches), searching from lo; null if absent. */
function locate(items, idx, want, lo = 0) {
  if (Number.isInteger(idx) && idx >= lo && idx < items.length && fingerprint(items[idx]) === want) return idx;
  for (let i = lo; i < items.length; i++) if (fingerprint(items[i]) === want) return i;
  return null;
}

// A caller with no tool list falls back to the bare names, which is also the historical text; an
// offered list resolves to the names the model can actually call. A request that offers no recall
// tool is NOT announced as unsearchable: the pointer's job is to route the model to the store, and
// telling it the door is shut is how recall stops being used. Whether the tools were really offered
// is measured, not asserted (bench/swebench/request_capture.py).
export const LEGACY_RECALL_NAMES = recallToolNames(["smart_compact_search", "smart_compact_sql"]);

export function pointerText(sid, dropped, names = LEGACY_RECALL_NAMES) {
  return `[smarter-compact: continuing session ${sid}; ${dropped} earlier messages are NOT in your context. Older `
    + "turns were moved out to save space, so a gap in what you see (missing batches, lines, keys) is in them. "
    + "Continue the task from where you were. "
    + "Nothing here is or was ever summarized: this trim wrote no summary of the dropped messages, so do not "
    + "treat a \"summary of earlier context\" you seem to remember as their record. The only complete record of "
    + "them is this session's store. "
    + availability(names)
    + `You MUST call ${names.search}(session_id="${sid}", query=... or pattern=...) before relying on `
    + 'anything in them, and never claim "not found", "never received" or "no search tool" '
    + "without calling it. "
    + "Dropped user turns may hold instructions or corrections that still apply: search them "
    + `(${names.search} with role="user") before changing course. `
    + "To recover a file you read earlier, search for the text you need instead of reading it again, "
    + "and re-read only the lines you still lack (offset/limit). "
    + "Every message of this session is stored and queryable with "
    + `${names.search} and ${names.sql}(session_id="${sid}", sql=...).]`;
}

export function addStandingInstruction(system, settings, names) {
  if (!settings.enabled || !settings.standingInstruction || !Array.isArray(system)) return false;
  // Marker, not exact text: the instruction now carries this request's tool names, so two
  // requests of one session can legitimately differ.
  const injected = (part) => String((typeof part === "string" ? part : part?.text) || "");
  if (system.some((part) => injected(part).includes(STANDING_MARKER))) return false;
  system.push({ type: "text", text: standingInstruction(names, settings.reuseRules !== false) });
  return true;
}

/** Start of every cleared or masked result's placeholder; detection matches on it. Same as compress.MASK_PREFIX. */
export const MASK_PREFIX = "[smarter-compact: tool output cleared to save context;";

/** A cleared result's text, naming its blob; a function of text, so cache-stable. Same as compress.mask_placeholder. */
export function maskPlaceholder(text) {
  return `${MASK_PREFIX} smart_compact_get(id="${compressedBlobId(text) || blobId(text)}") returns it]`;
}

/** Start of a repeated tool result's pointer (CONTEXT_DEDUP_RESULTS); never masked. Same as compress.DEDUP_PREFIX. */
export const DEDUP_PREFIX = "[smarter-compact: output identical to an earlier tool result still shown above;";
/** Only a result at least this long becomes a dedup pointer. Same as compress.DEDUP_MIN_CHARS. */
export const DEDUP_MIN_CHARS = 200;
/** Dedup decisions kept per session (trim state "dedup"), oldest dropped first. Same as core.DEDUP_MAX. */
const DEDUP_MAX = 512;

/** A repeated result's text, naming the blob of its full text; a function of text, so cache-stable. compress.dedup_placeholder. */
export function dedupPlaceholder(text) {
  return `${DEDUP_PREFIX} smart_compact_get(id="${compressedBlobId(text) || blobId(text)}") returns it]`;
}

/** Map of id to text for the message's tool-result parts that are text only (an image or file differs). */
function resultTexts(message) {
  const out = new Map();
  for (const p of parts(message)) {
    if (p?.type !== "tool-result") continue;
    if (p.result?.type === "content" && !(Array.isArray(p.result.value) && p.result.value.every((item) => item?.type === "text"))) continue;
    out.set(p.id, resultText(p.result));
  }
  return out;
}

/** The message with tool-result id's result replaced by text; itself when it has none. */
function replacedResult(message, id, text) {
  const content = parts(message);
  const at = content.findIndex((p) => p?.type === "tool-result" && p.id === id);
  if (at < 0) return message;
  const next = [...content];
  next[at] = { ...content[at], result: { type: content[at].result?.type === "error" ? "error" : "text", value: text } };
  return { ...message, content: next };
}

/** Map of index to decisions for each recorded dedup decision whose item is still in items: at its index, else the last with its fingerprint. Proxy _dedup_found. */
function dedupFound(items, decisions) {
  const out = new Map();
  const fps = new Map();
  let index = null;
  for (const d of decisions) {
    if (!isObject(d)) continue;
    let j = null;
    if (Number.isInteger(d.at) && d.at >= 0 && d.at < items.length) {
      if (!fps.has(d.at)) fps.set(d.at, fingerprint(items[d.at]));
      if (fps.get(d.at) === d.fp) j = d.at;
    }
    if (j === null) {
      if (index === null) {
        index = new Map();
        items.forEach((x, i) => index.set(fingerprint(x), i));
      }
      j = index.get(d.fp) ?? null;
    }
    if (j !== null) out.set(j, [...(out.get(j) ?? []), d]);
  }
  return out;
}

/** items with each found decision's result shown as its dedupPlaceholder; items itself when none changes. Proxy _deduped. */
function deduped(items, found) {
  let out = null;
  for (const [j, decisions] of found) {
    let x = items[j];
    for (const d of decisions) {
      const text = resultTexts(x).get(d.id);
      if (text === undefined || (compressedBlobId(text) || blobId(text)) !== d.bid) continue;
      // A reuse decision recorded before reuse became byte-identical-only keeps its recorded pointer.
      const ptr = d.kind === "reuse" && "ptr" in d ? d.ptr : dedupPlaceholder(text);
      if (typeof ptr === "string") x = replacedResult(x, d.id, ptr);
    }
    if (x !== items[j]) {
      out ??= [...items];
      out[j] = x;
    }
  }
  return out ?? items;
}

/** [decision, text] pairs for each new tool result (after the last assistant message) of at least DEDUP_MIN_CHARS identical to an earlier one the view (shown) carries in full. Proxy _dedup_new. */
function dedupNew(items, shown, found, skip = new Set()) {
  const last = items.findLastIndex((x) => x?.role === "assistant");
  if (last < 0) return [];
  const seen = new Map();
  const out = [];
  for (const j of [...shown.keys()].sort((a, b) => a - b)) {
    const decided = new Set((found.get(j) ?? []).map((d) => d.id));
    const whole = j > last ? resultTexts(items[j]) : new Map();
    const orig = resultTexts(items[j]);
    for (const [id, now] of resultTexts(shown.get(j))) {
      // An earlier result shown in full plus the ledger block the plugin appended to its text counts as its own text.
      const full = orig.get(id);
      const text = j <= last && full !== undefined && now.startsWith(full) ? full : now;
      if (j > last && !decided.has(id) && !skip.has(id) && seen.has(text) && cpLen(text) >= DEDUP_MIN_CHARS && whole.get(id) === text
          && !text.startsWith(DEDUP_PREFIX) && !text.startsWith(MASK_PREFIX) && !text.startsWith(REUSE_PREFIX)) {
        out.push([{ at: j, fp: fingerprint(items[j]), id, src: seen.get(text), bid: compressedBlobId(text) || blobId(text) }, text]);
      } else if (!seen.has(text)) seen.set(text, id);
    }
  }
  return out;
}

const lastAssistant = (items) => items.findLastIndex((x) => x?.role === "assistant");

/** Whether a tool result reports a failure: an error result, else an ERROR_RE line (proxy _is_error). */
export function isErrorResult(isError, text) {
  return Boolean(isError) || String(text || "").split("\n").some((line) => ERROR_RE.test(line));
}

/**
 * {items, fresh}: items with each recorded CONTEXT_LOG_REDUCE_OVER_CHARS decision's result, and each new one's, shown as
 * its reduceLog form, then compressed as usual; fresh the new decisions (text results after the last assistant
 * message that reduceLog takes). Proxy _log_reduced.
 */
function logReduced(original, items, decisions, settings, store, skip = new Set()) {
  const found = decisions.length ? dedupFound(original, decisions) : new Map();
  const last = lastAssistant(original);
  const fresh = [];
  for (let j = last >= 0 ? last + 1 : original.length; j < original.length; j++) {
    if (found.has(j)) continue;
    for (const [id, text] of resultTexts(original[j])) {
      if (skip.has(id) || !reduceLog(text, settings.logReduceOverChars)) continue;
      const d = { at: j, fp: fingerprint(original[j]), id, bid: blobId(text) };
      fresh.push(d);
      found.set(j, [...(found.get(j) ?? []), d]);
    }
  }
  let out = null;
  for (const [j, ds] of found) {
    let x = original[j];
    for (const d of ds) {
      const text = resultTexts(x).get(d.id);
      const r = text !== undefined && blobId(text) === d.bid ? reduceLog(text, 1) : null;
      if (!r) continue;
      store.writeBlob(r.id, text);
      x = replacedResult(x, d.id, r.reduced);
    }
    if (x === original[j]) continue;
    if (settings.compressOverTokens > 0) x = compressMessage(x, settings.compressOverTokens, (id, t) => store.writeBlob(id, t), skip);
    out ??= [...items];
    out[j] = x;
  }
  return { items: out ?? items, fresh };
}

/** Call ids of the k newest error tool results of at most KEEP_ERRORS_MAX_CHARS the view (shown) still carries whole, newest first; proxy _error_ids. */
function errorIds(items, k, shown) {
  const out = [];
  for (let j = items.length - 1; j >= 0; j--) {
    if (!shown.has(j)) continue;
    const results = new Map(parts(items[j]).filter((p) => p?.type === "tool-result").map((p) => [p.id, p.result]));
    const now = new Map(parts(shown.get(j)).filter((p) => p?.type === "tool-result").map((p) => [p.id, resultText(p.result)]));
    for (const [id, result] of [...results].reverse()) {
      const text = resultText(result);
      if (cpLen(text) <= KEEP_ERRORS_MAX_CHARS && isErrorResult(result?.type === "error", text) && now.has(id) && now.get(id) === text) {
        out.push(id);
        if (out.length >= k) return out;
      }
    }
  }
  return out;
}

/** items with signed reasoning stripped from every assistant message before bound, never emptying one; proxy _reason_stripped. */
function reasonStripped(items, bound) {
  if (bound === null) return items;
  let out = null;
  for (let j = 0; j < Math.min(bound, items.length); j++) {
    const x = items[j];
    const bare = x?.role === "assistant" ? bareOf(x, true) : x;
    if (bare === null || bare === x) continue;
    out ??= [...items];
    out[j] = bare;
  }
  return out ?? items;
}

const pickKeys = (entry, keys) => Object.fromEntries(Object.entries(entry ?? {}).filter(([k]) => keys.includes(k)));

/** [j, result id, tool name, args, text, isError] of each tool result whose call the items hold; proxy _ledger_steps. */
function ledgerSteps(items) {
  const calls = new Map();
  const steps = [];
  items.forEach((x, j) => {
    for (const p of parts(x)) if (p?.type === "tool-call") calls.set(p.id, [p.name, isObject(p.input) ? p.input : {}]);
    const results = new Map(parts(x).filter((p) => p?.type === "tool-result").map((p) => [p.id, p.result]));
    for (const [id, result] of results) {
      if (!calls.has(id)) continue;
      const [name, args] = calls.get(id);
      calls.delete(id);
      const text = resultText(result);
      steps.push([j, id, name, args, text, isErrorResult(result?.type === "error", text)]);
    }
  });
  return steps;
}

/** Whether the view (shown) carries result id of items[j] in full; proxy _visible. */
function visibleIn(items, shown, j, id) {
  const full = j >= 0 && j < items.length ? resultTexts(items[j]).get(id) : undefined;
  const now = shown.has(j) ? resultTexts(shown.get(j)).get(id) : undefined;
  return full !== undefined && now !== undefined && now.startsWith(full);
}

/** {state, reuse: [[decision, text]], nudges} for the new tool results under CONTEXT_LEDGER; proxy _ledger_new. */
function ledgerNew(items, shown, found, fresh, skip = new Set()) {
  const last = lastAssistant(items);
  if (last < 0) return { state: null, reuse: [], nudges: [] };
  const { state, repeats } = walk(ledgerSteps(items), last + 1);
  const key = (j, id) => JSON.stringify([j, id]);
  const decided = new Set([...[...found].flatMap(([j, ds]) => ds.map((d) => key(j, d.id))), ...fresh.map(([d]) => key(d.at, d.id))]);
  const gone = new Set(fresh.map(([d]) => key(d.at, d.src)));
  const reuse = [];
  const nudges = [];
  for (const r of repeats) {
    if (decided.has(key(r.j, r.rid)) || skip.has(r.rid)) continue;
    if ("test" in r) {
      nudges.push(nudgeLine(r));
      continue;
    }
    const shownCopies = r.covers.filter(([j, id]) => !decided.has(key(j, id)) && !gone.has(key(j, id)) && visibleIn(items, shown, j, id));
    const text = resultTexts(items[r.j]).get(r.rid);
    const src = shownCopies.find(([j, id]) => resultTexts(items[j]).get(id) === text) ?? null;
    if (!shownCopies.length) nudges.push(nudgeLine(r));
    else if (src !== null && text !== undefined && cpLen(text) >= DEDUP_MIN_CHARS
        && ![DEDUP_PREFIX, MASK_PREFIX, REUSE_PREFIX].some((prefix) => text.startsWith(prefix))) {
      // "kind": "reuse" ties the decision to CONTEXT_LEDGER; it renders as the dedup pointer.
      const bid = compressedBlobId(text) || blobId(text);
      reuse.push([{ kind: "reuse", at: r.j, fp: fingerprint(items[r.j]), id: r.rid, src: src[1], bid }, text]);
      decided.add(key(r.j, r.rid));
    }
  }
  return { state, reuse, nudges };
}

/** items with each recorded ledger block at or after index lo appended to its message; proxy _ledgered. */
function ledgered(items, found, lo) {
  for (const j of [...found.keys()].sort((a, b) => a - b)) {
    if (lo !== null && j < lo) continue;
    for (const d of found.get(j)) items = nudged(items, j, typeof d.text === "string" ? d.text : null);
  }
  return items;
}

/** Index of the newest message that is not pinned (a trailing system message); proxy _newest_unpinned. */
function newestUnpinned(items) {
  const j = items.findLastIndex((x) => !isPinned(x));
  return j < 0 ? items.length - 1 : j;
}

/** The ledger block decision for this request's newest unpinned message, or null; proxy _ledger_block. */
function newLedgerBlock(items, sent, state, nudges, found) {
  const j = newestUnpinned(items);
  const last = lastAssistant(items);
  if (state === null || last < 0 || j <= last || found.has(j) || !parts(items[j]).some((p) => p?.type === "tool-result")) return null;
  const tail = nudges.length ? `\nRepeats in this batch:\n${nudges.slice(0, 3).join("\n")}` : "";
  const core = ledgerBlock(state, cpLen(tail));
  const prev = found.size ? found.get(Math.max(...found.keys())).at(-1)?.core ?? null : null;
  if (!(core || tail) || (!tail && coreId(core) === prev)) return null;
  const text = (core || LEDGER_HEAD) + tail;
  if (nudged(sent, j, text) === sent) return null;
  return { at: j, fp: fingerprint(items[j]), text, core: coreId(core) };
}

/** Start of every cleared tool-input string's placeholder; never re-masked. Same as compress.INPUT_MASK_PREFIX. */
export const INPUT_MASK_PREFIX = "[smarter-compact: tool input cleared to save context;";
// Characters of an input placeholder besides its count digits; same as compress._INPUT_PLACEHOLDER_FIXED.
const INPUT_PLACEHOLDER_FIXED = INPUT_MASK_PREFIX.length + ' chars; smart_compact_get(id="'.length + 16 + '") returns it]'.length + 1;

/** A cleared tool-input string, naming its length and blob; a function of text, so cache-stable. Same as compress.input_placeholder. */
export function inputPlaceholder(text) {
  return `${INPUT_MASK_PREFIX} ${cpLen(text)} chars; smart_compact_get(id="${blobId(text)}") returns it]`;
}

/** Whether a tool-input string over `over` chars becomes its inputPlaceholder: longer than it, not one already. */
export function inputClearable(text, over = 0) {
  if (text.length <= over) return false;
  const n = cpLen(text);
  return n > over && n > INPUT_PLACEHOLDER_FIXED + String(n).length && !text.startsWith(INPUT_MASK_PREFIX);
}

/** value (a tool call's input) with every string, nested ones too, that inputClearable over `over` replaced by its inputPlaceholder; itself when none changes. Proxy masked_input. */
export function maskedInput(value, over) {
  if (typeof value === "string") return inputClearable(value, over) ? inputPlaceholder(value) : value;
  if (Array.isArray(value)) {
    const out = value.map((v) => maskedInput(v, over));
    return out.every((v, i) => v === value[i]) ? value : out;
  }
  if (isObject(value) && !ArrayBuffer.isView(value)) {
    let changed = false;
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      out[k] = maskedInput(v, over);
      changed ||= out[k] !== v;
    }
    return changed ? out : value;
  }
  return value;
}

/** The strings of value that shown carries as their inputPlaceholder (walked in parallel). Proxy cleared_inputs. */
function clearedInputs(value, shown) {
  if (typeof value === "string") return typeof shown === "string" && shown !== value && shown === inputPlaceholder(value) ? [value] : [];
  if (Array.isArray(value) && Array.isArray(shown)) return value.flatMap((v, i) => (i < shown.length ? clearedInputs(v, shown[i]) : []));
  if (isObject(value) && isObject(shown)) return Object.entries(value).flatMap(([k, v]) => (k in shown ? clearedInputs(v, shown[k]) : []));
  return [];
}

/** How to get back a result shown as a maskPlaceholder; same text as mask_text in proxy/smarter_compact/core.py. */
// Opens the note of a trim that only cleared tool outputs: no message is gone; proxy CLEARED_LEAD.
const CLEARED_LEAD = "Older tool outputs were moved out to save space. Continue the task from where you were.";

export function maskText(sid, names = LEGACY_RECALL_NAMES) {
  return `Older tool outputs in your context read "${MASK_PREFIX} smart_compact_get(id="<id>") returns it]". `
    + "Each names its own id: smart_compact_get with that id returns the full output. If that fails, find its call with "
    + `${names.search}(session_id="${sid}", query=<words from the call>); its output is the first tool_result `
    + `row of the same tool after that seq: ${names.sql}(session_id="${sid}", sql="SELECT text FROM messages `
    + "WHERE kind = 'tool_result' AND name = '<tool>' AND seq > <call seq> ORDER BY seq LIMIT 1\").";
}

/** The once-per-trim-cycle nudge to save task state; same text as scratchpad_nudge in proxy core.py. */
export function scratchpadNudge(sid, names) {
  const load = names.scratchpadLoad ? ` (load it first by calling ${names.scratchpadLoad})` : "";
  return "<compact-scratchpad-nudge>Your context will be trimmed soon: older turns will be removed. Save your "
    + "notes, then keep working: this is automatic, not a stopping point. The user is not waiting for an update. "
    + `If you have no scratchpad yet, create one with ${names.scratchpad}(session_id="${sid}", `
    + `command="create", file_text=...)${load}, at most ${SCRATCHPAD_MAX_CHARS} characters. Notes for yourself, `
    + "tersely:\n"
    + "- goal and how it is checked\n- what works, what fails\n- decisions and why\n"
    + "- facts you would otherwise re-discover (paths, commands, values)\n"
    + "- files you wrote (paths) and what each holds\n- your next step\n"
    + "No logs, code or command output. It is kept and shown after every trim; if you already have one, update it "
    + "(str_replace) only when the goal, plan or a key fact changed.</compact-scratchpad-nudge>";
}

/** True when the request offers the scratchpad tool, or Claude Code's deferred-tool list names it; proxy scratchpad_available. */
export function scratchpadAvailable(names, messages) {
  if (names.scratchpadOffered) return true;
  if (!names.scratchpadLoad) return false;
  return (messages || []).some((m) => {
    const content = m?.content;
    const texts = typeof content === "string" ? [content] : Array.isArray(content) ? content.map((b) => b?.text) : [];
    return texts.some((t) => typeof t === "string" && t.includes(DEFERRED_TOOLS_HEAD) && t.includes(names.scratchpad));
  });
}

/** The trim note's labelled block carrying the agent's pad, capped at SCRATCHPAD_MAX_CHARS; proxy scratchpad_block. */
export function scratchpadBlock(text) {
  return `${SCRATCHPAD_LABEL}\n<compact-scratchpad>\n${text.slice(0, SCRATCHPAD_MAX_CHARS)}\n</compact-scratchpad>`;
}

/** The message with note appended: a text part on a user message, after a tool message's last text result; else null. */
export function appendedNote(message, note) {
  if (message?.role === "user") {
    const content = typeof message.content === "string" ? [{ type: "text", text: message.content }] : parts(message);
    return { ...message, content: [...content, { type: "text", text: wrapNote(note) }] };
  }
  if (message?.role !== "tool") return null;
  const content = parts(message);
  const at = content.findLastIndex((p) => p?.type === "tool-result" && p.result?.type === "text" && typeof p.result.value === "string");
  if (at < 0) return null;
  const next = [...content];
  next[at] = { ...content[at], result: { ...content[at].result, value: `${content[at].result.value}\n\n${wrapNote(note)}` } };
  return { ...message, content: next };
}

/** items with the nudge appended to item `at`; items itself when there is none to show. */
function nudged(items, at, text) {
  if (at === null || !text) return items;
  const item = appendedNote(items[at], text);
  return item === null ? items : [...items.slice(0, at), item, ...items.slice(at + 1)];
}

// CONTEXT_PROGRESS_NOTE: the note's mechanical progress section, built from the trimmed tool calls (proxy core.py).
const PROGRESS_NOTE_MAX_CHARS = 2000;
const PROGRESS_MAX_FILES = 20;
const PROGRESS_MAX_COMMANDS = 5;
const PROGRESS_COMMAND_MAX_CHARS = 200;
/**
 * Lowercased tool name -> input keys naming the file it writes. Claude Code: Write/Edit/MultiEdit (file_path),
 * NotebookEdit (notebook_path). OpenCode 2: write/edit (path; sessions migrated from v1 keep filePath). Pi: write/edit (path).
 */
const EDIT_TOOL_PATHS = new Map([
  ["write", ["file_path", "filePath", "path"]], ["edit", ["file_path", "filePath", "path"]],
  ["multiedit", ["file_path"]], ["notebookedit", ["notebook_path", "file_path"]],
]);
/** Lowercased shell tool names: Claude Code Bash, Pi bash, OpenCode 2 shell (v1 bash); each takes `command`. */
const SHELL_TOOLS = new Set(["bash", "shell"]);
// A shell command that builds or tests: a word of it starts one of these. Same as _BUILD_RE in proxy core.py.
const BUILD_RE = new RegExp("(?:^|[\\s;&|(/])(?:py\\.?test|tox|nox|make|cmake|ctest|ninja|cargo (?:build|test|check)"
  + "|go (?:build|test|vet)|(?:npm|yarn|pnpm)(?: run)? (?:test|build)|node --test|npx (?:jest|vitest|tsc)|jest|vitest|tsc"
  + "|mvn|gradlew?|javac|gcc|g\\+\\+|clang|rustc|dotnet (?:build|test)|rspec|rake"
  + "|python[0-9.]* -m (?:pytest|unittest|build))(?=\\s|$|;)");
const EXIT_RE = /\bexit(?:ed with)?(?: code| status)?:? (-?\d+)/i;

/** What a trim does to tool pairs in the prose region: "mask", true (strip) or false; masking wins. Proxy region_mode. */
function regionMode(settings) {
  return settings.maskObservations ? "mask" : Boolean(settings.stripProse);
}

/**
 * Indices prose stripping (or masking) may touch: kept, unpinned, before bound, not the cut, and not the last such
 * one (the message right before the latest run keeps its results). Proxy _prose_region.
 */
function proseRegion(items, keep, bound, cut, mode) {
  const kept = [...keep].sort((a, b) => a - b);
  // A mid-conversation system message must follow a user message, so stripping skips the message before one.
  const beforeSystem = new Set();
  if (mode !== "mask") {
    for (let i = 0; i + 1 < kept.length; i++) if (items[kept[i + 1]]?.role === "system") beforeSystem.add(kept[i]);
  }
  return kept.filter((j) => j < bound && j !== cut && !beforeSystem.has(j) && !isPinned(items[j])).slice(0, -1);
}

/** Tool call ids whose tool-call and tool-result both sit in the prose region. Proxy prose_ids. */
function proseIds(items, keep, bound, cut, mode) {
  const region = proseRegion(items, keep, bound, cut, mode);
  const calls = new Set(region.flatMap((j) => [...callIds(items[j])]));
  return new Set(region.flatMap((j) => [...resultIds(items[j])]).filter((id) => calls.has(id)));
}

/** Shrink sizes (requestChars per message; sizeChars with `old`) to what prose stripping (or masking) leaves before stripTop. */
function proseSizes(items, cut, sizes, mode, inputs = 0, old = false, exempt = null) {
  const keep = new Set(items.keys());
  const bound = stripTop(items, old);
  const ids = without(proseIds(items, keep, bound, cut, mode), exempt);
  if (!ids.size) return;
  for (const j of proseRegion(items, keep, bound, cut, mode)) {
    const x = mode === "mask" ? maskInputs(maskResults(items[j], ids), ids, inputs) : withoutToolCalls(items[j], ids);
    sizes[j] = x === null ? 0 : sizeChars(old, x);
  }
}

/**
 * requestChars per message after prose stripping or masking (mode) and clearing before `clear`, inputs over `inputs`
 * chars included; with `old` sizes carry signed payloads (sizeChars) less the reasoning stripped before stripTop. Proxy _trim_sizes.
 */
function trimSizes(items, cut, mode, clear, first, inputs = 0, old = false, exempt = null) {
  const sizes = items.map((x) => sizeChars(old, x));
  if (mode) proseSizes(items, cut, sizes, mode, inputs, old, exempt);
  const calls = inputs > 0 ? clearedCalls(items, clear, first) : null;
  for (let j = 0; j < (clear ?? 0); j++) {
    const x = cleared(items[j], j, clear, first, inputs, calls, exempt);
    if (x !== items[j]) sizes[j] = Math.min(sizes[j], sizeChars(old, x));
  }
  if (old) {
    const top = stripTop(items, old);
    for (let j = 0; j < top; j++) {
      if (j === cut || items[j]?.role !== "assistant") continue;
      const bare = bareOf(items[j], old);
      sizes[j] = Math.max(0, sizes[j] - sizeChars(old, items[j]) + (bare === null ? 0 : sizeChars(old, bare)));
    }
  }
  return sizes;
}

/** Whether newStrip takes reasoning off a kept assistant message the current view (cut, strip) still sends; proxy _strips_more. */
function stripsMore(items, keep, cut, strip, newStrip) {
  const lo = cut !== null && strip !== null ? strip : 0;
  return [...keep].some((j) => j >= lo && j < newStrip && j !== cut && items[j]?.role === "assistant"
    && bareOf(items[j], true) !== items[j]);
}

/** The note of a trim that only stripped earlier reasoning: no message is gone; proxy reasoning_text. */
export function reasoningText(sid) {
  return `[smarter-compact: session ${sid}] Your thinking from earlier steps was moved out to save space; every `
    + "message, tool call and tool result is still here. Continue the task from where you were.]";
}

/** "exit N" from the result text, else failed/ok by its error type; "no result" when none was sent. */
function commandStatus(result) {
  if (!result) return "no result";
  const found = EXIT_RE.exec(result.text || "");
  if (found) return `exit ${found[1]}`;
  return result.isError ? "failed" : "ok";
}

/**
 * Files written or edited and the last build/test commands (with pass/fail when known) among the tool calls in
 * items[indices]; "" when there are none. Tool names match case-insensitively. Proxy progress_text.
 */
function progressText(items, indices) {
  const results = new Map();
  for (const x of items) {
    for (const p of parts(x)) {
      if (p?.type === "tool-result") results.set(p.id, { isError: p.result?.type === "error", text: resultText(p.result) });
    }
  }
  const files = new Map();
  const commands = [];
  for (const j of indices) {
    for (const p of parts(items[j])) {
      if (p?.type !== "tool-call") continue;
      const name = typeof p.name === "string" ? p.name.toLowerCase() : "";
      const args = isObject(p.input) ? p.input : {};
      for (const key of EDIT_TOOL_PATHS.get(name) || []) {
        if (typeof args[key] === "string" && args[key]) {
          files.delete(args[key]);
          files.set(args[key], true);
          break;
        }
      }
      const command = SHELL_TOOLS.has(name) ? args.command : null;
      if (typeof command === "string" && BUILD_RE.test(command)) {
        const words = command.split(/\s+/).filter(Boolean).join(" ");
        commands.push([cpSlice(words, 0, PROGRESS_COMMAND_MAX_CHARS), results.get(p.id)]);
      }
    }
  }
  if (!files.size && !commands.length) return "";
  const lines = ["Progress before this trim (from the removed tool calls):"];
  if (files.size) lines.push(`Files written or edited (latest last): ${[...files.keys()].slice(-PROGRESS_MAX_FILES).join(", ")}`);
  if (commands.length) {
    lines.push("Last build/test commands (latest last):");
    for (const [c, r] of commands.slice(-PROGRESS_MAX_COMMANDS)) lines.push(`- \`${c}\` -> ${commandStatus(r)}`);
  }
  return cpSlice(lines.join("\n"), 0, PROGRESS_NOTE_MAX_CHARS);
}

/** note plus the progress section for the messages this trim drops, prose-strips or masks; note alone when empty. */
function withProgress(items, cut, gap, first, settings, note) {
  const keep = keptIndices(items, cut, gap, first);
  const sources = new Set([...items.keys()].filter((j) => !keep.has(j)));
  const mode = regionMode(settings);
  if (mode) for (const j of proseRegion(items, keep, latestRunStart(items), cut, mode)) sources.add(j);
  const text = progressText(items, [...sources].sort((a, b) => a - b));
  return text ? `${note}\n${text}` : note;
}

const utcNow = (now = new Date()) => now.toISOString().replace(/\.\d{3}Z$/, "Z");

/** json.dumps with Python's default separators (", ", ": "). */
function pyDumps(value, sortKeys = false) {
  if (value === null || typeof value !== "object") return JSON.stringify(value ?? null);
  if (Array.isArray(value)) return `[${value.map((v) => pyDumps(v, sortKeys)).join(", ")}]`;
  const keys = Object.keys(value).filter((k) => value[k] !== undefined);
  if (sortKeys) keys.sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}: ${pyDumps(value[k], sortKeys)}`).join(", ")}}`;
}

/** Context store files (docs/context-store.md). Directories 700, files 600. */
export class Store {
  constructor(root) {
    this.root = root;
    this.sessions = path.join(root, "sessions");
    this.blobs = path.join(root, "blobs");
  }

  file(sid, suffix) { return path.join(this.sessions, `${sid}${suffix}`); }

  ensure(dir) {
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
    try { fs.chmodSync(dir, 0o700); } catch { /* not ours */ }
  }

  writeAtomic(file, data) {
    const temporary = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.tmp`);
    try {
      fs.writeFileSync(temporary, data, { mode: 0o600 });
      fs.renameSync(temporary, file);
    } catch (error) {
      try { fs.unlinkSync(temporary); } catch { /* already gone */ }
      throw error;
    }
  }

  registration(sid) {
    try {
      const value = JSON.parse(fs.readFileSync(this.file(sid, ".json"), "utf8"));
      return value && typeof value === "object" && !Array.isArray(value) ? value : null;
    } catch { return null; }
  }

  forceRequested(sid) { return fs.existsSync(this.file(sid, ".force")); }

  clearForce(sid) {
    try { fs.unlinkSync(this.file(sid, ".force")); } catch (error) { if (error?.code !== "ENOENT") throw error; }
  }

  /** Trim state with threads as an insertion-ordered Map. */
  loadTrim(sid) {
    let value = null;
    try { value = JSON.parse(fs.readFileSync(this.file(sid, ".trim.json"), "utf8")); } catch { /* fresh */ }
    if (!value || typeof value !== "object" || value.version !== TRIM_STATE_VERSION) return { version: TRIM_STATE_VERSION, threads: new Map() };
    const threads = value.threads && typeof value.threads === "object" && !Array.isArray(value.threads) ? value.threads : {};
    return { ...value, threads: new Map(Object.entries(threads)) };
  }

  saveTrim(sid, state) {
    this.ensure(this.sessions);
    const plain = { ...state, threads: Object.fromEntries(state.threads) };
    this.writeAtomic(this.file(sid, ".trim.json"), pyDumps(plain, true));
  }

  /** Session database (session-db.mjs); creates the sessions directory. */
  dbPath(sid) {
    this.ensure(this.sessions);
    return this.file(sid, ".db");
  }

  /** The pad's text, kept in place and copied to <sid>.<n>.md (n the next free integer); "" when missing or blank. */
  readScratchpad(sid) {
    const dir = path.join(this.root, "scratchpad");
    let text;
    try { text = fs.readFileSync(path.join(dir, `${sid}.md`), "utf8"); } catch { return ""; }
    if (!text.trim()) return "";
    let n = 1;
    while (fs.existsSync(path.join(dir, `${sid}.${n}.md`))) n += 1;
    try { fs.writeFileSync(path.join(dir, `${sid}.${n}.md`), text); } catch { /* injected anyway */ }
    return text;
  }

  writeBlob(id, text) {
    const file = path.join(this.blobs, `${id}.txt`);
    if (fs.existsSync(file)) return;
    this.ensure(this.blobs);
    this.writeAtomic(file, text);
  }
}

/** Serialize work per session; the plugin process may run hooks for one session concurrently. */
export async function withSessionLock(sid, fn) {
  const previous = locks.get(sid) || Promise.resolve();
  let release;
  const current = new Promise((resolve) => { release = resolve; });
  const chained = previous.then(() => current);
  locks.set(sid, chained);
  await previous;
  try { return await fn(); } finally {
    release();
    if (locks.get(sid) === chained) locks.delete(sid);
  }
}

function whereOf(error) {
  const frame = String(error?.stack || "").split("\n").find((line) => /^\s+at /.test(line)) || "";
  return /([^/\\(\s]+:\d+)(?::\d+)?\)?\s*$/.exec(frame)?.[1] || "?";
}

/** One stderr line: exception type and location only, never message content. */
export function logFailOpen(error) {
  process.stderr.write(`smarter-compact: fail-open ${error?.name || typeof error} at ${whereOf(error)}\n`);
}

/** The events table's version column for rows this plugin writes: "opencode:<package version>". */
export const VERSION = (() => {
  try {
    const manifest = new URL("../../package.json", import.meta.url);
    return `opencode:${JSON.parse(fs.readFileSync(manifest, "utf8")).version || "unknown"}`;
  } catch { return "opencode:unknown"; }
})();

/** Append an events row to sessions/<sid>.db (core._record_event); never throws or logs. */
function recordEvent(store, sid, kind, fields, ts) {
  let file = null;
  try {
    file = store.dbPath(sid);
    openSession(file).addEvent(kind, ts, VERSION, fields);
  } catch {
    if (file) dropSession(file);
  }
}


function compressAll(messages, settings, store, skip = recallCallIds(messages)) {
  if (settings.compressOverTokens <= 0) return messages;
  return messages.map((m) => compressMessage(m, settings.compressOverTokens, (id, text) => store.writeBlob(id, text), skip));
}

const validMessages = (messages) => Array.isArray(messages) && messages.length > 0
  && messages.every((m) => m && typeof m === "object" && !Array.isArray(m));

/**
 * [seconds since the thread's previous request or null, idle gaps over 5 minutes so far, this one included]; records
 * now as the thread's last request in state (core._tick).
 */
function tick(state, root, now) {
  const raw = state[LAST_REQUEST_KEY];
  const seen = new Map(isObject(raw) ? Object.entries(raw) : []);
  const prev = isObject(seen.get(root)) ? seen.get(root) : {};
  seen.delete(root);
  const at = Number.isInteger(prev.at) ? prev.at : null;
  let gaps = Number.isInteger(prev.long) ? prev.long : 0;
  const gap = at !== null ? now - at : null;
  if (gap !== null && gap > SHORT_TTL_SECONDS) gaps += 1;
  seen.set(root, { at: now, long: gaps });
  while (seen.size > MAX_THREADS) seen.delete(seen.keys().next().value);
  state[LAST_REQUEST_KEY] = Object.fromEntries(seen);
  return [gap, gaps];
}

/** Contract session id, or null when the id is empty after trimming. */
function sessionIdOf(request) {
  try { return sanitizeSessionId(request.sessionID); } catch { return null; }
}

/**
 * Context-hook rewrite: compression, sticky trim, pointer note. Returns the messages to send
 * (the input array itself when nothing changed). `deps.decorate(message)` adds render-only parts
 * (see render).
 * @param {{ sessionID: string, system?: unknown, tools?: unknown, messages: object[] }} request
 */
export async function trimContext(request, settings, deps = {}) {
  const original = request.messages;
  if (!settings.enabled || !validMessages(original)) return original;
  const sid = sessionIdOf(request);
  if (!sid) return original;
  const store = new Store(settings.contextDir);
  const items = compressAll(original, settings, store);
  const roots = items.filter((m) => !isPinned(m));
  if (!roots.length) return original;
  const root = fingerprint(roots[0]);
  const first = firstUserIndex(items);
  const decorate = deps.decorate || ((m) => m);
  try {
    return await trimSession(request, settings, deps, { original, sid, store, items, root, first, decorate });
  } catch (error) {
    // The caller logs and sends the request untrimmed; the session keeps the record.
    recordEvent(store, sid, "fail_open", { location: whereOf(error), error: error?.name || typeof error }, utcNow(deps.now?.()));
    throw error;
  }
}

async function trimSession(request, settings, deps, { original, sid, store, items, root, first, decorate }) {
  let trim = null;
  let trimmed = TRIM_NONE;
  const view = await withSessionLock(sid, async () => {
    const state = store.loadTrim(sid);
    const { threads } = state;
    const thread = threads.get(root);
    let gap0 = null;
    if (settings.coldTrim || (settings.cacheTtlSplit ?? 0) > 0) {
      [gap0] = tick(state, root, Math.floor((deps.now ? deps.now() : new Date()).getTime() / 1000));
      store.saveTrim(sid, state);
    }
    // CONTEXT_COLD_TRIM: the plugin sees no cache_control, so the lifetime is the configured one.
    const coldTtl = settings.coldTrim && gap0 !== null ? settings.coldTrimSeconds ?? DEFAULT_COLD_TRIM_SECONDS : null;
    let logFresh = [];
    if (settings.logReduceOverChars > 0) {
      // CONTEXT_LOG_REDUCE_OVER_CHARS: decided on a result's first request, recorded, rendered on every later one.
      const logs = Array.isArray(state.logs) ? state.logs : [];
      ({ items, fresh: logFresh } = logReduced(original, items, logs, settings, store, recallCallIds(original)));
      if (logFresh.length) {
        state.logs = [...logs, ...logFresh].slice(-LOG_MAX);
        store.saveTrim(sid, state);
      }
    }
    // CONTEXT_DEDUP_RESULTS: recorded pointers render on every request; fingerprints stay on the original items.
    // CONTEXT_LEDGER reuse pointers share the list ("kind": "reuse").
    const raw = (settings.dedupResults !== false || settings.ledger) && Array.isArray(state.dedup) ? state.dedup : [];
    const dedup = raw.filter((d) => isObject(d) && (d.kind === "reuse" ? Boolean(settings.ledger) : settings.dedupResults !== false));
    let found = dedup.length ? dedupFound(items, dedup) : new Map();
    const ledgers = settings.ledger && Array.isArray(state.ledger) ? state.ledger : [];
    let lfound = ledgers.length ? dedupFound(items, ledgers) : new Map();
    const every = settings.stripReasoningEvery ?? 0;
    let rbound = every > 0 && thread?.reason_fp ? locate(items, thread.reason, thread.reason_fp) : null;
    const reasonLocated = rbound !== null || !thread?.reason_fp;
    const makeBase = (decided) => reasonStripped(deduped(items, decided), rbound);
    let base = makeBase(found);
    let cut = null;
    let gap = null;
    let strip = null;
    let note = null;
    let clear = null;
    let exempt = null;
    let llo = null;
    const nudgeAt = thread?.nudge_fp ? locate(items, thread.nudge_at, thread.nudge_fp) : null;
    // A thread holding only a scratchpad nudge or batched reasoning state has no cut to locate.
    if (thread && (thread.cut_fp || !SIDE_KEYS.some((k) => k in thread))) {
      cut = locate(items, thread.cut, thread.cut_fp);
      if (cut === null) {
        threads.delete(root);
        const carried = pickKeys(thread, REASON_KEYS);
        if (Object.keys(carried).length) {
          threads.set(root, { format: FORMAT, updated: utcNow(deps.now?.()), ...carried });
          store.saveTrim(sid, state);
        }
      } else {
        note = thread.note;
        if (thread.gap_fp) gap = locate(items, thread.gap, thread.gap_fp, cut + 2);
        // From 0: a trim on the last user turn records strip as the reply before the cut.
        if (thread.strip_fp) strip = locate(items, thread.strip, thread.strip_fp);
        if (thread.clear_fp) clear = locate(items, thread.clear, thread.clear_fp);
        // CONTEXT_KEEP_ERRORS: the error results the last trim exempted stay whole until the next trim.
        if (settings.keepErrors > 0 && Array.isArray(thread.keep_errors) && thread.keep_errors.length) exempt = new Set(thread.keep_errors);
        if (settings.ledger && thread.ledger_from_fp) llo = locate(items, thread.ledger_from, thread.ledger_from_fp);
      }
    }
    let shown = new Map();
    const mode = regionMode(settings);
    // The items as sent: the recorded nudge stays on its item, byte-identical, until the next trim, and so do the
    // recorded ledger blocks from llo on (CONTEXT_LEDGER).
    let nudgePos = nudgeAt;
    let nudgeText = threads.has(root) ? threads.get(root).nudge : null;
    const dress = (b) => ledgered(nudged(b, nudgePos, nudgeText), lfound, llo);
    let sent = dress(base);
    const inputs = settings.clearInputsOverChars ?? 0;
    const old = Boolean(settings.stripOldReasoning);
    let rendered = render(sent, cut, note, first, gap, decorate, strip, clear, mode, shown, inputs, old, exempt);
    const force = store.forceRequested(sid);
    const ratio = settings.charsPerToken > 0 ? settings.charsPerToken : DEFAULT_CHARS_PER_TOKEN;
    const toolNames = recallToolNames(request.tools);
    const baseChars = requestChars(request.system, request.tools);
    let tokens = estimatePromptTokens(baseChars + sizeChars(old, rendered), state.usage, root, ratio);
    let reasonEvent = null;
    if (every > 0) {
      // CONTEXT_STRIP_REASONING_EVERY: at each new batch of `every` assistant steps the reasoning bound may move to the
      // latest assistant message, when that strips at least stripReasoningMinTokens.
      const batch = Math.floor(items.filter((x) => x?.role === "assistant").length / every);
      const lastBatch = reasonLocated ? thread?.reason_batch : null;
      if (batch > 0 && (!Number.isInteger(lastBatch) || batch > lastBatch)) {
        const entry = threads.get(root) ?? { format: FORMAT, updated: utcNow(deps.now?.()) };
        if (!reasonLocated) for (const k of REASON_KEYS) delete entry[k];
        entry.reason_batch = batch;
        threads.set(root, entry);
        const top = latestMessageStart(items);
        if (top < items.length && (rbound === null || top > rbound)) {
          const movedBase = reasonStripped(base, top);
          const movedShown = new Map();
          const rsent = ledgered(nudged(movedBase, nudgePos, nudgeText), lfound, llo);
          const rview = render(rsent, cut, note, first, gap, decorate, strip, clear, mode, movedShown, inputs, old, exempt);
          const freed = estimatePromptTokens(baseChars + sizeChars(true, rendered), state.usage, root, ratio)
            - estimatePromptTokens(baseChars + sizeChars(true, rview), state.usage, root, ratio);
          if (movedBase !== base && freed >= (settings.stripReasoningMinTokens ?? DEFAULT_STRIP_REASONING_MIN_TOKENS)) {
            Object.assign(entry, { reason: top, reason_fp: fingerprint(items[top]) });
            [rbound, base, sent, rendered, shown] = [top, movedBase, rsent, rview, movedShown];
            const after = estimatePromptTokens(baseChars + sizeChars(old, rendered), state.usage, root, ratio);
            reasonEvent = { tokens_before: tokens, tokens_after: after, dropped: 0 };
            tokens = after;
          }
        }
        while (threads.size > MAX_THREADS) threads.delete(threads.keys().next().value);
        store.saveTrim(sid, state);
      }
    }
    const trimAt = trimAtFor(settings, request.model);
    // CONTEXT_COLD_TRIM: the prompt cache expired since the thread's last request, so a trim to the target costs no
    // extra cache write now, below the threshold too.
    const cold = coldTtl !== null && !force && gap0 > coldTtl
      && Math.min(trimToFor(settings, request.model), trimAt) < tokens && tokens < trimAt;
    if (force || tokens >= trimAt || cold) {
      const nextClearAt = nextClear(items, settings.clearKeepResults, clear);
      // CONTEXT_KEEP_ERRORS: the trim exempts the newest error results the view still shows whole.
      const nextExempt = settings.keepErrors > 0 ? errorIds(items, settings.keepErrors, shown) : [];
      const tExempt = nextExempt.length ? new Set(nextExempt) : null;
      // CONTEXT_LEDGER: a trim keeps only the newest recorded ledger block.
      const nextLlo = lfound.size ? Math.max(...lfound.keys()) : llo;
      const tbase = ledgered(base, lfound, nextLlo);
      // Reasoning the batched strip already took off counts as gone (CONTEXT_STRIP_REASONING_EVERY).
      const ritems = reasonStripped(items, rbound);
      // A target at or above the threshold could not land a trim under it, so the threshold caps it.
      const target = Math.min(trimToFor(settings, request.model), trimAt);
      const under = (sizes, c, g) => {
        let chars = baseChars;
        for (const j of keptIndices(items, c, g, first)) chars += sizes[j];
        return estimatePromptTokens(chars, state.usage, root, ratio) < target;
      };
      let next = null;
      let nextGap = null;
      const [anchor, anchorGap] = cut !== null ? [cut, gap] : [first, null];
      // With `old`, stripping earlier reasoning settles a trim like clearing; a forced trim still drops turns.
      const strips = old && !force && first !== null
        && stripsMore(ritems, keptIndices(items, anchor, anchorGap, first), cut, strip, stripTop(items, old));
      if ((nextClearAt !== null || strips) && first !== null) {
        // Clearing (and stripping) first: the current cut, or the first user turn (dropping nothing).
        if (under(trimSizes(base, anchor, mode, nextClearAt, first, inputs, old, tExempt), anchor, anchorGap)) [next, nextGap] = [anchor, anchorGap];
      }
      if (next === null) {
        next = chooseCut(items, cut === null ? -1 : cut, first);
        if (next === null) next = cut !== null ? cut : lastTurnIndex(items);
        if (next !== null) {
          const sizes = trimSizes(base, next, mode, nextClearAt, first, inputs, old, tExempt);
          const after = next === cut && gap !== null ? gap : next + 1;
          nextGap = chooseGap(items, next, after, first, (g) => under(sizes, next, g));
          if (nextGap === null && next === cut) nextGap = gap;
        }
      }
      let dropped = next !== null ? droppable(items, next, nextGap, first).length : 0;
      let moved = dropped > 0 && (next !== cut || nextGap !== gap);
      const nextStrip = stripTop(items, old);
      if (next !== null && !moved) {
        // A newer clear bound alone moves the trim when it masks a result the current bound left whole.
        const kept = keptIndices(items, next, nextGap, first);
        moved = clearsMore(base, kept, clear, nextClearAt, first, inputs, tExempt);
        // So does a strip bound that takes reasoning off a message the current view still sends.
        if (!moved && old) moved = stripsMore(ritems, kept, cut, strip, nextStrip);
      }
      const nextShown = new Map();
      if (moved) {
        const bare = render(tbase, next, "", first, nextGap, decorate, nextStrip, nextClearAt, mode, nextShown, inputs, old, tExempt);
        const gain = cold ? Math.max(settings.trimMinGainTokens ?? 0, settings.coldTrimMinGainTokens ?? DEFAULT_COLD_TRIM_MIN_GAIN_TOKENS)
          : settings.trimMinGainTokens;
        if (!force && gain > 0) {
          // A cut freeing fewer estimated tokens (pointer note not counted) waits for a bigger one.
          moved = tokens - estimatePromptTokens(baseChars + sizeChars(old, bare), state.usage, root, ratio) >= gain;
        }
      }
      if (moved) {
        const ts = utcNow(deps.now?.());
        // The pointer counts every message absent from the view: dropped, or emptied by the strip.
        dropped = items.length - nextShown.size;
        const masked = (mode === "mask" || nextClearAt !== null) && [...nextShown].some(([j, x]) => x !== tbase[j]
          && [MASK_PREFIX, INPUT_MASK_PREFIX].some((prefix) => JSON.stringify(x.content ?? null).includes(prefix)));
        if (old && !dropped && !masked) {
          // Stripping earlier reasoning alone drops and masks nothing.
          note = reasoningText(sid);
        } else if (dropped || !masked) {
          note = pointerText(sid, dropped, toolNames);
          if (settings.progressNote) note = withProgress(items, next, nextGap, first, settings, note);
          if (masked) note = `${note}\n${maskText(sid, toolNames)}`;
        } else {
          // Clearing alone drops no message: the note only says how to fetch a cleared result back.
          note = `[smarter-compact: session ${sid}] ${CLEARED_LEAD} ${maskText(sid, toolNames)}`;
        }
        const pad = settings.scratchpad ? store.readScratchpad(sid) : "";
        if (pad) note = `${scratchpadBlock(pad)}\n${note}`; // first, ahead of the recall instructions
        const carried = pickKeys(threads.get(root), REASON_KEYS);
        threads.delete(root);
        const entry = { cut: next, cut_fp: fingerprint(items[next]), note, format: FORMAT, updated: ts, ...carried };
        if (nextExempt.length) entry.keep_errors = nextExempt;
        if (nextLlo !== null && settings.ledger) Object.assign(entry, { ledger_from: nextLlo, ledger_from_fp: fingerprint(items[nextLlo]) });
        if (nextGap !== null) Object.assign(entry, { gap: nextGap, gap_fp: fingerprint(items[nextGap]) });
        strip = nextStrip;
        if (strip < items.length) Object.assign(entry, { strip, strip_fp: fingerprint(items[strip]) });
        if (nextClearAt !== null) Object.assign(entry, { clear: nextClearAt, clear_fp: fingerprint(items[nextClearAt]) });
        threads.set(root, entry);
        while (threads.size > MAX_THREADS) threads.delete(threads.keys().next().value);
        store.saveTrim(sid, state);
        if (force) store.clearForce(sid);
        shown = new Map();
        rendered = render(tbase, next, note, first, nextGap, decorate, strip, nextClearAt, mode, shown, inputs, old, tExempt);
        cut = next;
        gap = nextGap;
        clear = nextClearAt;
        const after = estimatePromptTokens(baseChars + sizeChars(old, rendered), state.usage, root, ratio);
        trim = { tokens_before: tokens, tokens_after: after, dropped };
        if (cold) trim.location = "cold";
        if (settings.scratchpad) trim.scratchpad_chars = Math.min(pad.length, SCRATCHPAD_MAX_CHARS);
        [sent, exempt, nudgePos, llo] = [tbase, tExempt, null, nextLlo];
      } else if (thread && cut === null && !threads.has(root)) store.saveTrim(sid, state);
    } else if (thread && cut === null && !threads.has(root)) store.saveTrim(sid, state);
    let nudge = null;
    if (settings.scratchpad && !trim && !threads.get(root)?.nudge_fp && tokens >= trimAt * settings.scratchpadNudgeAt
        && scratchpadAvailable(toolNames, original)) {
      const text = scratchpadNudge(sid, toolNames);
      const last = items.length - 1;
      if (nudged(base, last, text) !== base) {
        // Recorded on the newest item, so every later request re-renders it unchanged.
        const entry = threads.get(root) ?? { format: FORMAT, updated: utcNow(deps.now?.()) };
        Object.assign(entry, { nudge: text, nudge_at: last, nudge_fp: fingerprint(items[last]) });
        threads.set(root, entry);
        while (threads.size > MAX_THREADS) threads.delete(threads.keys().next().value);
        store.saveTrim(sid, state);
        [nudgePos, nudgeText] = [last, text];
        sent = dress(base);
        shown = new Map();
        rendered = render(sent, cut, note, first, gap, decorate, strip, clear, mode, shown, inputs, old, exempt);
        nudge = { tokens_before: tokens };
      }
    }
    const ts = utcNow(deps.now?.());
    const recall = recallCallIds(original);
    const fresh = settings.dedupResults !== false ? dedupNew(items, shown, found, recall) : [];
    const { state: lstate, reuse, nudges: lnudges } = settings.ledger ? ledgerNew(items, shown, found, fresh, recall)
      : { state: null, reuse: [], nudges: [] };
    if (fresh.length || reuse.length) {
      // Recorded before the request leaves, so every later request renders the same pointer.
      for (const [d, text] of [...fresh, ...reuse]) if (!compressedBlobId(text)) store.writeBlob(d.bid, text);
      state.dedup = [...raw, ...[...fresh, ...reuse].map(([d]) => d)].slice(-DEDUP_MAX);
      store.saveTrim(sid, state);
      if (settings.ledger) {
        // Rebuilt in the order every later request renders: pointers, then the nudge and ledger blocks.
        found = dedupFound(items, [...dedup, ...[...fresh, ...reuse].map(([d]) => d)]);
        base = makeBase(found);
        sent = dress(base);
      } else sent = deduped(sent, dedupFound(items, fresh.map(([d]) => d)));
      shown = new Map();
      rendered = render(sent, cut, note, first, gap, decorate, strip, clear, mode, shown, inputs, old, exempt);
    }
    const block = settings.ledger ? newLedgerBlock(items, sent, lstate, lnudges, lfound) : null;
    if (block) {
      // Recorded on the newest message, so every later request re-renders the block unchanged.
      state.ledger = [...ledgers, block].slice(-LEDGER_MAX);
      store.saveTrim(sid, state);
      lfound = new Map([...lfound, [block.at, [block]]]);
      sent = dress(base);
      shown = new Map();
      rendered = render(sent, cut, note, first, gap, decorate, strip, clear, mode, shown, inputs, old, exempt);
    }
    writeMaskedBlobs(store, sent, shown);
    storeMessages(store, sid, settings, original, sent, cut, shown, ts, deps);
    if (trim) recordEvent(store, sid, "trim", trim, ts);
    if (nudge) recordEvent(store, sid, "nudge", nudge, ts);
    if (fresh.length) recordEvent(store, sid, "dedup", { dropped: fresh.length }, ts);
    if (reuse.length) recordEvent(store, sid, "reuse", { dropped: reuse.length }, ts);
    if (reasonEvent) recordEvent(store, sid, "strip_batch", reasonEvent, ts);
    if (logFresh.length) recordEvent(store, sid, "log_reduce", { dropped: logFresh.length }, ts);
    trimmed = trim ? TRIM_NEW : cut !== null ? TRIM_KEPT : TRIM_NONE;
    return rendered;
  });
  // Measured like the estimate, so a later recordPromptUsage compares like with like.
  deps.measured?.({ root, chars: requestChars(request.system, request.tools) + sizeChars(Boolean(settings.stripOldReasoning), view), model: modelName(request.model), trimmed });
  return view.length === original.length && view.every((m, i) => m === original[i]) ? original : view;
}

/**
 * Calibrate a session from the provider's reported prompt tokens for a request trimContext measured
 * (deps.measured's { root, chars }). Returns whether a sample was stored.
 */
export async function recordPromptUsage(settings, sessionID, measured, tokens) {
  if (!settings.enabled || !measured) return false;
  let sid;
  try { sid = sanitizeSessionId(sessionID); } catch { return false; }
  const store = new Store(settings.contextDir);
  return withSessionLock(sid, async () => {
    const state = store.loadTrim(sid);
    if (!recordUsage(state, measured.root, measured.chars, tokens)) return false;
    store.saveTrim(sid, state);
    return true;
  });
}

/**
 * Append the usage event of a step trimContext measured (core.process_usage): model, thread root, trim state and
 * the step's token counts. OpenCode adds cache_control in its provider layer, so breakpoints stay null.
 */
export function recordStepUsage(settings, sessionID, measured, tokens, now) {
  if (!settings.enabled || !measured) return false;
  let sid;
  try { sid = sanitizeSessionId(sessionID); } catch { return false; }
  recordEvent(new Store(settings.contextDir), sid, "usage", { model: measured.model ?? null, thread_root: measured.root,
    trimmed: measured.trimmed ?? null, ...usageCountsFromOpenCode(tokens) }, utcNow(now));
  return true;
}

function modelName(model) {
  if (typeof model === "string") return model;
  if (!isObject(model)) return null;
  const id = typeof model.id === "string" ? model.id : typeof model.modelID === "string" ? model.modelID : null;
  return id && typeof model.providerID === "string" ? `${model.providerID}/${id}` : id;
}

/**
 * Compaction-hook result: store every message as out of context, then return the pointer summary
 * that replaces OpenCode's model-written summary. Null when there is nothing to compact.
 */
export async function compactionSummary(request, settings, deps = {}) {
  const compactionToolNames = recallToolNames(request.tools);
  const original = request.messages;
  if (!validMessages(original)) return null;
  const sid = sessionIdOf(request);
  if (!sid) return null;
  const store = new Store(settings.contextDir);
  const items = compressAll(original, settings, store);
  const roots = items.filter((m) => !isPinned(m));
  if (!roots.length) return null;
  const root = fingerprint(roots[0]);
  const first = firstUserIndex(items);
  return withSessionLock(sid, async () => {
    const state = store.loadTrim(sid);
    const thread = state.threads.get(root);
    // Compaction drops every message, the pinned first user turn included; no cut turn, so trimmed_at_seq is NULL.
    storeMessages(store, sid, settings, original, items, null, new Map(), utcNow(deps.now?.()), deps);
    const note = pointerText(sid, roots.length, compactionToolNames);
    if (thread) {
      state.threads.delete(root);
      store.saveTrim(sid, state);
    }
    return note;
  });
}
