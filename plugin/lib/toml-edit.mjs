/**
 * Text-preserving TOML edits: locate statements (table headers and key/value pairs) with a
 * small lexer that understands strings, multi-line strings, comments and multi-line arrays,
 * so an edit removes exactly one entry's text and leaves every other byte alone.
 */

const BARE_KEY = /[A-Za-z0-9_-]/;

/** Offset just past the line break at or after `i` (or the end of the text). */
function lineEnd(text, i) {
  const nl = text.indexOf("\n", i);
  return nl < 0 ? text.length : nl + 1;
}

/** Offset after the string starting at `i` (any of the four TOML string forms). */
function skipString(text, i) {
  const quote = text[i];
  const triple = text.startsWith(quote.repeat(3), i);
  if (triple) {
    let j = i + 3;
    while (j < text.length) {
      if (quote === '"' && text[j] === "\\") { j += 2; continue; }
      if (text.startsWith(quote.repeat(3), j)) {
        j += 3;
        // Up to two quotes may directly precede the closing delimiter.
        for (let k = 0; k < 2 && text[j] === quote; k++) j++;
        return j;
      }
      j++;
    }
    throw new SyntaxError("unterminated multi-line string");
  }
  let j = i + 1;
  while (j < text.length && text[j] !== "\n") {
    if (quote === '"' && text[j] === "\\") { j += 2; continue; }
    if (text[j] === quote) return j + 1;
    j++;
  }
  throw new SyntaxError("unterminated string");
}

/** Parse a dotted key at `i`; returns the segments and the offset after the key. */
function parseKey(text, i) {
  const parts = [];
  for (;;) {
    while (text[i] === " " || text[i] === "\t") i++;
    if (text[i] === '"' || text[i] === "'") {
      const end = skipString(text, i);
      const raw = text.slice(i, end);
      if (raw[0] === "'") parts.push(raw.slice(1, -1));
      else {
        try { parts.push(JSON.parse(raw)); } catch { parts.push(raw.slice(1, -1)); }
      }
      i = end;
    } else {
      const start = i;
      while (i < text.length && BARE_KEY.test(text[i])) i++;
      if (i === start) throw new SyntaxError(`bad key at offset ${start}`);
      parts.push(text.slice(start, i));
    }
    while (text[i] === " " || text[i] === "\t") i++;
    if (text[i] !== ".") return { parts, end: i };
    i++;
  }
}

/** Offset after a value starting at `i`, through the end of its last line (comment included). */
function skipValue(text, i) {
  let depth = 0;
  while (i < text.length) {
    const c = text[i];
    if (c === '"' || c === "'") { i = skipString(text, i); continue; }
    if (c === "#") { i = text.indexOf("\n", i); if (i < 0) return text.length; continue; }
    if (c === "[" || c === "{") depth++;
    else if (c === "]" || c === "}") depth--;
    else if (c === "\n" && depth <= 0) return i + 1;
    i++;
  }
  return text.length;
}

/**
 * Statements of a TOML document in order: `{ kind: "table" | "array-table" | "kv", path, start, end }`.
 * `path` is the full key path (a key/value pair includes its table's path); `start` is the
 * offset of the statement's line and `end` the offset after its last line.
 * @param {string} text
 */
export function tomlStatements(text) {
  const out = [];
  let table = [];
  let i = 0;
  while (i < text.length) {
    const start = i;
    while (text[i] === " " || text[i] === "\t" || text[i] === "\r") i++;
    if (i >= text.length) break;
    if (text[i] === "\n") { i++; continue; }
    if (text[i] === "#") { i = lineEnd(text, i); continue; }
    if (text[i] === "[") {
      const array = text[i + 1] === "[";
      const key = parseKey(text, i + (array ? 2 : 1));
      table = key.parts;
      out.push({ kind: array ? "array-table" : "table", path: table, start, end: lineEnd(text, key.end) });
      i = lineEnd(text, key.end);
      continue;
    }
    const key = parseKey(text, i);
    if (text[key.end] !== "=") throw new SyntaxError(`expected "=" at offset ${key.end}`);
    const end = skipValue(text, key.end + 1);
    out.push({ kind: "kv", path: [...table, ...key.parts], start, end });
    i = end;
  }
  return out;
}

const startsWith = (pathParts, prefix) => prefix.every((p, k) => pathParts[k] === p);

/**
 * Text spans to delete so no statement at or under `prefix` remains: whole tables (header through
 * the table's last key/value pair, so comments that follow it stay) and single key/value pairs.
 * @param {string} text
 * @param {string[]} prefix
 * @returns {{ start: number, end: number }[]}
 */
export function tomlSpansUnder(text, prefix) {
  const statements = tomlStatements(text);
  const spans = [];
  for (let s = 0; s < statements.length; s++) {
    const st = statements[s];
    if (st.kind === "kv") {
      if (startsWith(st.path, prefix)) spans.push({ start: st.start, end: st.end });
      continue;
    }
    if (!startsWith(st.path, prefix)) continue;
    let end = st.end;
    let k = s + 1;
    for (; k < statements.length && statements[k].kind === "kv"; k++) end = statements[k].end;
    spans.push({ start: st.start, end });
    s = k - 1;
  }
  return spans;
}

/** Remove `spans` from `text`, also dropping blank lines a removal would leave doubled. */
export function removeSpans(text, spans) {
  let out = text;
  for (const { start, end } of [...spans].sort((a, b) => b.start - a.start)) {
    let stop = end;
    const before = out.slice(0, start);
    if (start === 0 || /\n[ \t\r]*\n$/.test(before)) {
      const m = /^(?:[ \t\r]*\n)+/.exec(out.slice(stop));
      if (m) stop += m[0].length;
    }
    out = out.slice(0, start) + out.slice(stop);
  }
  return out;
}

/** Remove every statement at or under `prefix` (e.g. `["mcp_servers", "hindsight"]`). */
export function removeTomlPath(text, prefix) {
  return removeSpans(text, tomlSpansUnder(text, prefix));
}
