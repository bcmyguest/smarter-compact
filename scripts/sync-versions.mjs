#!/usr/bin/env node
/** Copy package.json's version into each plugin manifest; `npm version` runs this before the release commit. */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export const MANIFESTS = [
  "plugin/.claude-plugin/plugin.json",
  "plugin/.codex-plugin/plugin.json",
  "plugin/.cursor-plugin/plugin.json",
  "plugin/.github/plugin/plugin.json",
];

export const packageVersion = () => JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8")).version;

/** Manifests whose `version` differs from package.json's. */
export function staleManifests() {
  const version = packageVersion();
  return MANIFESTS.filter((rel) => JSON.parse(fs.readFileSync(path.join(ROOT, rel), "utf8")).version !== version);
}

function sync() {
  const version = packageVersion();
  for (const rel of MANIFESTS) {
    const file = path.join(ROOT, rel);
    const text = fs.readFileSync(file, "utf8");
    // Replace in place so the manifest's own formatting survives.
    const next = text.replace(/("version":\s*")[^"]*(")/, `$1${version}$2`);
    if (next !== text) fs.writeFileSync(file, next);
  }
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) sync();
