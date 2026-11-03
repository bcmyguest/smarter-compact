/**
 * OpenCode 2 plugin. Trims in the context hook. Memory recall and retention stay in OpenMem.
 *
 *   mkdir -p ~/.config/opencode/plugins
 *   ln -s "$PWD/plugin/opencode/shelf.js" ~/.config/opencode/plugins/
 */
import os from "node:os";
import path from "node:path";
import { ensureProductEnv, PRODUCT, PRODUCT_NAME } from "../lib/brand.mjs";
import { SMART_COMPACT_BODY, isSmartCompactPrompt, registerSession, writeForceFlag } from "../lib/context-store.mjs";
import {
  addStandingInstruction, compactionSummary, logFailOpen, promptTokensFromOpenCode, recordPromptUsage,
  recordStepUsage, trimContext, trimSettings,
} from "../lib/context-trim.mjs";

ensureProductEnv();

const MAX_SESSIONS = 128;
const MAX_MEASURED_PER_SESSION = 8;
const EVENT_TYPES = ["session.created", "session.step.ended", "session.step.failed", "session.execution.interrupted", "session.execution.failed"];

function remember(map, key, value) {
  map.delete(key);
  map.set(key, value);
  while (map.size > MAX_SESSIONS) map.delete(map.keys().next().value);
}

async function setup(ctx) {
  const home = process.env.HOME || os.homedir();
  const settings = trimSettings(process.env, home);
  const projectDir = ctx.location?.directory;
  const measured = new Map();
  const registered = new Set();

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
    try { addStandingInstruction(event.system, settings); } catch (error) { logFailOpen(error); }
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
        else if (event.type === "session.execution.interrupted" || event.type === "session.execution.failed") measured.delete(data.sessionID);
      }
    } catch (error) {
      if (!controller.signal.aborted) logFailOpen(error);
    }
  })();
  return () => controller.abort();
}

export default { id: PRODUCT, setup };
