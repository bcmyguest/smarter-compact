/** Install the bundled plugin tree for Cursor Agent local plugins. */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { PRODUCT } from "./brand.mjs";
import { packageRoot } from "./proxy-daemon.mjs";

export function cursorPluginDest(home = os.homedir()) {
  return path.join(home, ".cursor", "plugins", "local", PRODUCT);
}

/**
 * @param {{ home?: string, importMetaUrl: string, copy?: boolean }} opts
 * @returns {{ ok: boolean, dest: string, mode: "symlink"|"copy", message?: string }}
 */
export function installCursorPlugin({ home = os.homedir(), importMetaUrl, copy = false } = {}) {
  const root = packageRoot(importMetaUrl);
  if (!root) return { ok: false, dest: "", mode: "copy", message: "package root not found (reinstall smarter-compact)" };
  const src = path.join(root, "plugin");
  if (!fs.existsSync(path.join(src, ".cursor-plugin", "plugin.json"))) {
    return { ok: false, dest: "", mode: "copy", message: `missing Cursor plugin manifest under ${src}` };
  }
  const dest = cursorPluginDest(home);
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  try {
    fs.rmSync(dest, { recursive: true, force: true });
  } catch { /* missing */ }
  const mode = copy ? "copy" : "symlink";
  try {
    if (copy) fs.cpSync(src, dest, { recursive: true });
    else fs.symlinkSync(src, dest, "dir");
  } catch (err) {
    if (!copy && err.code === "EPERM") {
      fs.cpSync(src, dest, { recursive: true });
      return { ok: true, dest, mode: "copy", message: "symlink failed; copied instead" };
    }
    return { ok: false, dest, mode, message: err.message };
  }
  return { ok: true, dest, mode };
}
