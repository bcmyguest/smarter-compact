/**
 * Semantic search by default: `enable` sets CONTEXT_EMBEDDING_MODEL to the built-in model (local-embedding.mjs),
 * which the proxy runs itself, so no embedding server is needed. An explicit CONTEXT_EMBEDDING_MODEL or
 * CONTEXT_EMBEDDING_URL (env or proxy.env, empty included) is left alone, so `CONTEXT_EMBEDDING_MODEL=` in proxy.env
 * opts out. After the proxy is up, warmEmbeddings() embeds one string through it: the first call downloads the
 * weights (~135 MB) and proves the route works.
 */
import { proxyEnvPath, readProxyEnv } from "./brand.mjs";
import { upsertEnvFile } from "./file-io.mjs";
import { embeddingEndpoint, LOCAL_EMBEDDING_MODEL } from "./local-embedding.mjs";

const WARM_TIMEOUT_MS = 5 * 60_000;

/** "configured" (the user set the model or URL; untouched), "enabled" (written now) or "unchanged" (already on). */
export function ensureEmbeddings({ home, env = process.env }) {
  const file = readProxyEnv(home);
  if (env.CONTEXT_EMBEDDING_URL !== undefined || Object.hasOwn(file, "CONTEXT_EMBEDDING_URL")) {
    return { status: "configured", model: env.CONTEXT_EMBEDDING_MODEL ?? file.CONTEXT_EMBEDDING_MODEL ?? "" };
  }
  const model = env.CONTEXT_EMBEDDING_MODEL ?? file.CONTEXT_EMBEDDING_MODEL;
  if (model === LOCAL_EMBEDDING_MODEL) return { status: "unchanged", model };
  if (model !== undefined) return { status: "configured", model };
  upsertEnvFile(proxyEnvPath(home), { CONTEXT_EMBEDDING_MODEL: LOCAL_EMBEDDING_MODEL }, { mode: 0o600 });
  return { status: "enabled", model: LOCAL_EMBEDDING_MODEL };
}

/** Embed one string with the built-in model through the running proxy; {ok, seconds, dim} or {ok: false, reason}. */
export async function warmEmbeddings({ home, env = process.env, fetch: fetchImpl = globalThis.fetch }) {
  const { url, apiKey } = embeddingEndpoint({ embeddingApiKey: "" }, LOCAL_EMBEDDING_MODEL, { home, env });
  const started = Date.now();
  try {
    const res = await fetchImpl(`${url}/embeddings`, {
      method: "POST",
      headers: { "content-type": "application/json", ...(apiKey ? { authorization: `Bearer ${apiKey}` } : {}) },
      body: JSON.stringify({ model: LOCAL_EMBEDDING_MODEL, input: ["smarter-compact"] }),
      signal: AbortSignal.timeout(WARM_TIMEOUT_MS),
    });
    if (!res.ok) return { ok: false, reason: `HTTP ${res.status}` };
    const dim = (await res.json())?.data?.[0]?.embedding?.length || 0;
    return dim ? { ok: true, dim, seconds: (Date.now() - started) / 1000 } : { ok: false, reason: "no vector in the response" };
  } catch (err) {
    return { ok: false, reason: err instanceof Error ? err.message : String(err) };
  }
}
