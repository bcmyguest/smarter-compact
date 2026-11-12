/**
 * The context proxy as a Docker container: the pinned LiteLLM image with the installed package's `proxy/` mounted,
 * created with `--restart always` so the Docker daemon brings it back after a reboot or crash. The default runtime
 * when a Docker daemon answers; `SMARTER_COMPACT_PROXY_RUNTIME=native` keeps the uv-installed litellm instead.
 */
import fs from "node:fs";
import path from "node:path";
import { PROXY_ENV_CONTEXT_KEYS, PROXY_ENV_SECRET_KEYS, stateParent } from "./brand.mjs";
import { LITELLM_IMAGE } from "./litellm.mjs";

export const CONTAINER_NAME = "smarter-compact-proxy";
export const RUNTIME_ENV = "SMARTER_COMPACT_PROXY_RUNTIME";
const LABEL = "smarter-compact";
const CONTAINER_PORT = 4000;
const INFO_TIMEOUT_MS = 10_000;
const RUN_TIMEOUT_MS = 10 * 60_000; // `docker run` pulls the image when it is missing
const RM_TIMEOUT_MS = 30_000;
const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]", "::1"]);

/**
 * "docker" or "native". `SMARTER_COMPACT_PROXY_RUNTIME` forces one; otherwise docker when its daemon answers.
 * exec(cmd, args, timeoutMs, opts) resolves stdout, or null on any failure.
 */
export async function proxyRuntime(env, exec) {
  const forced = String(env[RUNTIME_ENV] || "").trim().toLowerCase();
  if (forced === "docker" || forced === "native") return forced;
  return await dockerAvailable(exec) ? "docker" : "native";
}

export async function dockerAvailable(exec) {
  const out = await exec("docker", ["info", "--format", "{{.ServerVersion}}"], INFO_TIMEOUT_MS);
  return Boolean(out && out.trim());
}

/** A loopback URL as the container reaches it: the host through host.docker.internal. */
export function containerUrl(url) {
  try {
    const u = new URL(url);
    if (!LOOPBACK.has(u.hostname)) return url;
    u.hostname = "host.docker.internal";
    return u.toString().replace(/\/$/, url.endsWith("/") ? "/" : "");
  } catch {
    return url;
  }
}

/**
 * Environment the container gets, from the env the native proxy would run with (litellmEnv). Only the proxy's own
 * keys and the upstream ANTHROPIC_BASE_URL cross over; host paths and the host PATH do not.
 */
export function containerEnv(nativeEnv, port) {
  const out = {};
  for (const key of ["LITELLM_MASTER_KEY", "ANTHROPIC_BASE_URL", ...PROXY_ENV_CONTEXT_KEYS, ...PROXY_ENV_SECRET_KEYS]) {
    const v = nativeEnv[key];
    if (v !== undefined && v !== null && String(v).trim() !== "") out[key] = String(v);
  }
  if (out.ANTHROPIC_BASE_URL) out.ANTHROPIC_BASE_URL = containerUrl(out.ANTHROPIC_BASE_URL);
  // settings.py defaults to 127.0.0.1:11434, which inside the container is the container itself.
  out.CONTEXT_EMBEDDING_URL = containerUrl(out.CONTEXT_EMBEDDING_URL || "http://127.0.0.1:11434/v1");
  Object.assign(out, {
    HOME: "/tmp",
    PYTHONDONTWRITEBYTECODE: "1",
    SMARTER_COMPACT_STATE_DIR: "/state",
    CONTEXT_PROXY_PORT: String(port),
  });
  return out;
}

/** `docker run` arguments. Values go by name only (`-e KEY`), read from the CLI's env, so no secret is on argv. */
export function dockerRunArgs({ root, port, version, state, containerEnvKeys, platform = process.platform, ids }) {
  const args = [
    "run", "-d", "--name", CONTAINER_NAME, "--restart", "always",
    "--label", `${LABEL}.version=${version ?? ""}`,
    "--label", `${LABEL}.root=${root}`,
    "--label", `${LABEL}.port=${port}`,
    "-p", `127.0.0.1:${port}:${CONTAINER_PORT}`,
    "-v", `${path.join(root, "proxy")}:/app/proxy:ro`,
    "-v", `${path.join(root, "package.json")}:/app/package.json:ro`,
    "-v", `${state}:/state`,
    "--add-host", "host.docker.internal:host-gateway",
  ];
  // Run as the host user so the session store stays host-owned (docs/context-store.md).
  if (platform !== "win32" && ids) args.push("--user", `${ids.uid}:${ids.gid}`);
  for (const key of containerEnvKeys) args.push("-e", key);
  args.push(LITELLM_IMAGE, "--config", "/app/proxy/config.yaml", "--port", String(CONTAINER_PORT));
  return args;
}

/** The container's state: running, and the version, root and port it was created for; null when it does not exist. */
export async function inspectContainer(exec) {
  const fmt = `{{.State.Running}}\t{{index .Config.Labels "${LABEL}.version"}}\t{{index .Config.Labels "${LABEL}.root"}}\t{{index .Config.Labels "${LABEL}.port"}}`;
  const out = await exec("docker", ["inspect", "--format", fmt, CONTAINER_NAME], INFO_TIMEOUT_MS);
  if (!out) return null;
  const [running, version, root, port] = out.trim().split("\t");
  return { running: running === "true", version: version || null, root: root || null, port: port || null };
}

/** Remove the container and create it from the installed package. */
export async function recreateContainer({ home, env, root, port, version, nativeEnv, exec, log }) {
  await exec("docker", ["rm", "-f", CONTAINER_NAME], RM_TIMEOUT_MS);
  const state = stateParent(home, env);
  // Docker would create a missing bind source as root.
  fs.mkdirSync(path.join(state, "context"), { recursive: true });
  const cenv = containerEnv(nativeEnv, port);
  const ids = typeof process.getuid === "function" ? { uid: process.getuid(), gid: process.getgid() } : null;
  const args = dockerRunArgs({ root, port, version, state, containerEnvKeys: Object.keys(cenv), ids });
  const out = await exec("docker", args, RUN_TIMEOUT_MS, { env: { ...process.env, ...cenv }, log });
  return out !== null ? { ok: true } : { ok: false, reason: "docker-run-failed" };
}

/**
 * Make the container serve the installed package on port: leave one that is running for this version, root and port
 * (it may still be booting, e.g. right after a reboot), else recreate it.
 */
export async function ensureContainer(opts) {
  const { root, port, version, exec } = opts;
  const c = await inspectContainer(exec);
  if (c?.running && c.version === (version ?? null) && c.root === root && c.port === String(port)) {
    return { ok: true, status: "running" };
  }
  const r = await recreateContainer(opts);
  return r.ok ? { ok: true, status: "created" } : r;
}
