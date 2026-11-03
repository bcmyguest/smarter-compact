/**
 * OpenCode 2 `setup`: native trim in session.hook("context"), same rules as the LiteLLM proxy.
 */
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { ensureProductEnv, PRODUCT, PRODUCT_NAME } from "../plugin/lib/brand.mjs";
import { SMART_COMPACT_BODY, isSmartCompactPrompt, registerSession, writeForceFlag } from "../plugin/lib/context-store.mjs";
import {
  addStandingInstruction, compactionSummary, logFailOpen, promptTokensFromOpenCode, recallToolNames, recordPromptUsage,
  recordStepUsage, trimContext, trimSettings,
} from "../plugin/lib/context-trim.mjs";
import { ensureContextProxyRunning } from "../plugin/lib/proxy-daemon.mjs";
import { envFromPluginOptions } from "./options.mjs";

const MAX_SESSIONS = 128;
const MAX_MEASURED_PER_SESSION = 8;
const EVENT_TYPES = [
  "session.created", "session.step.ended", "session.step.failed",
  "session.execution.interrupted", "session.execution.failed",
];

/**
 * One stderr line per plugin-level fact a bench run must be able to assert on.
 * Registration and instruction-injection used to fail by silent `return`, which made
 * a whole sweep indistinguishable from "model ignored the tool". stderr is multiplexed
 * into the captured agent stream by the runners, so nothing extra has to be collected.
 */
function diag(message) {
  try { process.stderr.write(`smarter-compact: ${message}\n`); } catch { /* never fail the session */ }
}

function remember(map, key, value) {
  map.delete(key);
  map.set(key, value);
  while (map.size > MAX_SESSIONS) map.delete(map.keys().next().value);
}

/**
 * Best-effort self-registration of the search MCP server (OpenCode >= 2.2 `ctx.mcp.add`), so a
 * trimmed turn can be fetched back with no user config. A rejected/absent `ctx.mcp.add` is
 * invisible otherwise, so the reason is logged; the `mcp.servers` config entry (docs/opencode.md)
 * is the path that must be relied on, and the bench writes it for every smart trial.
 */
async function registerSearchTools(ctx, { home, enabled, directory }) {
  if (!enabled) return;
  if (!ctx.mcp?.add || !directory) {
    diag(`recall MCP NOT registered reason=${ctx.mcp?.add ? "no-location-directory" : "no-ctx.mcp.add"} ctx=[${Object.keys(ctx).join(",")}] mcp=[${Object.keys(ctx.mcp ?? {}).join(",")}]`);
    return;
  }
  try {
    const script = path.join(
      path.dirname(fileURLToPath(import.meta.url)), "..", "plugin", "lib", "mcp", "server.mjs",
    );
    await ctx.mcp.add({
      server: PRODUCT,
      location: { directory },
      config: {
        type: "local",
        command: ["node", script],
        environment: {
          HOME: home,
          SMARTER_COMPACT_STATE_DIR:
            process.env.SMARTER_COMPACT_STATE_DIR || path.join(home, ".local", "share", PRODUCT),
          CONTEXT_COMPACTION_RECALL: process.env.CONTEXT_COMPACTION_RECALL || "0",
        },
        enabled: true,
      },
    });
    diag(`recall MCP registered via ctx.mcp.add dir=${directory}`);
  } catch (error) {
    diag(`recall MCP registration FAILED ${error?.name || typeof error}`);
    logFailOpen(error);
  }
}

/**
 * @param {import("@opencode-ai/plugin").PluginContext} ctx
 * @param {Record<string, unknown>} [options]
 */
export async function setup(ctx, options = {}) {
  ensureProductEnv();
  const home = process.env.HOME || os.homedir();
  await ensureContextProxyRunning({ home, env: process.env, importMetaUrl: import.meta.url, waitMs: 8000 });
  const settings = trimSettings(envFromPluginOptions(options), home);
  const projectDir = ctx.location?.directory;
  const measured = new Map();
  const registered = new Set();
  const reportedSystem = new Set();

  function register(sessionID) {
    if (!sessionID || registered.has(sessionID)) return;
    try {
      registerSession({ home, env: process.env, sessionId: sessionID, agent: "opencode", cwd: projectDir });
      registered.add(sessionID);
      while (registered.size > MAX_SESSIONS) registered.delete(registered.values().next().value);
    } catch { /* best effort */ }
  }

  function flagSmartCompact(sessionID, text) {
    if (!isSmartCompactPrompt(text)) return false;
    try { writeForceFlag({ home, env: process.env, sessionId: sessionID }); } catch { /* best effort */ }
    return true;
  }

  function onMeasured(sessionID, sample) {
    const queue = measured.get(sessionID) || [];
    queue.push(sample);
    while (queue.length > MAX_MEASURED_PER_SESSION) queue.shift();
    remember(measured, sessionID, queue);
  }

  async function onStepEnded(data) {
    const sample = measured.get(data.sessionID)?.shift();
    if (!sample) return;
    recordStepUsage(settings, data.sessionID, sample, data.tokens);
    const tokens = promptTokensFromOpenCode(data.tokens);
    if (tokens) await recordPromptUsage(settings, data.sessionID, sample, tokens);
  }

  async function onContext(event) {
    register(event.sessionID);
    const original = event.messages;
    let messages = original;
    try {
      messages = await trimContext(event, settings, { measured: (sample) => onMeasured(event.sessionID, sample) });
    } catch (error) { logFailOpen(error); }
    if (messages !== original) event.messages = messages;
    try {
      // Names come from this request: the model can only call what it was offered, and OpenCode
      // namespaces the recall tools as `<server>.<tool>`, i.e. `smarter-compact.smart_compact_search`.
      const pushed = addStandingInstruction(event.system, settings, recallToolNames(event.tools));
      if (!reportedSystem.has(event.sessionID)) {
        if (reportedSystem.size > MAX_SESSIONS) reportedSystem.clear();
        reportedSystem.add(event.sessionID);
        diag(`standing_instruction pushed=${pushed} system=${
          Array.isArray(event.system) ? `array(${event.system.length})` : typeof event.system}`);
      }
    } catch (error) { logFailOpen(error); }
  }

  async function onCompaction(event) {
    if (!settings.enabled) return;
    try {
      const summary = await compactionSummary(event, settings, {});
      if (summary) event.result = { summary, metadata: { [PRODUCT]: "native-trim" } };
    } catch (error) { logFailOpen(error); }
  }

  await ctx.session.hook("prompt", async (event) => {
    register(event.sessionID);
    flagSmartCompact(event.sessionID, String(event.prompt?.text || ""));
  });
  await ctx.session.hook("context", onContext);
  await ctx.session.hook("compaction", onCompaction);
  await registerSearchTools(ctx, { home, enabled: settings.enabled, directory: projectDir });
  if (ctx.command?.transform) {
    await ctx.command.transform((editor) => {
      editor.add({
        name: "compact",
        description: `Trim this session on the next request (${PRODUCT_NAME})`,
        execute: async ({ sessionID, prompt, delivery }) => {
          flagSmartCompact(sessionID, SMART_COMPACT_BODY);
          await ctx.session.prompt({ ...prompt, sessionID, text: SMART_COMPACT_BODY, delivery });
        },
      });
    });
  }
  const controller = new AbortController();
  void (async () => {
    try {
      for await (const event of ctx.event.subscribe({ types: EVENT_TYPES, signal: controller.signal })) {
        const data = event?.data || event?.properties || {};
        const where = data.location?.directory;
        if (where && projectDir && path.resolve(where) !== path.resolve(projectDir)) continue;
        if (event.type === "session.created") register(data.sessionID);
        else if (event.type === "session.step.ended") await onStepEnded(data);
        else if (event.type === "session.step.failed") measured.get(data.sessionID)?.shift();
        else if (event.type === "session.execution.interrupted" || event.type === "session.execution.failed") {
          measured.delete(data.sessionID);
        }
      }
    } catch (error) {
      if (!controller.signal.aborted) logFailOpen(error);
    }
  })();
  return () => controller.abort();
}
