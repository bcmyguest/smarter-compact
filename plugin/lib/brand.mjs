/**
 * Product identity for the extracted trimmer. The session-store wire format
 * (`[openmem: …]`, `smart_compact_*`, `OPENMEM_STATE_DIR`) stays the OpenMem contract.
 */
import os from "node:os";
import path from "node:path";

export const PRODUCT = "shelf";
export const PRODUCT_NAME = "Shelf";
export const DEFAULT_PORT = 4101;
export const STATE_ENV = "OPENMEM_STATE_DIR";
export const ROOT_ENV = "SHELF_ROOT";

/** Parent of `context/`. The core appends `context` itself. */
export function stateParent(home = os.homedir(), env = process.env) {
  const set = String(env[STATE_ENV] || "").replace(/^[ \t\n\v\f\r]+|[ \t\n\v\f\r]+$/g, "");
  return set || path.join(home, ".local", "share", PRODUCT);
}

/** Fill process env the core already reads. Existing values win. */
export function ensureProductEnv(env = process.env, home = os.homedir()) {
  if (!String(env[STATE_ENV] || "").trim()) env[STATE_ENV] = stateParent(home, env);
  if (env.CONTEXT_COMPACTION_RECALL === undefined) env.CONTEXT_COMPACTION_RECALL = "0";
  if (env.CONTEXT_PROXY_PORT === undefined) env.CONTEXT_PROXY_PORT = String(DEFAULT_PORT);
  return env;
}

export function configDir(home) {
  return path.join(home, ".config", PRODUCT);
}
