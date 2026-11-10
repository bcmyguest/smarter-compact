import fs from "node:fs";
import { closeSessions } from "../plugin/lib/session-db.mjs";

/** Delete a test home; close cached session databases first, since Windows cannot delete an open file. */
export function rmHome(dir) {
  closeSessions();
  fs.rmSync(dir, { recursive: true, force: true });
}
