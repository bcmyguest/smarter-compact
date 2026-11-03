#!/usr/bin/env node
/**
 * Agent hook. Reads one JSON object on stdin and writes one JSON object on stdout.
 * A failure prints nothing and exits 0: the session continues untrimmed.
 */
import { ensureProductEnv, PRODUCT, PRODUCT_NAME } from "../lib/brand.mjs";
import { isSmartCompactPrompt, registerSession, writeForceFlag } from "../lib/context-store.mjs";

ensureProductEnv();

const NOTE = `${PRODUCT_NAME} keeps every turn of this session in a local store and drops older turns from the model request once the prompt crosses its token threshold. Nothing is summarized. Call smart_compact_search, smart_compact_sql, or smart_compact_get; the pointer names the session id. /${PRODUCT}:compact trims on the next request.`;

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
  const cwd = typeof msg.cwd === "string" ? msg.cwd : process.cwd();
  if (sid && (name.includes("sessionstart") || name === "startup" || name === "")) {
    try { registerSession({ home, env: process.env, sessionId: sid, agent: String(msg.host || "agent"), cwd }); } catch { /* best effort */ }
  }
  if (sid && isSmartCompactPrompt(promptText(msg))) {
    try { writeForceFlag({ home, env: process.env, sessionId: sid }); } catch { /* best effort */ }
  }
  if (name.includes("sessionstart") || name === "startup") {
    const hookEventName = msg.hook_event_name || "SessionStart";
    emit({
      hookSpecificOutput: { hookEventName, additionalContext: NOTE },
      additional_context: NOTE,
    });
  } else {
    emit({});
  }
} catch {
  try { emit({}); } catch { /* stdout closed */ }
}
