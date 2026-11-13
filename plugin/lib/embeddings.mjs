/**
 * Semantic search by default: `enable` turns vectors on (CONTEXT_EMBEDDING_MODEL in proxy.env) when a local Ollama
 * answers, pulling the model if needed. Without Ollama, search stays lexical and `enable` says so. An explicit
 * CONTEXT_EMBEDDING_MODEL or CONTEXT_EMBEDDING_URL (env or proxy.env, empty included) is left alone, so
 * `CONTEXT_EMBEDDING_MODEL=` in proxy.env opts out.
 */
import { proxyEnvPath, readProxyEnv } from "./brand.mjs";
import { upsertEnvFile } from "./file-io.mjs";

export const DEFAULT_EMBEDDING_MODEL = "nomic-embed-text";
export const OLLAMA_URL = "http://127.0.0.1:11434";
const PROBE_TIMEOUT_MS = 2_000;
const PULL_TIMEOUT_MS = 15 * 60_000;
const EMBED_TIMEOUT_MS = 60_000;

async function request(fetchImpl, url, init, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetchImpl(url, { ...init, signal: ctrl.signal });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

const post = (fetchImpl, url, body, timeoutMs) =>
  request(fetchImpl, url, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) }, timeoutMs);

/**
 * Result status: "configured" (the user set the model or URL; untouched), "enabled" (written to proxy.env now),
 * "no-ollama", "pull-failed", "embed-failed".
 */
export async function ensureEmbeddings({ home, env = process.env, fetch: fetchImpl = globalThis.fetch, log = () => {} }) {
  const file = readProxyEnv(home);
  for (const key of ["CONTEXT_EMBEDDING_MODEL", "CONTEXT_EMBEDDING_URL"]) {
    if (env[key] !== undefined || Object.hasOwn(file, key)) return { status: "configured", model: env.CONTEXT_EMBEDDING_MODEL ?? file.CONTEXT_EMBEDDING_MODEL ?? "" };
  }
  const model = DEFAULT_EMBEDDING_MODEL;
  const tags = await request(fetchImpl, `${OLLAMA_URL}/api/tags`, {}, PROBE_TIMEOUT_MS);
  if (!tags) return { status: "no-ollama" };
  const have = (tags.models || []).some((m) => [m.name, m.model].some((n) => n === model || String(n || "").startsWith(`${model}:`)));
  if (!have) {
    log(`pulling ${model} into Ollama for semantic search`);
    const pulled = await post(fetchImpl, `${OLLAMA_URL}/api/pull`, { model, stream: false }, PULL_TIMEOUT_MS);
    if (!pulled || (pulled.status && pulled.status !== "success")) return { status: "pull-failed", model };
  }
  // The same OpenAI-compatible route the proxy embeds through (settings DEFAULT_EMBEDDING_URL).
  const probe = await post(fetchImpl, `${OLLAMA_URL}/v1/embeddings`, { model, input: ["smarter-compact"] }, EMBED_TIMEOUT_MS);
  if (!probe?.data?.[0]?.embedding?.length) return { status: "embed-failed", model };
  upsertEnvFile(proxyEnvPath(home), { CONTEXT_EMBEDDING_MODEL: model }, { mode: 0o600 });
  return { status: "enabled", model };
}

/** One line for `enable` to print, or null when there is nothing to say. */
export function embeddingsMessage(r, product) {
  const otherServer = "or set CONTEXT_EMBEDDING_URL and CONTEXT_EMBEDDING_MODEL in proxy.env for another OpenAI-compatible embeddings server";
  switch (r.status) {
    case "enabled": return { level: "info", text: `semantic search on (${r.model} via Ollama)` };
    case "no-ollama": return { level: "warn", text: `semantic search off: no Ollama at ${OLLAMA_URL}, so search is keyword-only. Install Ollama and run ${product} enable again, ${otherServer}.` };
    case "pull-failed": return { level: "warn", text: `semantic search off: ollama pull ${r.model} failed. Run it yourself and ${product} enable again, ${otherServer}.` };
    case "embed-failed": return { level: "warn", text: `semantic search off: Ollama did not return an embedding from ${r.model}. Check ollama run ${r.model}, ${otherServer}.` };
    default: return null;
  }
}
