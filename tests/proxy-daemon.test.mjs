import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { resolveProxyPort } from "../plugin/lib/brand.mjs";
import { agentsPointedAtProxy, configureAgent, proxyEnvPath } from "../plugin/lib/enable.mjs";
import { upsertEnvFile } from "../plugin/lib/file-io.mjs";
import { ensureContextProxyRunning, packageRoot, probeContextProxy } from "../plugin/lib/proxy-daemon.mjs";
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

test("resolveProxyPort reads proxy.env when env unset", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-port-"));
  upsertEnvFile(proxyEnvPath(home), { CONTEXT_PROXY_PORT: "4209" }, { mode: 0o600 });
  const env = { ...process.env };
  delete env.CONTEXT_PROXY_PORT;
  assert.equal(resolveProxyPort(home, env), 4209);
});

test("agentsPointedAtProxy detects claude wiring", () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "sc-point-"));
  fs.mkdirSync(path.join(home, ".claude"), { recursive: true });
  configureAgent(home, "claude", { enabled: true, port: 4101 });
  assert.deepEqual(agentsPointedAtProxy(home, 4101), ["claude"]);
});
