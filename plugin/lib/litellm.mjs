/**
 * The native LiteLLM the proxy runs: find it on PATH, check its version, install it with uv when it is missing or old.
 */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { gte } from "@renovatebot/pep440";

// The first LiteLLM release candidate with PR #41522, the ChatGPT/Codex subscription pass-through (/backend-api/codex).
export const LITELLM_MIN = "1.109.0rc1";
// A pre-release in the specifier lets uv pick pre-releases for litellm alone.
export const LITELLM_SPEC = `litellm[proxy]>=${LITELLM_MIN}`;
export const UV_INSTALL_URL = "https://docs.astral.sh/uv/getting-started/installation/";
// litellm imports its whole proxy stack before it prints a version.
const VERSION_TIMEOUT_MS = 60_000;
const INSTALL_TIMEOUT_MS = 15 * 60_000;

/** Full path of the first executable `bin` on env.PATH, or null. spawn reports ENOENT only through an async event. */
export function whichOnPath(bin, env = process.env) {
  for (const dir of String(env.PATH || "").split(path.delimiter).filter(Boolean)) {
    const file = path.join(dir, bin);
    try {
      fs.accessSync(file, fs.constants.X_OK);
      return file;
    } catch { /* next */ }
  }
  return null;
}

export function onPath(bin, env = process.env) {
  return whichOnPath(bin, env) !== null;
}

/** Version the first `litellm` on PATH reports (`LiteLLM: Current Version = X`), or null. */
export function litellmVersion(env = process.env) {
  if (!onPath("litellm", env)) return null;
  const r = spawnSync("litellm", ["--version"], { env, encoding: "utf8", timeout: VERSION_TIMEOUT_MS });
  return /Current Version = (\S+)/.exec(`${r.stdout || ""}${r.stderr || ""}`)?.[1] ?? null;
}

function current(version) {
  try {
    return Boolean(version) && gte(version, LITELLM_MIN);
  } catch {
    return false;
  }
}

/**
 * Ensure the `litellm` on PATH is at least LITELLM_MIN; install LITELLM_SPEC with `uv tool install --force` when not.
 * `log` receives one line before the install starts; uv's own output goes to stderr.
 */
export function ensureLitellm({ env = process.env, log = () => {} } = {}) {
  const found = litellmVersion(env);
  if (current(found)) return { ok: true, status: "present", version: found };
  if (!onPath("uv", env)) return { ok: false, reason: "uv-missing", found };

  log(`installing ${LITELLM_SPEC} with uv tool install (${found ? `found ${found}` : "litellm not on PATH"})`);
  const install = spawnSync("uv", ["tool", "install", "--force", LITELLM_SPEC], {
    env, stdio: ["ignore", 2, 2], timeout: INSTALL_TIMEOUT_MS,
  });
  if (install.status !== 0) return { ok: false, reason: "install-failed", found };

  const version = litellmVersion(env);
  if (current(version)) return { ok: true, status: "installed", version };
  const uvBin = spawnSync("uv", ["tool", "dir", "--bin"], { env, encoding: "utf8" }).stdout?.trim() || null;
  return { ok: false, reason: version ? "shadowed" : "not-on-path", found: version, path: whichOnPath("litellm", env), uvBin };
}

/** One line that says why LiteLLM is unusable and how to fix it. */
export function litellmProblem(result, product) {
  const docker = `or run the proxy with docker compose from a clone (docs/context-proxy.md)`;
  switch (result.reason) {
    case "uv-missing":
      return `litellm >= ${LITELLM_MIN} is not on PATH${result.found ? ` (found ${result.found})` : ""} and uv is missing. Install uv (${UV_INSTALL_URL}) and run ${product} enable again, ${docker}.`;
    case "install-failed":
      return `uv tool install ${LITELLM_SPEC} failed (output above). Fix it and run ${product} enable again, ${docker}.`;
    case "shadowed":
      return `uv installed litellm in ${result.uvBin}, but ${result.path} (${result.found}) comes first on PATH. Remove it or put ${result.uvBin} first.`;
    case "not-on-path":
      return `uv installed litellm in ${result.uvBin}, which is not on PATH. Add it (uv tool update-shell) and run ${product} enable again.`;
    default:
      return `litellm is unusable (${result.reason}).`;
  }
}
