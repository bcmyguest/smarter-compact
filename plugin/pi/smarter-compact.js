/**
 * Smarter Compact for Pi. Pi has no pre-request rewrite, so this extension only forwards
 * Pi's events to the shared hook (session note, /compact force flag) and runs smarter-compact.
 */
import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const HOOK = join(HERE, "..", "scripts", "smarter-compact-hook.mjs");
const CLI = join(HERE, "..", "scripts", "smarter-compact.mjs");

function run(script, args, input) {
  return new Promise((resolve) => {
    let out = "";
    let child;
    try {
      child = spawn(process.execPath, [script, ...args], { stdio: ["pipe", "pipe", "ignore"] });
    } catch {
      resolve("");
      return;
    }
    const timer = setTimeout(() => { child.kill("SIGKILL"); resolve(""); }, 15000);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (d) => { out += d; });
    child.on("error", () => { clearTimeout(timer); resolve(""); });
    child.on("close", () => { clearTimeout(timer); resolve(out); });
    child.stdin.on("error", () => {});
    child.stdin.end(input);
  });
}

export default function smartCompact(pi) {
  const base = (ctx) => ({
    session_id: ctx.sessionManager.getSessionId(),
    cwd: ctx.cwd,
    host: "pi",
  });

  pi.on("session_start", async (_event, ctx) => {
    const out = await run(HOOK, [], JSON.stringify({ ...base(ctx), hook_event_name: "SessionStart" }));
    try {
      const parsed = JSON.parse(out.trim().split("\n").pop() || "{}");
      const text = parsed?.hookSpecificOutput?.additionalContext;
      if (text && ctx.hasUI) ctx.ui.notify(text, "info");
    } catch { /* fail open */ }
  });

  pi.on("before_agent_start", async (event, ctx) => {
    await run(HOOK, [], JSON.stringify({
      ...base(ctx),
      hook_event_name: "UserPromptSubmit",
      prompt: event.prompt,
    }));
  });

  const cliHandler = async (args, ctx) => {
    const argv = (args || "status").trim().split(/\s+/).filter(Boolean);
    if (argv[0] === "compact") argv.push("--session", ctx.sessionManager.getSessionId());
    const out = await run(CLI, argv, "");
    if (ctx.hasUI) ctx.ui.notify(out.trim() || "sc: no output", "info");
    else console.error(out.trim());
  };
  pi.registerCommand("sc", {
    description: "Smarter Compact: status, or compact to trim this session on the next request",
    handler: cliHandler,
  });
  pi.registerCommand("smarter-compact", {
    description: "Same as /sc",
    handler: cliHandler,
  });
}
