import { spawnSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { writeFileAtomic } from "./file-io.mjs";

/** OpenCode major version from `opencode --version`; any failure counts as 1. */
export function opencodeMajorVersion({ env = process.env, spawn = spawnSync } = {}) {
  try {
    const r = spawn("opencode", ["--version"], { env, encoding: "utf8", timeout: 5000, stdio: ["ignore", "pipe", "ignore"] });
    const match = r?.status === 0 ? /(\d+)\.\d+/.exec(String(r.stdout || "")) : null;
    const major = match ? Number(match[1]) : 1;
    return major >= 2 ? major : 1;
  } catch {
    return 1;
  }
}

export function opencodeConfigPath(home) {
  const dir = path.join(home, ".config", "opencode");
  for (const name of ["opencode.jsonc", "opencode.json"]) {
    const file = path.join(dir, name);
    if (fs.existsSync(file)) return file;
  }
  return path.join(dir, "opencode.json");
}

function tokens(source) {
  const out = [];
  for (let i = 0; i < source.length;) {
    if (/\s/.test(source[i])) { i++; continue; }
    if (source.startsWith("//", i)) { i = source.indexOf("\n", i); if (i < 0) break; continue; }
    if (source.startsWith("/*", i)) {
      const end = source.indexOf("*/", i + 2);
      if (end < 0) throw new SyntaxError("Unclosed JSONC comment");
      i = end + 2;
      continue;
    }
    const start = i;
    if (source[i] === '"') {
      i++;
      while (i < source.length) {
        if (source[i++] === '"') break;
        if (source[i - 1] === "\\") i++;
      }
    } else if ("{}[]:,".includes(source[i])) i++;
    else while (i < source.length && !/[\s{}\[\]:,]/.test(source[i]) && !source.startsWith("//", i) && !source.startsWith("/*", i)) i++;
    out.push({ value: source.slice(start, i), start, end: i });
  }
  return out;
}

export function parseJsonc(source) {
  const list = tokens(source);
  const compact = list.filter((token, i) => token.value !== "," || !["}", "]"].includes(list[i + 1]?.value));
  return JSON.parse(compact.map((token) => token.value).join(""));
}

function fieldSpan(list, name) {
  let depth = 0;
  let found = null;
  for (let i = 0; i < list.length; i++) {
    const token = list[i];
    if (depth === 1 && token.value.startsWith('"') && list[i + 1]?.value === ":" && JSON.parse(token.value) === name) {
      const start = list[i + 2]?.start;
      if (start === undefined) throw new SyntaxError(`Missing JSONC value for ${name}`);
      let nested = 0;
      let end = start;
      for (let j = i + 2; j < list.length; j++) {
        const value = list[j].value;
        if (nested === 0 && (value === "," || value === "}")) break;
        if (value === "{" || value === "[") nested++;
        if (value === "}" || value === "]") nested--;
        end = list[j].end;
      }
      found = { start, end };
    }
    if (token.value === "{" || token.value === "[") depth++;
    if (token.value === "}" || token.value === "]") depth--;
  }
  return found;
}

/**
 * Update one top-level JSONC field while retaining text and comments outside that field.
 * The file is replaced atomically; `opts.mode` sets its mode, else the existing mode is kept.
 */
export function mergeOpencodeConfig(home, field, update, opts = {}) {
  const file = opencodeConfigPath(home);
  const source = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "{}\n";
  const parsed = parseJsonc(source);
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") throw new SyntaxError("OpenCode config must be an object");
  const value = update(parsed[field]);
  const list = tokens(source);
  const span = fieldSpan(list, field);
  let output;
  if (span) output = source.slice(0, span.start) + JSON.stringify(value, null, 2) + source.slice(span.end);
  else {
    const close = list.findLast((token) => token.value === "}");
    if (!close) throw new SyntaxError("OpenCode config has no closing object brace");
    const hasFields = Object.keys(parsed).length > 0;
    const last = list[list.indexOf(close) - 1];
    const separator = hasFields && last?.value !== "," ? "," : "";
    output = source.slice(0, close.start) + `${separator}\n  ${JSON.stringify(field)}: ${JSON.stringify(value, null, 2)}\n` + source.slice(close.start);
  }
  writeFileAtomic(file, output, opts);
  return file;
}
