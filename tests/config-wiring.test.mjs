import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { ensureProductEnv } from "../plugin/lib/brand.mjs";
import { proxyEnvPath } from "../plugin/lib/enable.mjs";
import { upsertEnvFile } from "../plugin/lib/file-io.mjs";
import { contextStoreSettings } from "../plugin/lib/mcp/context-store.mjs";
import { trimSettings } from "../plugin/lib/context-trim.mjs";
import { configTomlPath } from "../plugin/lib/context-config.mjs";
import * as proxyDaemon from "../plugin/lib/proxy-daemon.mjs";
import { server } from "../src/server.mjs";
import { setup } from "../src/v2.mjs";

const SAVED_ENV = { ...process.env };

function tmp(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), prefix));
}

/** A fresh HOME whose process env points the store somewhere other than the plugin option. */
function isolate(extra = {}) {
  const home = tmp("sc-wiring-");
  for (const key of Object.keys(process.env)) {
    if (/^(CONTEXT_|SMARTER_COMPACT_|OPENMEM_)/.test(key)) delete process.env[key];
  }
  Object.assign(process.env, { HOME: home, SMARTER_COMPACT_STATE_DIR: path.join(home, "env-state"), ...extra });
  return home;
}

function restoreEnv() {
  for (const key of Object.keys(process.env)) if (!(key in SAVED_ENV)) delete process.env[key];
  Object.assign(process.env, SAVED_ENV);
}

function sessionFile(state, name) {
  return path.join(state, "context", "sessions", name);
}

/** Minimal OpenCode 2 plugin context that records hooks and the MCP registration. */
function fakeCtx(directory) {
  const hooks = {};
  const mcp = [];
  return {
    hooks,
    mcp,
    ctx: {
      location: { directory },
      session: { hook: async (name, fn) => { hooks[name] = fn; }, prompt: async () => {} },
      mcp: { add: async (entry) => { mcp.push(entry); } },
      event: { subscribe: async function* () {} },
    },
  };
}

test.afterEach(restoreEnv);

test("OpenCode 1: stateDir option places registration and /compact flag", async () => {
  const home = isolate({ SMARTER_COMPACT_AUTO_PROXY: "0" });
  const state = path.join(home, "option-state");
  const hooks = await server({ directory: home }, { stateDir: state });
  await hooks.event({ event: { type: "session.created", properties: { info: { id: "oc1" } } } });
  await hooks["chat.message"]({ sessionID: "oc1" }, { parts: [{ type: "text", text: "/smarter-compact" }] });
  assert.ok(fs.existsSync(sessionFile(state, "oc1.json")), "registration under the option's stateDir");
  assert.ok(fs.existsSync(sessionFile(state, "oc1.force")), "force flag under the option's stateDir");
  assert.ok(!fs.existsSync(sessionFile(path.join(home, "env-state"), "oc1.json")));
});

test("OpenCode 2: stateDir option reaches registration, /compact flag and the MCP server", async () => {
  const home = isolate({ SMARTER_COMPACT_AUTO_PROXY: "0" });
  const state = path.join(home, "option-state");
  const { ctx, hooks, mcp } = fakeCtx(home);
  const stop = await setup(ctx, { stateDir: state });
  await hooks.prompt({ sessionID: "oc2", prompt: { text: "/smarter-compact" } });
  stop();
  assert.ok(fs.existsSync(sessionFile(state, "oc2.json")), "registration under the option's stateDir");
  assert.ok(fs.existsSync(sessionFile(state, "oc2.force")), "force flag under the option's stateDir");
  assert.equal(mcp[0].config.environment.SMARTER_COMPACT_STATE_DIR, state);
});

test("OpenCode 2: proxyPort option is the port auto-start probes, over the environment", async () => {
  const probes = [];
  const probe = http.createServer((req, res) => { probes.push(req.url); res.end("ok"); });
  await new Promise((resolve) => { probe.listen(0, "127.0.0.1", resolve); });
  const { port } = probe.address();
  try {
    const home = isolate({ SMARTER_COMPACT_AUTO_PROXY: "1", CONTEXT_PROXY_PORT: "1" });
    const { ctx } = fakeCtx(home);
    const stop = await setup(ctx, { proxyPort: port });
    stop();
  } finally {
    probe.close();
  }
  assert.ok(probes.includes("/health/liveliness"), `auto-start probed the option's port ${port}`);
});

test("MCP store settings read [context_store] from config.toml; env wins", async () => {
  const home = tmp("sc-store-cfg-");
  fs.mkdirSync(path.join(home, ".config", "smarter-compact"), { recursive: true });
  fs.writeFileSync(path.join(home, ".config", "smarter-compact", "config.toml"), [
    "[context_store]",
    "ttl_days = 7",
    'embedding_model = "nomic"',
    'embedding_url = "http://127.0.0.1:9/v1"',
    "embedding_timeout_s = 12",
    "embedding_max_chars = 900",
    "",
  ].join("\n"));
  const fromFile = await contextStoreSettings({ HOME: home });
  assert.equal(fromFile.ttlDays, 7);
  assert.equal(fromFile.embeddingModel, "nomic");
  assert.equal(fromFile.embeddingUrl, "http://127.0.0.1:9/v1");
  assert.equal(fromFile.embeddingTimeoutS, 12);
  assert.equal(fromFile.embeddingMaxChars, 900);
  const envWins = await contextStoreSettings({ HOME: home, CONTEXT_STORE_TTL_DAYS: "3" });
  assert.equal(envWins.ttlDays, 3);
});

test("MCP store settings report an unreadable config.toml on stderr", async () => {
  const home = tmp("sc-store-bad-");
  fs.mkdirSync(path.join(home, ".config", "smarter-compact"), { recursive: true });
  fs.writeFileSync(path.join(home, ".config", "smarter-compact", "config.toml"), "[context_store\nttl_days = \n");
  const written = [];
  const write = process.stderr.write;
  process.stderr.write = (chunk) => { written.push(String(chunk)); return true; };
  let settings;
  try {
    settings = await contextStoreSettings({ HOME: home });
  } finally {
    process.stderr.write = write;
  }
  assert.equal(settings.ttlDays, 30);
  assert.match(written.join(""), /config\.toml/);
});

test("proxy.env supplies every proxy setting the environment leaves unset", () => {
  const home = tmp("sc-proxyenv-");
  upsertEnvFile(proxyEnvPath(home), {
    CONTEXT_MASK_OBSERVATIONS: "0",
    CONTEXT_SERVER_CLEAR: "1",
    CONTEXT_STORE_TTL_DAYS: "9",
    CONTEXT_EMBEDDING_API_KEY: "secret",
  }, { mode: 0o600 });
  const env = { HOME: home, CONTEXT_SERVER_CLEAR: "0" };
  ensureProductEnv(env, home);
  assert.equal(env.CONTEXT_MASK_OBSERVATIONS, "0");
  assert.equal(env.CONTEXT_STORE_TTL_DAYS, "9");
  assert.equal(env.CONTEXT_SERVER_CLEAR, "0", "the environment wins");
  assert.equal(env.CONTEXT_EMBEDDING_API_KEY, undefined, "a secret stays out of the agent's environment");
  assert.equal(typeof proxyDaemon.litellmEnv, "function");
  const proxy = proxyDaemon.litellmEnv(home, home, "4101", { HOME: home });
  assert.equal(proxy.CONTEXT_MASK_OBSERVATIONS, "0");
  assert.equal(proxy.CONTEXT_EMBEDDING_API_KEY, "secret", "the spawned proxy gets the secret");
});

test("the embedding key in proxy.env reaches the MCP server and the OpenCode 2 trim, not the env", async () => {
  const home = tmp("sc-embedkey-");
  upsertEnvFile(proxyEnvPath(home), { CONTEXT_EMBEDDING_API_KEY: "file-key" }, { mode: 0o600 });
  const env = { HOME: home };
  assert.equal((await contextStoreSettings(env, { home })).embeddingApiKey, "file-key");
  assert.equal(trimSettings(env, home).embeddingApiKey, "file-key");
  assert.equal(env.CONTEXT_EMBEDDING_API_KEY, undefined);
  assert.equal(trimSettings({ HOME: home, CONTEXT_EMBEDDING_API_KEY: "env-key" }, home).embeddingApiKey, "env-key", "env wins");
});

function writeToml(home, text) {
  fs.mkdirSync(path.dirname(configTomlPath(home)), { recursive: true });
  fs.writeFileSync(configTomlPath(home), text);
}

test("config.toml [context_store] reaches hooks, the spawned proxy and the OpenCode 2 trim; env wins", () => {
  const home = tmp("sc-storetoml-");
  writeToml(home, "[context_store]\nttl_days = 0\nembedding_model = \"m\"\n");
  const env = { HOME: home };
  ensureProductEnv(env, home);
  assert.equal(env.CONTEXT_STORE_TTL_DAYS, "0");
  assert.equal(trimSettings(env, home).ttlDays, 0);
  assert.equal(proxyDaemon.litellmEnv(home, home, "4101", { HOME: home }).CONTEXT_STORE_TTL_DAYS, "0");
  const set = { HOME: home, CONTEXT_STORE_TTL_DAYS: "5" };
  ensureProductEnv(set, home);
  assert.equal(set.CONTEXT_STORE_TTL_DAYS, "5", "env wins");
});

test("a non-integer [context_store] ttl_days is rejected on stderr", async () => {
  const home = tmp("sc-ttlfloat-");
  writeToml(home, "[context_store]\nttl_days = 7.5\n");
  const written = [];
  const write = process.stderr.write;
  process.stderr.write = (chunk) => { written.push(String(chunk)); return true; };
  let settings;
  try {
    settings = await contextStoreSettings({ HOME: home }, { home });
  } finally {
    process.stderr.write = write;
  }
  assert.equal(settings.ttlDays, 30);
  assert.match(written.join(""), /ttl_days = 7\.5.*whole number/);
});

test("config.toml [context_proxy] standing_instruction = false turns the instruction off; env wins", () => {
  const home = tmp("sc-standing-");
  writeToml(home, "[context_proxy]\nstanding_instruction = false\n");
  const env = { HOME: home };
  ensureProductEnv(env, home);
  assert.equal(env.CONTEXT_STANDING_INSTRUCTION, "0");
  assert.equal(trimSettings(env, home).standingInstruction, false);
  const set = { HOME: home, CONTEXT_STANDING_INSTRUCTION: "1" };
  ensureProductEnv(set, home);
  assert.equal(set.CONTEXT_STANDING_INSTRUCTION, "1", "env wins");
});
