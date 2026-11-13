import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { embeddingsMessage, ensureEmbeddings } from "../plugin/lib/embeddings.mjs";

function home() {
  const h = fs.mkdtempSync(path.join(os.tmpdir(), "sc-embed-"));
  fs.mkdirSync(path.join(h, ".config", "smarter-compact"), { recursive: true });
  return h;
}
const envFile = (h) => path.join(h, ".config", "smarter-compact", "proxy.env");
const json = (body, ok = true) => ({ ok, json: async () => body });

/** An Ollama stand-in: tags lists `models`; pull adds the model; embeddings answer when the model is present. */
function fakeOllama({ up = true, models = [], pullOk = true, embedOk = true } = {}) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    const route = new URL(url).pathname;
    calls.push(route);
    if (!up) throw new Error("ECONNREFUSED");
    if (route === "/api/tags") return json({ models: models.map((name) => ({ name, model: name })) });
    if (route === "/api/pull") {
      if (!pullOk) return json({ error: "nope" }, false);
      models.push(`${JSON.parse(init.body).model}:latest`);
      return json({ status: "success" });
    }
    if (route === "/v1/embeddings") return json(embedOk ? { data: [{ embedding: [0.1, 0.2] }] } : { data: [] });
    return json({}, false);
  };
  return { fetch, calls };
}

test("an installed model is turned on without a pull", async () => {
  const h = home();
  const o = fakeOllama({ models: ["nomic-embed-text:latest"] });
  const r = await ensureEmbeddings({ home: h, env: {}, fetch: o.fetch });
  assert.deepEqual(r, { status: "enabled", model: "nomic-embed-text" });
  assert.deepEqual(o.calls, ["/api/tags", "/v1/embeddings"]);
  assert.match(fs.readFileSync(envFile(h), "utf8"), /^CONTEXT_EMBEDDING_MODEL=nomic-embed-text$/m);
});

test("a missing model is pulled first", async () => {
  const h = home();
  const o = fakeOllama();
  const r = await ensureEmbeddings({ home: h, env: {}, fetch: o.fetch });
  assert.equal(r.status, "enabled");
  assert.deepEqual(o.calls, ["/api/tags", "/api/pull", "/v1/embeddings"]);
});

test("without Ollama search stays lexical and nothing is written", async () => {
  const h = home();
  const r = await ensureEmbeddings({ home: h, env: {}, fetch: fakeOllama({ up: false }).fetch });
  assert.equal(r.status, "no-ollama");
  assert.equal(fs.existsSync(envFile(h)), false);
  const msg = embeddingsMessage(r, "smarter-compact");
  assert.equal(msg.level, "warn");
  assert.match(msg.text, /keyword-only/);
});

test("pull and embedding failures leave it off", async () => {
  assert.equal((await ensureEmbeddings({ home: home(), env: {}, fetch: fakeOllama({ pullOk: false }).fetch })).status, "pull-failed");
  assert.equal((await ensureEmbeddings({ home: home(), env: {}, fetch: fakeOllama({ embedOk: false }).fetch })).status, "embed-failed");
});

test("an explicit model or URL, empty included, is left alone", async () => {
  const o = fakeOllama();
  assert.equal((await ensureEmbeddings({ home: home(), env: { CONTEXT_EMBEDDING_MODEL: "" }, fetch: o.fetch })).status, "configured");
  assert.equal((await ensureEmbeddings({ home: home(), env: { CONTEXT_EMBEDDING_URL: "http://gpu:8080/v1" }, fetch: o.fetch })).status, "configured");
  const h = home();
  fs.writeFileSync(envFile(h), "CONTEXT_EMBEDDING_MODEL=\n");
  assert.equal((await ensureEmbeddings({ home: h, env: {}, fetch: o.fetch })).status, "configured");
  assert.deepEqual(o.calls, []);
  assert.equal(embeddingsMessage({ status: "configured" }, "x"), null);
});
