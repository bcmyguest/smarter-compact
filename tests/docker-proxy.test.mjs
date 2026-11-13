import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { configureAgent } from "../plugin/lib/enable.mjs";
import {
  CONTAINER_NAME, containerEnv, containerUrl, dockerRunArgs, ensureContainer, proxyRuntime,
} from "../plugin/lib/docker-proxy.mjs";
import { LITELLM_IMAGE } from "../plugin/lib/litellm.mjs";
import { ensureContextProxyRunning, restartContextProxy } from "../plugin/lib/proxy-daemon.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const INSTALLED = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8")).version;

/** A docker CLI stand-in: records each call, answers `info`, `inspect` and `ps` from state. */
function fakeDocker({ daemon = true, container = null, published = null } = {}) {
  const calls = [];
  const exec = async (cmd, args, timeout, opts) => {
    calls.push({ args, env: opts?.env });
    if (cmd !== "docker" || !daemon) return null;
    switch (args[0]) {
      case "info": return "27.0.0\n";
      case "inspect": return container ? `${container.running}\t${container.version}\t${container.root}\t${container.port}\n` : null;
      case "ps": return published ? `${published}\t\n` : "";
      default: return "ok\n";
    }
  };
  return { exec, calls, verbs: () => calls.map((c) => c.args[0]) };
}

function home() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "sc-docker-"));
}

test("proxyRuntime prefers docker when the daemon answers, and honours the override", async () => {
  assert.equal(await proxyRuntime({}, fakeDocker().exec), "docker");
  assert.equal(await proxyRuntime({}, fakeDocker({ daemon: false }).exec), "native");
  assert.equal(await proxyRuntime({ SMARTER_COMPACT_PROXY_RUNTIME: "native" }, fakeDocker().exec), "native");
  assert.equal(await proxyRuntime({ SMARTER_COMPACT_PROXY_RUNTIME: "docker" }, fakeDocker({ daemon: false }).exec), "docker");
});

test("containerUrl points loopback at the host", () => {
  assert.equal(containerUrl("http://127.0.0.1:11434/v1"), "http://host.docker.internal:11434/v1");
  assert.equal(containerUrl("http://localhost:8080"), "http://host.docker.internal:8080");
  assert.equal(containerUrl("https://gateway.example/v1"), "https://gateway.example/v1");
});

test("containerEnv keeps proxy keys only and sets the container paths", () => {
  const env = containerEnv({
    LITELLM_MASTER_KEY: "sk-x", CONTEXT_TRIM_AT_TOKENS: "90000", PATH: "/usr/bin", PYTHONPATH: "/host/proxy",
    SMARTER_COMPACT_STATE_DIR: "/home/u/.local/share/smarter-compact",
  }, 4101);
  assert.equal(env.LITELLM_MASTER_KEY, "sk-x");
  assert.equal(env.CONTEXT_TRIM_AT_TOKENS, "90000");
  assert.equal(env.SMARTER_COMPACT_STATE_DIR, "/state");
  assert.equal(env.CONTEXT_PROXY_PORT, "4101");
  assert.equal(env.CONTEXT_EMBEDDING_URL, "http://host.docker.internal:11434/v1");
  assert.equal("PATH" in env, false);
  assert.equal("PYTHONPATH" in env, false);
});

test("dockerRunArgs restarts always, binds loopback and keeps secrets off argv", () => {
  const args = dockerRunArgs({
    root: "/pkg", port: 4101, version: "1.2.3", state: "/st", containerEnvKeys: ["LITELLM_MASTER_KEY"],
    platform: "linux", ids: { uid: 1000, gid: 1000 },
  });
  const s = args.join(" ");
  assert.match(s, /--restart always/);
  assert.match(s, /-p 127\.0\.0\.1:4101:4000/);
  assert.ok(s.includes(`-v ${path.join("/pkg", "proxy")}:/app/proxy:ro`), s); // host path in the platform's form
  assert.match(s, /-v \/st:\/state/);
  assert.match(s, /--user 1000:1000/);
  assert.match(s, /-e LITELLM_MASTER_KEY /);
  assert.ok(args.includes(LITELLM_IMAGE));
  assert.equal(args[args.indexOf("--name") + 1], CONTAINER_NAME);
  assert.equal(dockerRunArgs({ root: "/p", port: 1, state: "/s", containerEnvKeys: [], platform: "win32", ids: { uid: 1, gid: 1 } }).includes("--user"), false);
});

test("ensureContainer leaves a running container for this version alone (it may still be booting)", async () => {
  const d = fakeDocker({ container: { running: true, version: "1.2.3", root: "/pkg", port: "4101" } });
  const r = await ensureContainer({ home: home(), env: {}, root: "/pkg", port: "4101", version: "1.2.3", nativeEnv: {}, exec: d.exec });
  assert.equal(r.status, "running");
  assert.deepEqual(d.verbs(), ["inspect"]);
});

test("ensureContainer recreates a container for another version", async () => {
  const d = fakeDocker({ container: { running: true, version: "0.0.1", root: "/old", port: "4101" } });
  const h = home();
  const r = await ensureContainer({
    home: h, env: { SMARTER_COMPACT_STATE_DIR: path.join(h, "st") }, root: "/pkg", port: "4101", version: "1.2.3",
    nativeEnv: { LITELLM_MASTER_KEY: "sk-x" }, exec: d.exec,
  });
  assert.equal(r.status, "created");
  assert.deepEqual(d.verbs(), ["inspect", "rm", "run"]);
  const run = d.calls.at(-1);
  assert.equal(run.env.LITELLM_MASTER_KEY, "sk-x");
  assert.equal(run.args.join(" ").includes("sk-x"), false);
  assert.ok(fs.existsSync(path.join(h, "st", "context")));
});

test("session start creates the container when docker runs and the proxy is down", async () => {
  const h = home();
  configureAgent(h, "codex", { enabled: true, port: 4101 });
  const d = fakeDocker();
  let probes = 0;
  const r = await ensureContextProxyRunning({
    home: h,
    env: { ...process.env, SMARTER_COMPACT_AUTO_PROXY: "1", CONTEXT_PROXY_PORT: "4101", SMARTER_COMPACT_PROXY_RUNTIME: "" },
    importMetaUrl: import.meta.url,
    fetch: async () => { probes += 1; if (probes < 2) throw new Error("down"); return { ok: true }; },
    exec: d.exec,
    waitMs: 2000,
  });
  assert.deepEqual([r.ok, r.status, r.runtime], [true, "started", "docker"]);
  assert.deepEqual(d.verbs(), ["info", "inspect", "rm", "run"]);
  const args = d.calls.at(-1).args;
  assert.ok(args.includes(`smarter-compact.version=${INSTALLED}`));
  assert.ok(args.includes(`smarter-compact.root=${ROOT}`));
});

test("a stale proxy container is recreated from the installed package when idle", async () => {
  const h = home();
  const dir = path.join(h, ".local", "share", "smarter-compact", "context");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, "proxy-4101.json"), JSON.stringify({ version: "0.0.1", pid: 1, started: 1, last_request: null }));
  const d = fakeDocker({ published: CONTAINER_NAME });
  const env = { ...process.env, SMARTER_COMPACT_AUTO_PROXY: "1", CONTEXT_PROXY_PORT: "4101" };
  delete env.SMARTER_COMPACT_STATE_DIR;
  delete env.OPENMEM_STATE_DIR;
  const r = await ensureContextProxyRunning({
    home: h, env, importMetaUrl: import.meta.url, fetch: async () => ({ ok: true }), exec: d.exec, waitMs: 50, now: 1_800_000_000_000,
  });
  assert.deepEqual([r.status, r.from, r.to], ["restarted", "0.0.1", INSTALLED]);
  assert.deepEqual(d.verbs(), ["ps", "rm", "run"]);
});

test("proxy restart recreates its own container", async () => {
  const d = fakeDocker({ published: CONTAINER_NAME });
  const r = await restartContextProxy({
    home: home(), env: { ...process.env, CONTEXT_PROXY_PORT: "4101" }, importMetaUrl: import.meta.url,
    fetch: async () => ({ ok: true }), exec: d.exec, waitMs: 50,
  });
  assert.deepEqual([r.ok, r.via], [true, `docker ${CONTAINER_NAME}`]);
  assert.deepEqual(d.verbs(), ["ps", "rm", "run"]);
});
