#!/usr/bin/env node
/**
 * Agent hook. Reads one JSON object on stdin and writes one JSON object on stdout.
 * A failure prints nothing and exits 0: the session continues untrimmed.
 */
import { ensureProductEnv, PRODUCT, PRODUCT_NAME } from "../lib/brand.mjs";
import { isSmartCompactPrompt, registerSession, writeForceFlag } from "../lib/context-store.mjs";
import { hookHost, isMcpOnlyHost, trimsOnWire } from "../lib/hook-host.mjs";
import { ensureContextProxyRunning } from "../lib/proxy-daemon.mjs";

ensureProductEnv();

const NOTE_TRIM = `${PRODUCT_NAME} keeps every turn of this session in a local store and drops older turns from the model request once the prompt crosses its token threshold. Nothing is summarized. Call smart_compact_search, smart_compact_sql, or smart_compact_get; the pointer names the session id. /${PRODUCT}:compact trims on the next request.`;

const NOTE_MCP_ONLY = `${PRODUCT_NAME} is connected here for MCP search and session registration only — this agent does not rewrite model traffic, so context is not trimmed automatically. Rows appear in the store only after requests go through the Smarter Compact proxy (Claude Code, Codex, or OpenCode with smarter-compact enable). Use smart_compact_* to search stored turns from those sessions, or run one of those agents for full compaction. /${PRODUCT}:compact has no effect in this CLI until the vendor supports a custom API base URL or an in-process context hook (OpenCode 2 uses the bundled context plugin).`;

function readStdin() {
  return new Promise((resolve) => {
    const chunks = [];
    process.stdin.on("data", (chunk) => chunks.push(chunk));
    process.stdin.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    process.stdin.on("error", () => resolve(""));
  });
}

function sessionId(msg) {
  return String(msg.session_id || msg.sessionId || msg.sessionID || msg.conversation_id || "");
}

function eventName(msg) {
  return String(msg.hook_event_name || msg.hookEventName || msg.event_name || "").toLowerCase();
}

function promptText(msg) {
  if (typeof msg.prompt === "string") return msg.prompt;
  if (typeof msg.prompt?.text === "string") return msg.prompt.text;
  if (typeof msg.text === "string") return msg.text;
  return "";
}

function emit(value) {
  process.stdout.write(`${JSON.stringify(value)}\n`);
}

const raw = await readStdin();
let msg = {};
try {
  msg = raw.trim() ? JSON.parse(raw) : {};
} catch {
  process.exit(0);
}

try {
  const home = process.env.HOME;
  const sid = sessionId(msg);
  const name = eventName(msg);
  const host = hookHost(process.argv, msg);
  const wireTrim = trimsOnWire(host);
  const cwd = typeof msg.cwd === "string" ? msg.cwd : process.cwd();
  let proxy = null;
  if (wireTrim && (name.includes("sessionstart") || name === "startup")) {
    proxy = await ensureContextProxyRunning({ home, env: process.env, importMetaUrl: import.meta.url, waitMs: 8000 });
  }
  if (sid && (name.includes("sessionstart") || name === "startup" || name === "")) {
    try { registerSession({ home, env: process.env, sessionId: sid, agent: host, cwd }); } catch { /* best effort */ }
  }
  if (wireTrim && sid && isSmartCompactPrompt(promptText(msg))) {
    try { writeForceFlag({ home, env: process.env, sessionId: sid }); } catch { /* best effort */ }
  }
  if (name.includes("sessionstart") || name === "startup") {
    const hookEventName = msg.hook_event_name || "SessionStart";
    const note = isMcpOnlyHost(host) ? NOTE_MCP_ONLY : NOTE_TRIM;
    // systemMessage reaches the user, not the model: a stale or just-restarted proxy.
    const shown = proxy?.warning || (proxy?.status === "restarted" ? `proxy restarted, ${proxy.from} -> ${proxy.to}` : null);
    emit({
      hookSpecificOutput: { hookEventName, additionalContext: note },
      additional_context: note,
      ...(shown ? { systemMessage: `Smarter Compact: ${shown}` } : {}),
    });
  } else {
    emit({});
  }
} catch {
  try { emit({}); } catch { /* stdout closed */ }
}
