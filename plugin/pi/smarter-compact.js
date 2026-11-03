/**
 * Smarter Compact for Pi: native trim via Pi's `context` hook (same trim core as OpenCode 2).
 * @see https://pi.dev/docs/latest/extensions#context
 */
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { ensureProductEnv, PRODUCT_NAME } from "../lib/brand.mjs";
import { isSmartCompactPrompt, registerSession, writeForceFlag } from "../lib/context-store.mjs";
import { logFailOpen, trimContext, trimSettings } from "../lib/context-trim.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const CLI = join(HERE, "..", "scripts", "smarter-compact.mjs");

function runCli(args) {
  return new Promise((resolve) => {
    let out = "";
    let child;
    try {
      child = spawn(process.execPath, [CLI, ...args], { stdio: ["ignore", "pipe", "ignore"] });
    } catch {
      resolve("");
      return;
    }
    const timer = setTimeout(() => { child.kill("SIGKILL"); resolve(""); }, 15000);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (d) => { out += d; });
    child.on("error", () => { clearTimeout(timer); resolve(""); });
    child.on("close", () => { clearTimeout(timer); resolve(out); });
  });
}

export default function smartCompact(pi) {
  ensureProductEnv();
  const home = process.env.HOME;
  const settings = trimSettings(process.env, home);

  const sid = (ctx) => ctx.sessionManager.getSessionId();

  pi.on("session_start", async (_event, ctx) => {
    try {
      registerSession({ home, env: process.env, sessionId: sid(ctx), agent: "pi", cwd: ctx.cwd });
    } catch { /* best effort */ }
    if (ctx.hasUI) {
      ctx.ui.notify(
        `${PRODUCT_NAME}: trims on each LLM call via the context hook; use smart_compact_search when you need an earlier turn.`,
        "info",
      );
    }
  });

  pi.on("before_agent_start", async (event, ctx) => {
    const sessionID = sid(ctx);
    if (sessionID && isSmartCompactPrompt(String(event.prompt ?? ""))) {
      try { writeForceFlag({ home, env: process.env, sessionId: sessionID }); } catch { /* best effort */ }
    }
  });

  pi.on("context", async (event, ctx) => {
    const sessionID = sid(ctx);
    if (!sessionID) return;
    try {
      registerSession({ home, env: process.env, sessionId: sessionID, agent: "pi", cwd: ctx.cwd });
    } catch { /* best effort */ }
    const original = event.messages;
    let messages = original;
    try {
      messages = await trimContext(
        { sessionID, model: ctx.model, messages: original },
        settings,
      );
    } catch (error) {
      logFailOpen(error);
    }
    if (messages !== original) return { messages };
  });

  const cliHandler = async (args, ctx) => {
    const argv = (args || "status").trim().split(/\s+/).filter(Boolean);
    if (argv[0] === "compact") argv.push("--session", sid(ctx));
    const out = await runCli(argv);
    const text = out.trim() || "smarter-compact: no output";
    if (ctx.hasUI) ctx.ui.notify(text, "info");
    else console.error(text);
  };

  pi.registerCommand("smarter-compact", {
    description: "Smarter Compact: status, or compact to trim this session on the next request",
    handler: cliHandler,
  });
}
