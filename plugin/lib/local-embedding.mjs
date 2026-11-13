/**
 * The built-in embedding model (proxy/smarter_compact/local_embed.py) runs in the proxy and is served on its
 * /v1/embeddings as `smarter-compact-embed`. Node callers reach it there with the proxy's master key, whatever
 * CONTEXT_EMBEDDING_URL says (that URL is for a model of your own).
 */
import os from "node:os";
import { readProxyEnv, resolveProxyPort } from "./brand.mjs";

export const LOCAL_EMBEDDING_MODEL = "smarter-compact-embed";
// bge v1.5's instruction for short queries against passages (local_embed.QUERY_PREFIX); queries only, never rows.
export const LOCAL_QUERY_PREFIX = "Represent this sentence for searching relevant passages: ";

/** {url, apiKey} to embed with model: the proxy for the built-in model, else the configured endpoint. */
export function embeddingEndpoint(settings, model, { home = os.homedir(), env = process.env } = {}) {
  if (model !== LOCAL_EMBEDDING_MODEL) return { url: settings.embeddingUrl, apiKey: settings.embeddingApiKey };
  return {
    url: `http://127.0.0.1:${resolveProxyPort(home, env)}/v1`,
    apiKey: settings.embeddingApiKey || String(env.LITELLM_MASTER_KEY || readProxyEnv(home).LITELLM_MASTER_KEY || "").trim(),
  };
}

/** The text to embed for a search query. */
export function queryText(model, query) {
  return model === LOCAL_EMBEDDING_MODEL ? `${LOCAL_QUERY_PREFIX}${query}` : query;
}
