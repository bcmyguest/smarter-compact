/**
 * OpenCode 1 `server`: session id on outbound chat (for the proxy), `/compact` force flag, registration.
 * Trimming on OpenCode 1 runs in the LiteLLM proxy (`smarter-compact enable opencode` + `smarter-compact proxy`), not here.
 */
import fs from "node:fs";
import os from "node:os";
import { ensureProductEnv } from "../plugin/lib/brand.mjs";
import { isSmartCompactPrompt, registerSession, writeForceFlag } from "../plugin/lib/context-store.mjs";
import { parseJsonc, opencodeConfigPath } from "../plugin/lib/opencode-config.mjs";
import { envFromPluginOptions } from "./options.mjs";

function messageText(item) {
  return (item.parts || []).filter((part) => part.type === "text" && part.text).map((part) => part.text).join("\n");
}

function proxiedFromConfig(home) {
  try {
    const file = opencodeConfigPath(home);
    if (!fs.existsSync(file)) return false;
    const base = parseJsonc(fs.readFileSync(file, "utf8"))?.provider?.anthropic?.options?.baseURL || "";
    return /^http:\/\/127\.0\.0\.1:\d+(?:\/v1)?$/.test(String(base));
  } catch {
    return false;
  }
}

/**
 * @param {{ directory: string }} input
 * @param {Record<string, unknown>} [options]
 */
export async function server(input, options = {}) {
  ensureProductEnv();
  envFromPluginOptions(options);
  const home = process.env.HOME || os.homedir();
  const projectDir = input.directory;
  const proxied = proxiedFromConfig(home);

  return {
    "chat.headers": async (hookInput, output) => {
      if (proxied && hookInput.sessionID) output.headers["x-smarter-compact-session-id"] = hookInput.sessionID;
    },
    "chat.message": async (hookInput, output) => {
      if (isSmartCompactPrompt(messageText(output))) {
        try { writeForceFlag({ home, env: process.env, sessionId: hookInput.sessionID }); } catch { /* best effort */ }
      }
    },
    event: async ({ event }) => {
      if (event.type !== "session.created") return;
      const id = event.properties?.info?.id;
      if (!id) return;
      try {
        registerSession({ home, env: process.env, sessionId: id, agent: "opencode", cwd: projectDir });
      } catch { /* best effort */ }
    },
  };
}
