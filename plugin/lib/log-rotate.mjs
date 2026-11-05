import fs from "node:fs";

/** Size at which an append-only log moves to `<file>.1`, replacing the previous generation. */
export const LOG_MAX_BYTES = 5 * 1024 * 1024;

/** Rename `file` to `file.1` when it reaches `maxBytes`; never throws. */
export function rotateLog(file, maxBytes = LOG_MAX_BYTES) {
  try {
    if (fs.statSync(file).size >= maxBytes) fs.renameSync(file, `${file}.1`);
  } catch { /* missing or unreadable: nothing to rotate */ }
}
