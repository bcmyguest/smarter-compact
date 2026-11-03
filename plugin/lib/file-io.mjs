/**
 * File helpers for the coding-agent installer: atomic rewrites of user config files
 * (temp file in the same directory + rename), JSON read/write, and a silent logger.
 */
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const SILENT = Object.freeze({ info: () => {}, ok: () => {}, warn: () => {} });

/**
 * Replace `file` with `data` atomically. A symlinked file is written through to its target.
 * Mode: `opts.mode` when given (also on an existing file), else the existing file's mode,
 * else the process default (0666 minus umask).
 * @param {string} file
 * @param {string} data
 * @param {{ mode?: number }} [opts]
 */
export function writeFileAtomic(file, data, opts = {}) {
  let target = file;
  try {
    target = fs.realpathSync(file);
  } catch {
    // Missing file: write it where asked.
  }
  const dir = path.dirname(target);
  fs.mkdirSync(dir, { recursive: true });
  const existing = fs.statSync(target, { throwIfNoEntry: false });
  const mode = opts.mode ?? (existing ? existing.mode & 0o7777 : undefined);
  const tmp = path.join(dir, `.${path.basename(target)}.${process.pid}.${crypto.randomBytes(4).toString("hex")}.tmp`);
  try {
    const fd = fs.openSync(tmp, "wx", mode ?? 0o666);
    try {
      fs.writeFileSync(fd, data);
      if (mode !== undefined) fs.fchmodSync(fd, mode);
      fs.fsyncSync(fd);
    } finally {
      fs.closeSync(fd);
    }
    fs.renameSync(tmp, target);
  } catch (err) {
    fs.rmSync(tmp, { force: true });
    throw err;
  }
}

/** Parsed JSON of `file`, or `{}` when it is missing. Malformed JSON throws. */
export function readJson(file) {
  return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : {};
}

/** @param {string} file @param {unknown} value @param {{ mode?: number }} [opts] */
export function writeJson(file, value, opts = {}) {
  writeFileAtomic(file, `${JSON.stringify(value, null, 2)}\n`, opts);
}

/**
 * Set `KEY=value` lines in a dotenv file, replacing earlier lines for those keys and keeping
 * every other line. The file keeps its mode; a missing file is created with `opts.mode`.
 * @param {string} file
 * @param {Record<string, string>} entries
 * @param {{ mode?: number }} [opts]
 */
export function upsertEnvFile(file, entries, opts = {}) {
  const keys = Object.keys(entries);
  const exists = fs.existsSync(file);
  const lines = exists ? fs.readFileSync(file, "utf8").split(/\r?\n/) : [];
  const kept = lines.filter((line) => !keys.some((k) => line.startsWith(`${k}=`)));
  while (kept.length && kept[kept.length - 1] === "") kept.pop();
  const text = `${[...kept, ...keys.map((k) => `${k}=${entries[k]}`)].join("\n")}\n`;
  writeFileAtomic(file, text, exists ? {} : opts);
}
