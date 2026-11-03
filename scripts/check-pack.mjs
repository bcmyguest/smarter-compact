#!/usr/bin/env node
/** Fail if `npm pack` would ship forbidden paths or miss required files. */
import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const REQUIRED = [
  "package.json",
  "README.md",
  "LICENSE",
  "index.mjs",
  "plugin/scripts/smarter-compact.mjs",
  "proxy/smarter_compact_hook.py",
  "proxy/config.yaml",
];

const FORBIDDEN = [
  [/(^|\/)\.env$/, ".env"],
  [/(^|\/)\.git(\/|$)/, ".git"],
  [/(^|\/)\.github(\/|$)/, ".github"],
  [/(^|\/)\.claude-plugin(\/|$)/, ".claude-plugin"],
  [/(^|\/)\.cursor-plugin(\/|$)/, ".cursor-plugin"],
  [/(^|\/)tests(\/|$)/, "tests/"],
  [/\/home\/[^/\s]+/, "home path"],
  [/\/Users\/[^/\s]+/, "Users path"],
];

function packList() {
  const r = spawnSync("npm", ["pack", "--dry-run", "--json"], { cwd: ROOT, encoding: "utf8" });
  if (r.status !== 0) {
    console.error(r.stderr || r.stdout);
    process.exit(1);
  }
  const data = JSON.parse(r.stdout.trim());
  const entry = Array.isArray(data) ? data[0] : data;
  return entry.files.map((f) => f.path);
}

const files = packList();
const missing = REQUIRED.filter((f) => !files.includes(f));
const problems = [];
for (const f of files) {
  for (const [re, label] of FORBIDDEN) {
    if (re.test(f)) problems.push(`${f}: forbidden (${label})`);
  }
}
if (missing.length) problems.push(`missing required: ${missing.join(", ")}`);
if (problems.length) {
  for (const p of problems) console.error(p);
  process.exit(1);
}
console.log(`pack ok (${files.length} files)`);
