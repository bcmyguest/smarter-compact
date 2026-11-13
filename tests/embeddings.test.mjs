import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { ensureEmbeddings, warmEmbeddings } from "../plugin/lib/embeddings.mjs";
import { embeddingEndpoint, LOCAL_EMBEDDING_MODEL, queryText } from "../plugin/lib/local-embedding.mjs";

function home(proxyEnv) {
  const h = fs.mkdtempSync(path.join(os.tmpdir(), "sc-embed-"));
  fs.mkdirSync(path.join(h, ".config", "smarter-compact"), { recursive: true });
  if (proxyEnv !== undefined) fs.writeFileSync(envFile(h), proxyEnv);
  return h;
}
const envFile = (h) => path.join(h, ".config", "smarter-compact", "proxy.env");

test("enable turns on the built-in model when nothing is configured", () => {
  const h = home();
  assert.deepEqual(ensureEmbeddings({ home: h, env: {} }), { status: "enabled", model: LOCAL_EMBEDDING_MODEL });
  assert.match(fs.readFileSync(envFile(h), "utf8"), /^CONTEXT_EMBEDDING_MODEL=smarter-compact-embed$/m);
  assert.equal(ensureEmbeddings({ home: h, env: {} }).status, "unchanged");
});

test("an explicit model or URL, empty included, is left alone", () => {
  assert.equal(ensureEmbeddings({ home: home(), env: { CONTEXT_EMBEDDING_MODEL: "" } }).status, "configured");
  assert.equal(ensureEmbeddings({ home: home(), env: { CONTEXT_EMBEDDING_URL: "http://gpu:8080/v1" } }).status, "configured");
  const h = home("CONTEXT_EMBEDDING_MODEL=nomic-embed-text\n");
  assert.equal(ensureEmbeddings({ home: h, env: {} }).status, "configured");
  assert.equal(fs.readFileSync(envFile(h), "utf8"), "CONTEXT_EMBEDDING_MODEL=nomic-embed-text\n");
  assert.equal(ensureEmbeddings({ home: home("CONTEXT_EMBEDDING_MODEL=\n"), env: {} }).status, "configured");
});

test("the built-in model is reached through the proxy with its master key", () => {
  const h = home("LITELLM_MASTER_KEY=sk-test\nCONTEXT_PROXY_PORT=4555\n");
  const settings = { embeddingUrl: "http://127.0.0.1:11434/v1", embeddingApiKey: "" };
  assert.deepEqual(embeddingEndpoint(settings, LOCAL_EMBEDDING_MODEL, { home: h, env: {} }), { url: "http://127.0.0.1:4555/v1", apiKey: "sk-test" });
  assert.deepEqual(embeddingEndpoint(settings, "nomic-embed-text", { home: h, env: {} }), { url: "http://127.0.0.1:11434/v1", apiKey: "" });
  assert.match(queryText(LOCAL_EMBEDDING_MODEL, "q"), /^Represent this sentence for searching relevant passages: q$/);
  assert.equal(queryText("nomic-embed-text", "q"), "q");
});

test("warmEmbeddings reports the vector size, or why it failed", async () => {
  const h = home("LITELLM_MASTER_KEY=sk-test\nCONTEXT_PROXY_PORT=4555\n");
  let seen;
  const ok = await warmEmbeddings({ home: h, env: {}, fetch: async (url, init) => {
    seen = { url, auth: init.headers.authorization, body: JSON.parse(init.body) };
    return { ok: true, json: async () => ({ data: [{ embedding: new Array(384).fill(0) }] }) };
  } });
  assert.equal(ok.ok, true);
  assert.equal(ok.dim, 384);
  assert.deepEqual([seen.url, seen.auth, seen.body.model], ["http://127.0.0.1:4555/v1/embeddings", "Bearer sk-test", LOCAL_EMBEDDING_MODEL]);
  assert.deepEqual(await warmEmbeddings({ home: h, env: {}, fetch: async () => ({ ok: false, status: 500 }) }), { ok: false, reason: "HTTP 500" });
  assert.equal((await warmEmbeddings({ home: h, env: {}, fetch: async () => { throw new Error("ECONNREFUSED"); } })).ok, false);
});
