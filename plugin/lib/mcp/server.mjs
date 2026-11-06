#!/usr/bin/env node
/**
 * MCP server for the session store: smart_compact_search, smart_compact_sql,
 * smart_compact_get, context_status, and smart_compact_scratchpad with CONTEXT_SCRATCHPAD on. Stdio, NDJSON or Content-Length.
 */
import os from "node:os";
import { ensureProductEnv, PRODUCT_NAME } from "../brand.mjs";
import { callContextStoreTool, contextStoreTools, startContextStore } from "./context-store.mjs";
import { serveStdio } from "./stdio.mjs";

ensureProductEnv();

const home = process.env.HOME || os.homedir();

function ok(id, result) {
  return { jsonrpc: "2.0", id, result };
}

async function handle(msg) {
  if (!msg || msg.jsonrpc !== "2.0" || msg.id === undefined) return null;
  const { id, method, params } = msg;
  try {
    if (method === "initialize") {
      return ok(id, {
        protocolVersion: params?.protocolVersion || "2024-11-05",
        capabilities: { tools: {} },
        serverInfo: { name: PRODUCT_NAME.toLowerCase(), version: "0.1.0" },
      });
    }
    if (method === "notifications/initialized" || method === "ping") return ok(id, {});
    if (method === "tools/list") return ok(id, { tools: contextStoreTools(process.env) });
    if (method === "tools/call") {
      const data = await callContextStoreTool(params?.name, params?.arguments || {}, { home });
      return ok(id, { content: [{ type: "text", text: JSON.stringify(data) }] });
    }
    return { jsonrpc: "2.0", id, error: { code: -32601, message: `method not found: ${method}` } };
  } catch (err) {
    return { jsonrpc: "2.0", id, error: { code: -32000, message: err instanceof Error ? err.message : String(err) } };
  }
}

await startContextStore(process.env, { home });
await serveStdio(process.stdin, handle);
