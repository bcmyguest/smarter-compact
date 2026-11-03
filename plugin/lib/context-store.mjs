/** Hook side of the context store contract (docs/context-store.md). */
import fs from "node:fs";
import path from "node:path";

/** Marker in the installed smart-compact command body; survives command expansion. */
export const SMART_COMPACT_MARKER = "openmem:smart-compact";
/** Prompt that `/smart-compact` sends in every agent (Claude Code command file, OpenCode 2 command). */
export const SMART_COMPACT_BODY = `<!-- ${SMART_COMPACT_MARKER} -->
OpenMem will trim this session's earlier context on the next request. Reply with one short line acknowledging this; do not summarize the conversation. After the trim, use \`smart_compact_search\` with this session's id to look up anything earlier.`;
/** Claude Code command file (`~/.claude/commands/smart-compact.md`): frontmatter plus SMART_COMPACT_BODY. */
export const SMART_COMPACT_COMMAND = `---
description: Trim this session's context on the next request (OpenMem)
---
${SMART_COMPACT_BODY}
`;
const SMART_COMPACT_PREFIX = /^\s*\/smart-compact(?:\s|$)/;
const SMART_COMPACT_TAG = "<command-name>/smart-compact</command-name>";
/** ASCII whitespace only, so the hooks and the proxy trim the same characters (session ids, OPENMEM_STATE_DIR). */
const ASCII_TRIM_RE = /^[ \t\n\v\f\r]+|[ \t\n\v\f\r]+$/g;
const SID_UNSAFE_RE = /[^A-Za-z0-9._-]/gu;

/** `$OPENMEM_STATE_DIR/context` (value trimmed), default `~/.local/share/openmem/context`. */
export function contextRoot(home, env = process.env) {
  const state = String(env.OPENMEM_STATE_DIR || "").replace(ASCII_TRIM_RE, "") || path.join(home, ".local", "share", "openmem");
  return path.join(state, "context");
}

/**
 * Contract sanitizer (docs/context-store.md): trim surrounding ASCII whitespace, replace every character
 * outside `[A-Za-z0-9._-]` with `_`, throw on an empty result.
 */
export function sanitizeSessionId(sessionId) {
  const sid = String(sessionId ?? "").replace(ASCII_TRIM_RE, "").replace(SID_UNSAFE_RE, "_");
  if (!sid) throw new Error("empty session id");
  return sid;
}

function sessionsDir(home, env) {
  const root = contextRoot(home, env);
  const dir = path.join(root, "sessions");
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  fs.chmodSync(root, 0o700);
  fs.chmodSync(dir, 0o700);
  return dir;
}

function writePrivate(file, text) {
  const temporary = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, text, { mode: 0o600 });
  fs.renameSync(temporary, file);
}

/** Write `sessions/<sid>.json`. Returns the path, or null without a session id. */
export function registerSession({ home, env, sessionId, agent, cwd, bank, now = new Date() }) {
  if (!sessionId) return null;
  const sid = sanitizeSessionId(sessionId);
  const file = path.join(sessionsDir(home, env), `${sid}.json`);
  const registration = {
    session_id: sid,
    agent,
    cwd,
    bank,
    registered_at: now.toISOString().replace(/\.\d{3}Z$/, "Z"),
  };
  writePrivate(file, `${JSON.stringify(registration)}\n`);
  return file;
}

/** Write the empty `sessions/<sid>.force` flag. Returns the path, or null without a session id. */
export function writeForceFlag({ home, env, sessionId }) {
  if (!sessionId) return null;
  const file = path.join(sessionsDir(home, env), `${sanitizeSessionId(sessionId)}.force`);
  writePrivate(file, "");
  return file;
}

/** True for `/smart-compact …`, Claude Code's command tag, or the expanded command body. */
export function isSmartCompactPrompt(prompt) {
  const text = String(prompt || "");
  return SMART_COMPACT_PREFIX.test(text) || text.includes(SMART_COMPACT_TAG) || text.includes(SMART_COMPACT_MARKER);
}
