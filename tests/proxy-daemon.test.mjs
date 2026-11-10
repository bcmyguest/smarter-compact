import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { ensureProductEnv, resolveProxyPort } from "../plugin/lib/brand.mjs";
import { agentsPointedAtProxy, configureAgent, proxyEnvPath } from "../plugin/lib/enable.mjs";
import { upsertEnvFile } from "../plugin/lib/file-io.mjs";
import { ensureContextProxyRunning, litellmEnv, packageRoot, probeContextProxy, restartContextProxy } from "../plugin/lib/proxy-daemon.mjs";
import http from "node:http";
import { once } from "node:events";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

test("packageRoot finds proxy from repo layout", () => {
  assert.equal(packageRoot(import.meta.url), ROOT);
});

test("probeContextProxy respects fetch mock", async () => {
  assert.equal(await probeContextProxy(4101, { fetch: async () => ({ ok: true }) }), true);
  assert.equal(await probeContextProxy(4101, { fetch: async () => { throw new Error("down"); } }), false);
});

test("ensure skips when no agent points at proxy", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-proxy-"));
  const r = await ensureContextProxyRunning({
    home,
    env: { ...process.env, SMARTER_COMPACT_AUTO_PROXY: "1" },
    importMetaUrl: import.meta.url,
    fetch: async () => { throw new Error("down"); },
  });
  assert.equal(r.ok, true);
  assert.equal(r.status, "not-needed");
});

test("ensure reports litellm-missing at once when litellm is not on PATH", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-nolitellm-"));
  configureAgent(home, "codex", { enabled: true, port: 4101 });
  const emptyBin = fs.mkdtempSync(path.join(os.tmpdir(), "sc-bin-"));
  const started = Date.now();
  const r = await ensureContextProxyRunning({
    home,
    env: { ...process.env, PATH: emptyBin, SMARTER_COMPACT_AUTO_PROXY: "1" },
    importMetaUrl: import.meta.url,
    fetch: async () => { throw new Error("down"); },
  });
  assert.equal(r.ok, false);
  assert.equal(r.status, "spawn-failed");
  assert.equal(r.reason, "litellm-missing");
  assert.ok(Date.now() - started < 3000);
});

test("litellmEnv drops client settings that point the proxy at itself", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-env-"));
  const loop = litellmEnv(home, ROOT, "4101", {
    PATH: process.env.PATH,
    ANTHROPIC_BASE_URL: "http://127.0.0.1:4101",
    ANTHROPIC_CUSTOM_HEADERS: "x-litellm-api-key: Bearer k",
  });
  assert.equal("ANTHROPIC_BASE_URL" in loop, false);
  assert.equal("ANTHROPIC_CUSTOM_HEADERS" in loop, false);
  const upstream = litellmEnv(home, ROOT, "4101", { PATH: process.env.PATH, ANTHROPIC_BASE_URL: "https://gateway.example" });
  assert.equal(upstream.ANTHROPIC_BASE_URL, "https://gateway.example");
});

test("resolveProxyPort reads proxy.env when env unset", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-port-"));
  upsertEnvFile(proxyEnvPath(home), { CONTEXT_PROXY_PORT: "4209" }, { mode: 0o600 });
  const env = { ...process.env };
  delete env.CONTEXT_PROXY_PORT;
  assert.equal(resolveProxyPort(home, env), 4209);
});

test("ensureProductEnv loads CONTEXT_TRIM_AT_TOKENS from proxy.env", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-trim-"));
  upsertEnvFile(proxyEnvPath(home), { CONTEXT_TRIM_AT_TOKENS: "88000" }, { mode: 0o600 });
  const env = { HOME: home };
  ensureProductEnv(env, home);
  assert.equal(env.CONTEXT_TRIM_AT_TOKENS, "88000");
  env.CONTEXT_TRIM_AT_TOKENS = "99000";
  ensureProductEnv(env, home);
  assert.equal(env.CONTEXT_TRIM_AT_TOKENS, "99000");
});

test("agentsPointedAtProxy detects claude wiring", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-point-"));
  fs.mkdirSync(path.join(home, ".claude"), { recursive: true });
  configureAgent(home, "claude", { enabled: true, port: 4101 });
  assert.deepEqual(agentsPointedAtProxy(home, 4101), ["claude"]);
});

function staleHome(status, port = 4101) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-stale-"));
  const dir = path.join(home, ".local", "share", "smarter-compact", "context");
  fs.mkdirSync(dir, { recursive: true });
  if (status) fs.writeFileSync(path.join(dir, `proxy-${port}.json`), JSON.stringify(status));
  return home;
}

const INSTALLED = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8")).version;
const NOW = 1_800_000_000_000;
const up = async () => ({ ok: true });

async function ensureStale(home, exec, now = NOW) {
  const env = { ...process.env, SMARTER_COMPACT_AUTO_PROXY: "1", CONTEXT_PROXY_PORT: "4101" };
  delete env.SMARTER_COMPACT_STATE_DIR;
  delete env.OPENMEM_STATE_DIR;
  return ensureContextProxyRunning({ home, env, importMetaUrl: import.meta.url, fetch: up, exec, now, waitMs: 50 });
}

test("a proxy on the installed version is left alone", async () => {
  const home = staleHome({ version: INSTALLED, pid: 1, started: 1, last_request: null });
  const r = await ensureStale(home, async () => { throw new Error("no docker call expected"); });
  assert.equal(r.status, "already-up");
});

test("a proxy without a status file gets a restart-once warning", async () => {
  const r = await ensureStale(staleHome(null), async () => null);
  assert.equal(r.status, "stale");
  assert.match(r.warning, /reports no version; run `smarter-compact proxy restart`/);
});

test("a stale compose proxy restarts when idle and its checkout matches", async () => {
  const checkout = fs.mkdtempSync(path.join(os.tmpdir(), "sc-checkout-"));
  fs.writeFileSync(path.join(checkout, "package.json"), JSON.stringify({ version: INSTALLED }));
  const home = staleHome({ version: "0.0.1", pid: 7, started: 1, last_request: NOW / 1000 - 120 });
  const calls = [];
  const exec = async (cmd, args) => {
    calls.push(args[0]);
    return args[0] === "ps" ? `sc-proxy\t${checkout}\n` : "sc-proxy\n";
  };
  const r = await ensureStale(home, exec);
  assert.deepEqual([r.status, r.from, r.to], ["restarted", "0.0.1", INSTALLED]);
  assert.deepEqual(calls, ["ps", "restart"]);
});

test("a busy stale compose proxy only warns", async () => {
  const checkout = fs.mkdtempSync(path.join(os.tmpdir(), "sc-checkout-"));
  fs.writeFileSync(path.join(checkout, "package.json"), JSON.stringify({ version: INSTALLED }));
  const home = staleHome({ version: "0.0.1", pid: 7, started: 1, last_request: NOW / 1000 - 10 });
  const calls = [];
  const r = await ensureStale(home, async (cmd, args) => { calls.push(args[0]); return `sc-proxy\t${checkout}\n`; });
  assert.equal(r.status, "stale");
  assert.match(r.warning, /restarts once idle/);
  assert.deepEqual(calls, ["ps"]);
});

test("a compose proxy whose checkout is behind is not restarted", async () => {
  const checkout = fs.mkdtempSync(path.join(os.tmpdir(), "sc-checkout-"));
  fs.writeFileSync(path.join(checkout, "package.json"), JSON.stringify({ version: "0.0.1" }));
  const home = staleHome({ version: "0.0.1", pid: 7, started: 1, last_request: null });
  const calls = [];
  const r = await ensureStale(home, async (cmd, args) => { calls.push(args[0]); return `sc-proxy\t${checkout}\n`; });
  assert.match(r.warning, /update that checkout/);
  assert.deepEqual(calls, ["ps"]);
});

// A fake litellm: a node HTTP server answering /health/liveliness on --port; "litellm" stays in its cmdline.
function fakeLitellmBin() {
  const bin = fs.mkdtempSync(path.join(os.tmpdir(), "sc-fake-ll-"));
  const server = path.join(bin, "server.mjs");
  fs.writeFileSync(server, 'import http from "node:http";\nconst port = Number(process.argv[process.argv.indexOf("--port") + 1]);\nhttp.createServer((q, r) => r.end("ok")).listen(port, "127.0.0.1");\n');
  fs.writeFileSync(path.join(bin, "litellm"), `#!/bin/sh\nexec "${process.execPath}" "${server}" litellm "$@"\n`, { mode: 0o755 });
  return bin;
}

async function freePort() {
  const srv = http.createServer().listen(0, "127.0.0.1");
  await once(srv, "listening");
  const { port } = srv.address();
  srv.close();
  return port;
}

const SH_ONLY = process.platform === "win32" && "fake litellm is a sh script";

test("proxy restart starts, then replaces, the proxy it owns", { skip: SH_ONLY }, async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-restart-"));
  const port = await freePort();
  const env = { ...process.env, PATH: `${fakeLitellmBin()}${path.delimiter}${process.env.PATH}`, CONTEXT_PROXY_PORT: String(port) };
  const opts = { home, env, importMetaUrl: import.meta.url, waitMs: 5_000, exec: async () => null };
  const first = await restartContextProxy(opts);
  assert.equal(first.ok, true, first.reason);
  const second = await restartContextProxy(opts);
  try {
    assert.equal(second.ok, true, second.reason);
    assert.notEqual(second.pid, first.pid);
    assert.throws(() => process.kill(first.pid, 0));
    assert.equal(await probeContextProxy(port), true);
  } finally {
    process.kill(second.pid, "SIGTERM");
  }
});

test("proxy restart leaves a listener it did not start alone", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-restart-"));
  const srv = http.createServer((q, r) => r.end("ok")).listen(0, "127.0.0.1");
  await once(srv, "listening");
  try {
    const env = { ...process.env, CONTEXT_PROXY_PORT: String(srv.address().port) };
    const r = await restartContextProxy({ home, env, importMetaUrl: import.meta.url, exec: async () => null });
    assert.deepEqual([r.ok, r.status], [false, "foreign"]);
    assert.equal(await probeContextProxy(srv.address().port), true);
  } finally {
    srv.close();
  }
});
