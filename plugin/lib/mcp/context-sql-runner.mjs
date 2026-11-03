/**
 * Child process for smart_compact_sql (cli/mcp/context-store.mjs): reads {file, sql, cap, cellMax} as JSON on
 * stdin, runs the statement on a read-only connection and prints {columns, rows, truncated} or {error}. The parent
 * kills it after its time limit, which also stops a long-running SQLite step.
 */
import fs from "node:fs";

const SQLITE_OK = 0;
const SQLITE_DENY = 1;
// sqlite3_set_authorizer action codes allowed for a query: SELECT, READ, FUNCTION, RECURSIVE.
const ALLOWED_ACTIONS = new Set([21, 20, 31, 33]);
// SQLITE_PRAGMA; FTS5 reads `PRAGMA data_version` internally on a MATCH. User SQL never reaches here with a
// PRAGMA (the parent's statement check rejects it), and an assignment carries a value in arg2.
const SQLITE_PRAGMA = 19;
const READ_PRAGMAS = new Set(["data_version"]);

function cell(value, cellMax) {
  if (value instanceof Uint8Array) return `<blob ${value.byteLength} bytes>`;
  if (typeof value === "bigint") return Number.isSafeInteger(Number(value)) ? Number(value) : String(value);
  if (typeof value === "string" && value.length > cellMax) return `${value.slice(0, cellMax)}…`;
  return value;
}

function run({ file, sql, cap, cellMax }) {
  const { DatabaseSync } = process.getBuiltinModule("node:sqlite");
  const db = new DatabaseSync(file, { readOnly: true });
  try {
    db.exec("PRAGMA busy_timeout = 1000");
    // Node >= 24.10 (setAuthorizer): deny every action a query does not need, on top of the read-only
    // connection and the parent's statement check.
    if (typeof db.setAuthorizer === "function") {
      db.setAuthorizer((action, arg1, arg2) => (ALLOWED_ACTIONS.has(action)
        || (action === SQLITE_PRAGMA && READ_PRAGMAS.has(String(arg1).toLowerCase()) && arg2 === null) ? SQLITE_OK : SQLITE_DENY));
    }
    const rows = [];
    let truncated = false;
    let columns = [];
    for (const row of db.prepare(sql).iterate()) {
      if (rows.length === cap) {
        truncated = true;
        break;
      }
      if (!columns.length) columns = Object.keys(row);
      rows.push(Object.fromEntries(Object.entries(row).map(([k, v]) => [k, cell(v, cellMax)])));
    }
    return { columns, rows, truncated };
  } finally {
    db.close();
  }
}

let out;
try {
  out = run(JSON.parse(fs.readFileSync(0, "utf8")));
} catch (err) {
  out = { error: err instanceof Error ? err.message : String(err) };
}
process.stdout.write(JSON.stringify(out));
