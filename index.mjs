/**
 * smarter-compact: smart compaction for OpenCode 1 and 2.
 *
 * OpenCode 2 reads `id` + `setup` and trims in the context hook.
 * OpenCode 1 calls `server` and trims through the LiteLLM proxy when `smarter-compact enable opencode` points at it.
 */
import { server } from "./src/server.mjs";
import { setup as setupV2 } from "./src/v2.mjs";

export const PLUGIN_ID = "smarter-compact";

/** OpenCode 2 entry; `ctx.options` map to CONTEXT_* (see src/options.mjs). */
export async function setup(ctx) {
  return setupV2(ctx, ctx?.options ?? {});
}

export { server };

export default { id: PLUGIN_ID, setup, server };
