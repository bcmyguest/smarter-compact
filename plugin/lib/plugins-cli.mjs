/** Register the bundled plugin marketplace with agent CLIs (Claude, Codex, Copilot). */
import { spawnSync } from "node:child_process";
import { PRODUCT } from "./brand.mjs";

export const PLUGIN_AGENTS = ["claude", "codex", "copilot"];

/** @param {string} root package directory (contains plugin/) */
export function pluginMarketplaceSteps(agent, root) {
  switch (agent) {
    case "claude":
      return [
        ["claude", "plugin", "marketplace", "add", root],
        ["claude", "plugin", "install", "smarter-compact@smarter-compact"],
      ];
    case "codex":
      return [
        ["codex", "plugin", "marketplace", "add", root],
        ["codex", "plugin", "add", "smarter-compact@smarter-compact"],
      ];
    case "copilot":
      return [
        ["copilot", "plugin", "marketplace", "add", root],
        ["copilot", "plugin", "install", "smarter-compact@smarter-compact"],
      ];
    default:
      return null;
  }
}

/**
 * @param {string} agent
 * @param {string} root
 * @param {{ spawn?: typeof spawnSync }} deps
 */
export function addPluginMarketplace(agent, root, { spawn = spawnSync } = {}) {
  const steps = pluginMarketplaceSteps(agent, root);
  if (!steps) return { ok: false, message: `unknown plugin agent ${agent}` };
  for (const [bin, ...args] of steps) {
    const r = spawn(bin, args, { env: process.env, stdio: "inherit", windowsHide: true });
    if (r.error) {
      if (r.error.code === "ENOENT") return { ok: false, message: `${bin} not on PATH` };
      return { ok: false, message: r.error.message };
    }
    if (r.status !== 0) return { ok: false, message: `${bin} ${args.join(" ")} exited ${r.status}` };
  }
  return { ok: true, message: `${agent}: plugin marketplace registered` };
}
