/**
 * Execution ledger (CONTEXT_LEDGER) and test/build log reduction (CONTEXT_LOG_REDUCE_OVER_CHARS) for the OpenCode 2 /
 * Pi trim: ports of proxy/smarter_compact/ledger.py, compress.reduce_log and the read-only rule of evict.py, same bytes.
 * Regexes use explicit character classes and match "\n"-split lines, as the Python side does.
 */
import crypto from "node:crypto";

const SURROGATE_RE = /[\uD800-\uDFFF]/;
const cpLen = (s) => (SURROGATE_RE.test(s) ? Array.from(s).length : s.length);
const cpSlice = (s, start, end) => (SURROGATE_RE.test(s) ? Array.from(s).slice(start, end).join("") : s.slice(start, end));
const sha16 = (text) => crypto.createHash("sha256").update(text, "utf8").digest("hex").slice(0, 16);

// Same as compress.MARKER_PREFIX in the proxy and MARKER_PREFIX in context-trim.mjs.
const MARKER_PREFIX = "[smarter-compact: tool result compressed,";
/** Start of a repeated read's pointer recorded before ledger reuse became the dedup pointer; never masked. Same as compress.REUSE_PREFIX. */
export const REUSE_PREFIX = "[smarter-compact: this read repeats an earlier one still shown above";
// compress.py LOG_*: LogDx-CI's grep reducer (arXiv 2605.28876, Table 2): failure pattern, 3 before / 8 after.
const LOG_FAILURE_RE = /error|failed|failure|traceback|exception|assert|panic|exit code|##\[error\]/i;
const LOG_BEFORE = 3;
const LOG_AFTER = 8;
export const LOG_TAIL_LINES = 40;
const LOG_MATCH_MAX_CHARS = 8000;
const LOG_SIGNATURE_RE = new RegExp("^=+ (?:test session starts|FAILURES|ERRORS|short test summary info) =+"
  + "|^(?:FAILED|PASSED|ERROR) [^ ]+::|[0-9]+ (?:passed|failed)(?:,| in )|^Ran [0-9]+ tests? in "
  + "|^(?:Tests|Test Suites): +[0-9]|^(?:---|===) (?:FAIL|RUN|PASS)|^(?:ok|FAIL)[ \\t]+[^ \\t]+[ \\t]+[0-9.]+s"
  + "|^test result: (?:ok|FAILED)\\.|^[ \\t]*Compiling [^ ]+ v[0-9]|^error\\[E[0-9]{4}\\]|^make(?:\\[[0-9]+\\])?: "
  + "|^[^ \\t:]+:[0-9]+:[0-9]+: (?:fatal )?(?:error|warning):|error TS[0-9]+:|^\\[(?:INFO|ERROR)\\] (?:BUILD|Tests run:)"
  + "|^BUILD (?:SUCCESSFUL|FAILED) in |^> Task :|^npm ERR!");
// ledger.py constants.
export const LEDGER_MAX_CHARS = 1500;
const LEDGER_MAX_MODIFIED = 12;
const LEDGER_MAX_RANGES = 4;
export const LEDGER_MAX = 512;
export const LEDGER_HEAD = "[smarter-compact ledger, from your tool calls: current = unchanged since you read it, stale = may have "
  + "changed since]";
const READ_TOOLS = new Set(["read", "read_file", "view"]);
const LEDGER_WRITE_TOOLS = new Set(["edit", "write", "multiedit", "notebookedit", "write_file", "create_file"]);
const PATCH_TOOLS = new Set(["apply_patch", "patch"]);
const SHELL_TOOLS = new Set(["bash", "shell", "exec_command", "local_shell", "run_terminal_cmd"]);
const LEDGER_PATH_KEYS = ["file_path", "filePath", "notebook_path", "path", "file"];
const PATCH_FILE_RE = /^\*\*\* (?:(?:Update|Add|Delete) File|Move to): ([\s\S]+)$/;
const TEST_RE = new RegExp("(?:^|[ \\t\\n;&|(/])(?:py\\.?test|tox|nox|ctest|cargo test|go test|(?:npm|yarn|pnpm)(?: run)? test"
  + "|node --test|npx (?:jest|vitest)|jest|vitest|rspec|dotnet test|python[0-9.]* -m (?:pytest|unittest))(?=[ \\t\\n;]|$)");
const TRUNCATED_RE = /truncated|output too large|<persisted-output>|exceeds maximum|has more lines/i;
const NUMBERED_RE = /^ *([0-9]+)(?:\t|→)/;
const DIGITS_RE = /^[0-9]+$/;
const SED_RANGE_RE = /^([0-9]+)(?:,([0-9]+))?p$/;
const HEAD_N_RE = /^-n?([0-9]+)$/;
// Shell operators (shlex punctuation runs) whose next word is a file the command writes, or a word that is no file.
const WRITE_OPS = new Set([">", ">>", ">|", "&>", "&>>"]);
const SKIP_OPS = new Set([">&", "<", "<<", "<<<", "<&", "<>", "&>&"]);
// A recorded written path: plain path characters only, so code, quoted text and globs never count as a file.
const PATH_RE = /^[A-Za-z0-9_./~+@%-]+$/;
const ALNUM_RE = /[A-Za-z0-9]/;
const ASSIGN_RE = /^[A-Za-z_][A-Za-z0-9_]*=/;
const SHELL_META = "|;&><`$\n";
// evict.py read_only.
const READ_ONLY_COMMANDS = new Set(["cat", "head", "tail", "less", "more", "wc", "ls", "find", "tree", "stat", "file",
  "grep", "egrep", "fgrep", "rg", "ag", "pwd", "which"]);
const READ_ONLY_GIT = new Set(["status", "diff", "log", "show", "branch"]);
const GIT_BRANCH_LIST = new Set(["-a", "-r", "-v", "-vv", "--all", "--remotes", "--verbose", "--list", "--show-current"]);
const FIND_ACTIONS = new Set(["-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"]);
const CHAIN = new Set(["|", "&&", "||", ";"]);
const PUNCT = "|&;()<>";
const SED_PRINT_RE = /^(?:(?:\d+|\$|\/[^/]*\/)(?:,(?:\d+|\$|\/[^/]*\/))?)?p\n?$/;

/**
 * Python shlex in posix mode with whitespace_split: the words of s, punctuation runs as their own tokens when given;
 * throws on an unclosed quote or a trailing escape, as shlex raises ValueError.
 */
export function shlexTokens(s, { punctuation = "", commenters = "" } = {}) {
  const ws = " \t\r\n";
  const out = [];
  const pushback = [];
  let i = 0;
  const next = () => (pushback.length ? pushback.pop() : i < s.length ? s[i++] : "");
  const readline = () => { while (i < s.length) if (s[i++] === "\n") break; };
  const isQuote = (c) => c === "'" || c === '"';
  for (;;) {
    let state = " ";
    let token = "";
    let quoted = false;
    let escaped = " ";
    for (;;) {
      const c = next();
      if (state === " ") {
        if (!c) break;
        if (ws.includes(c)) { if (token || quoted) break; continue; }
        if (commenters.includes(c)) { readline(); continue; }
        if (c === "\\") { escaped = "a"; state = c; continue; }
        if (punctuation.includes(c)) { token = c; state = "c"; continue; }
        if (isQuote(c)) { state = c; continue; }
        token = c;
        state = "a";
        continue;
      }
      if (isQuote(state)) {
        quoted = true;
        if (!c) throw new Error("No closing quotation");
        if (c === state) state = "a";
        else if (c === "\\" && state === '"') { escaped = state; state = c; } else token += c;
        continue;
      }
      if (state === "\\") {
        if (!c) throw new Error("No escaped character");
        if (isQuote(escaped) && c !== state && c !== escaped) token += state;
        token += c;
        state = escaped;
        continue;
      }
      if (!c) break;
      if (ws.includes(c)) { state = " "; if (token || quoted) break; continue; }
      if (commenters.includes(c)) { readline(); state = " "; if (token || quoted) break; continue; }
      if (state === "c") {
        if (punctuation.includes(c)) { token += c; continue; }
        if (!ws.includes(c)) pushback.push(c);
        break;
      }
      if (isQuote(c)) { state = c; continue; }
      if (c === "\\") { escaped = "a"; state = c; continue; }
      if (!punctuation.includes(c)) { token += c; continue; }
      pushback.push(c);
      state = " ";
      if (token || quoted) break;
    }
    if (!quoted && token === "") return out;
    out.push(token);
  }
}

const shortFlag = (arg, letter) => arg.startsWith("-") && !arg.startsWith("--") && arg.slice(1).includes(letter);

function sedReadOnly(args) {
  if (args.some((a) => a.startsWith("--in-place") || shortFlag(a, "i"))) return false;
  if (!args.some((a) => a === "--quiet" || a === "--silent" || shortFlag(a, "n"))) return false;
  const scripts = args.filter((a) => !a.startsWith("-"));
  return scripts.length > 0 && SED_PRINT_RE.test(scripts[0]);
}

function gitReadOnly(args) {
  while (args.length && args[0].startsWith("-")) args = ["-C", "-c"].includes(args[0]) ? args.slice(2) : args.slice(1);
  if (!args.length || !READ_ONLY_GIT.has(args[0]) || args.some((a) => a.startsWith("--output"))) return false;
  return args[0] !== "branch" || args.slice(1).every((a) => GIT_BRANCH_LIST.has(a));
}

function simpleReadOnly(words) {
  const [name, ...args] = words;
  switch (name) {
    case "sed": return sedReadOnly(args);
    case "git": return gitReadOnly(args);
    case "env": return !args.length;
    case "find": return !args.some((a) => FIND_ACTIONS.has(a));
    default: return READ_ONLY_COMMANDS.has(name);
  }
}

/** Whether a shell command only reads (evict.read_only): allowlisted commands joined by |, &&, || or ;. */
export function readOnly(command) {
  if (command.includes("$(") || command.includes("`") || command.includes(">")) return false;
  let tokens;
  try { tokens = shlexTokens(command, { punctuation: "();<>|&", commenters: "#" }); } catch { return false; }
  const simple = [];
  let words = [];
  for (const token of tokens) {
    if (CHAIN.has(token)) {
      if (!words.length) return false;
      simple.push(words);
      words = [];
    } else if ([...token].every((ch) => PUNCT.includes(ch))) return false;
    else words.push(token);
  }
  if (words.length) simple.push(words);
  return simple.length > 0 && simple.every(simpleReadOnly);
}

/** Whether a line of text looks like test or build output (compress.is_log). */
export function isLog(text) {
  return text.split("\n").some((line) => LOG_SIGNATURE_RE.test(line));
}

function folded(lines) {
  const out = [];
  for (let k = 0; k < lines.length;) {
    let n = 1;
    while (k + n < lines.length && lines[k + n] === lines[k]) n += 1;
    out.push(n > 1 ? `${lines[k]} ×${n}` : lines[k]);
    k += n;
  }
  return out;
}

/** `{reduced, id}` for test/build output over overChars that shrinks, else null; same bytes as compress.reduce_log. */
export function reduceLog(text, overChars) {
  if (!(overChars > 0) || cpLen(text) <= overChars || text.startsWith(MARKER_PREFIX) || !isLog(text)) return null;
  const lines = text.split("\n");
  const tail = Math.max(0, lines.length - LOG_TAIL_LINES);
  const windows = [];
  for (let k = 0; k < tail; k++) {
    if (!LOG_FAILURE_RE.test(lines[k])) continue;
    const lo = Math.max(0, k - LOG_BEFORE);
    const hi = Math.min(tail, k + LOG_AFTER + 1);
    if (windows.length && lo <= windows[windows.length - 1][1]) {
      windows[windows.length - 1][1] = Math.max(windows[windows.length - 1][1], hi);
    } else windows.push([lo, hi]);
  }
  const out = [];
  const seen = new Set();
  let used = 0;
  let shownTo = 0;
  let omitted = 0;
  for (const [lo, hi] of windows) {
    const body = folded(lines.slice(lo, hi)).join("\n");
    if (seen.has(body)) continue;
    if (used + cpLen(body) > LOG_MATCH_MAX_CHARS) { omitted += 1; continue; }
    if (lo > shownTo) out.push(`… ${lo - shownTo} lines …`);
    out.push(body);
    seen.add(body);
    used += cpLen(body);
    shownTo = hi;
  }
  if (omitted) out.push(`… ${omitted} more failure windows not shown …`);
  if (tail > shownTo) out.push(`… ${tail - shownTo} lines …`);
  out.push(...folded(lines.slice(tail)));
  const id = sha16(text);
  const reduced = `${MARKER_PREFIX} test/build log of ${lines.length} lines, ${cpLen(text)} chars, reduced to its failure lines `
    + `and last ${LOG_TAIL_LINES} lines — smart_compact_get(id="${id}") returns the full text]\n${out.join("\n")}`;
  return cpLen(reduced) < cpLen(text) ? { reduced, id } : null;
}

/** path with leading "./" and doubled slashes removed (ledger.norm_path). */
export function normPath(path) {
  let out = path.replace(/\/{2,}/g, "/");
  while (out.startsWith("./")) out = out.slice(2);
  return out;
}

const intOf = (v) => (Number.isInteger(v) ? v : typeof v === "string" && DIGITS_RE.test(v) ? Number.parseInt(v, 10) : null);

function shellQuote(s) {
  if (!s) return "''";
  return /[^\w@%+=:,./-]/.test(s) ? `'${s.replaceAll("'", "'\"'\"'")}'` : s;
}

/** A shell call's command string (ledger.shell_command). */
export function shellCommand(args) {
  const value = "command" in args ? args.command : args.cmd;
  if (Array.isArray(value) && value.every((v) => typeof v === "string")) {
    if (value.length === 3 && ["bash", "sh", "zsh"].includes(value[0]) && ["-c", "-lc"].includes(value[1])) return value[2];
    return value.map(shellQuote).join(" ");
  }
  return typeof value === "string" ? value : null;
}

function words(command) {
  try { return shlexTokens(command); } catch { return null; }
}

function shellReads(command) {
  if ([...SHELL_META].some((ch) => command.includes(ch))) return [];
  const w = words(command);
  if (!w || !w.length) return [];
  const [name, ...args] = w;
  if (name === "cat") return args.filter((a) => !a.startsWith("-")).map((a) => [normPath(a), 1, null, "cat"]);
  if (name === "sed") {
    const flags = args.filter((a) => a.startsWith("-"));
    const rest = args.filter((a) => !a.startsWith("-"));
    const m = rest.length ? SED_RANGE_RE.exec(rest[0]) : null;
    if (flags.length !== 1 || flags[0] !== "-n" || !m || rest.length !== 2) return [];
    const lo = Number.parseInt(m[1], 10);
    return [[normPath(rest[1]), lo, m[2] ? Number.parseInt(m[2], 10) : lo, "sed"]];
  }
  if (name !== "head" && name !== "tail") return [];
  let n = 10;
  const paths = [];
  for (let k = 0; k < args.length; k++) {
    const a = args[k];
    if (a === "-n" && k + 1 < args.length && DIGITS_RE.test(args[k + 1])) { n = Number.parseInt(args[k + 1], 10); k += 1; continue; }
    const m = HEAD_N_RE.exec(a);
    if (m) n = Number.parseInt(m[1], 10);
    else if (a.startsWith("-")) return [];
    else paths.push(normPath(a));
  }
  if (paths.length !== 1) return [];
  return name === "head" ? [[paths[0], 1, n, "head"]] : [[paths[0], null, n, "tail"]];
}

const patchFiles = (text) => text.split("\n").map((line) => PATCH_FILE_RE.exec(line)).filter(Boolean)
  .map((m) => normPath(m[1].replace(/^[ \t\r]+|[ \t\r]+$/g, "")));

/** command with heredoc bodies, comments and line continuations removed and each unquoted newline a ";" (ledger._shell_text). */
function shellText(command) {
  const out = [];
  let pending = [];
  let i = 0;
  let quote = null;
  const n = command.length;
  while (i < n) {
    const c = command[i];
    if (quote === "'") {
      out.push(c);
      if (c === "'") quote = null;
      i += 1;
    } else if (quote === '"') {
      out.push(c === "\\" ? command.slice(i, i + 2) : c);
      if (c === '"') quote = null;
      i += c === "\\" ? 2 : 1;
    } else if (c === "\\") {
      out.push(command.slice(i + 1, i + 2) === "\n" ? "" : command.slice(i, i + 2));
      i += 2;
    } else if (c === "'" || c === '"') {
      out.push(c);
      quote = c;
      i += 1;
    } else if (c === "#" && (i === 0 || " \t\n;&|()".includes(command[i - 1]))) {
      while (i < n && command[i] !== "\n") i += 1;
    } else if (c === "\n") {
      out.push(" ; ");
      i += 1;
      for (const [delim, dash] of pending) {
        while (i < n) {
          let end = command.indexOf("\n", i);
          if (end < 0) end = n;
          const line = command.slice(i, end);
          i = end + 1;
          if ((dash ? line.replace(/^\t+/, "") : line) === delim) break;
        }
      }
      pending = [];
    } else if (command.startsWith("<<", i) && !command.startsWith("<<<", i)) {
      let j = i + 2;
      const dash = command[j] === "-";
      if (dash) j += 1;
      while (j < n && " \t".includes(command[j])) j += 1;
      let k = j;
      while (k < n && !" \t\n;&|()<>".includes(command[k])) k += 1;
      const delim = command.slice(j, k).replace(/["'\\]/g, "");
      if (delim) pending.push([delim, dash]);
      out.push(`<< ${delim} `);
      i = k;
    } else {
      out.push(c);
      i += 1;
    }
  }
  return out.join("");
}

/** Files a simple command's words name as written: tee files, sed -i files, the cp/mv destination (ledger._argv_writes). */
function argvWrites(words) {
  let w = words;
  while (w.length && ASSIGN_RE.test(w[0])) w = w.slice(1);
  if (!w.length) return [];
  const [name, ...args] = w;
  const plain = args.filter((a) => !a.startsWith("-"));
  if (name === "tee") return plain;
  if (name === "sed" && args.some((a) => a.startsWith("-i") || a.startsWith("--in-place"))) return plain.slice(1);
  if (name === "cp" || name === "mv") {
    for (let k = 0; k < args.length; k++) {
      if (args[k] === "-t" && k + 1 < args.length) return [args[k + 1]];
      if (args[k].startsWith("--target-directory=")) return [args[k].slice(args[k].indexOf("=") + 1)];
    }
    return plain.length >= 2 ? plain.slice(-1) : [];
  }
  return [];
}

const plausible = (p) => PATH_RE.test(p) && !p.startsWith("-") && !p.startsWith("/dev/") && ALNUM_RE.test(p);
const isOp = (token) => [...token].every((ch) => PUNCT.includes(ch));

/** Paths a shell command visibly writes, after shell-quote parsing (ledger._shell_writes). */
function shellWrites(command) {
  let tokens;
  try { tokens = shlexTokens(shellText(command), { punctuation: PUNCT }); } catch { tokens = []; }
  const out = [];
  let w = [];
  for (let k = 0; k <= tokens.length;) {
    const token = k < tokens.length ? tokens[k] : ";";
    const op = isOp(token);
    if (op && (WRITE_OPS.has(token) || SKIP_OPS.has(token))) {
      if (w.length && DIGITS_RE.test(w.at(-1))) w.pop(); // the fd of 2> or 2>&1
      const nxt = k + 1 < tokens.length ? tokens[k + 1] : "";
      if (WRITE_OPS.has(token) && !isOp(nxt)) out.push(nxt);
      k += 2;
      continue;
    }
    if (op) {
      out.push(...argvWrites(w));
      w = [];
    } else w.push(token);
    k += 1;
  }
  return [...out.filter(plausible).map(normPath), ...patchFiles(command)];
}

/** What one tool call does (ledger.effects). */
export function effects(name, args) {
  const tool = typeof name === "string" ? name.toLowerCase() : "";
  const a = args && typeof args === "object" && !Array.isArray(args) ? args : {};
  const path = LEDGER_PATH_KEYS.map((k) => a[k]).find((v) => typeof v === "string" && v) ?? null;
  if (READ_TOOLS.has(tool)) {
    if (!path) return [];
    const offset = intOf(a.offset);
    const limit = intOf(a.limit);
    if (offset === null && limit === null) return [["read", normPath(path), 1, null, tool]];
    const lo = Math.max(offset || 1, 1);
    return [["read", normPath(path), lo, limit ? lo + limit - 1 : null, tool]];
  }
  if (LEDGER_WRITE_TOOLS.has(tool)) return path ? [["edit", normPath(path)]] : [];
  if (PATCH_TOOLS.has(tool)) return Object.values(a).filter((v) => typeof v === "string").flatMap(patchFiles).map((p) => ["edit", p]);
  if (!SHELL_TOOLS.has(tool)) return [];
  const command = shellCommand(a);
  if (!command) return [];
  if (readOnly(command)) return shellReads(command).map(([p, lo, hi, kind]) => ["read", p, lo, hi, kind]);
  const out = shellWrites(command).map((p) => ["edit", p]);
  out.push(TEST_RE.test(command) ? ["test", command.split(/\s+/).filter(Boolean).join(" ")] : ["global"]);
  return out;
}

function rangeLabel(lo, hi, kind) {
  if (kind === "tail") return `last ${hi} lines`;
  if (lo === 1 && hi === null) return "whole";
  return hi !== null ? `lines ${lo}-${hi}` : `lines ${lo}-`;
}

function shownRange(text, lo, hi) {
  const nums = text.split("\n").map((line) => NUMBERED_RE.exec(line)).filter(Boolean).map((m) => Number.parseInt(m[1], 10));
  return nums.length ? [nums[0], nums[nums.length - 1]] : [lo, hi];
}

function covers(o, r) {
  if (r.kind === "tail" || o.kind === "tail" || o.truncated) return false;
  if (r.hi === null) return (o.hi === null && o.lo <= r.lo) || (r.whole && o.whole && o.kind === r.kind);
  return o.lo !== null && o.lo <= r.lo && (o.hi === null || r.hi <= o.hi);
}

/** The ledger of steps [[j, rid, name, args, text, isError]] in result order and the new steps' repeats (ledger.walk). */
export function walk(steps, newFrom) {
  const files = new Map();
  const reads = new Map();
  const modified = new Map();
  const tests = new Map();
  const repeats = [];
  let total = 0;
  let glob = 0;
  for (const [j, rid, name, args, text, isError] of steps) {
    for (const effect of effects(name, args)) {
      if (effect[0] === "read") {
        const [, path, lo, hi, kind] = effect;
        if (isError) continue;
        const t = text || "";
        const shown = READ_TOOLS.has(kind) ? shownRange(t, lo, hi) : [lo, hi];
        const r = { j, rid, kind, lo: kind !== "tail" ? shown[0] : lo, hi: kind !== "tail" ? shown[1] : hi,
          whole: lo === 1 && hi === null, label: rangeLabel(lo, hi, kind), file: files.get(path) ?? 0, global: glob,
          truncated: TRUNCATED_RE.test(t) || t.startsWith(MARKER_PREFIX) };
        if (j >= newFrom) {
          const found = [...(reads.get(path) ?? [])].reverse()
            .filter((o) => o.file === r.file && o.global === glob && covers(o, r)).map((o) => [o.j, o.rid]);
          if (found.length) repeats.push({ rid, j, path, label: r.label, covers: found });
        }
        const list = reads.get(path) ?? [];
        list.push(r);
        reads.delete(path);
        reads.set(path, list);
      } else if (effect[0] === "edit") {
        const path = effect[1];
        files.set(path, (files.get(path) ?? 0) + 1);
        modified.delete(path);
        modified.set(path, files.get(path));
        total += 1;
      } else if (effect[0] === "global") {
        glob += 1;
        total += 1;
      } else if (effect[0] === "test") {
        const command = effect[1];
        if (j >= newFrom && tests.get(command) === total && !isError) repeats.push({ rid, j, test: command });
        tests.set(command, total);
      }
    }
  }
  return { state: { reads, modified, files, global: glob }, repeats };
}

/** The ledger block for state, at most LEDGER_MAX_CHARS less reserve; "" when it lists nothing (ledger.block). */
export function ledgerBlock(state, reserve = 0) {
  const { reads, modified } = state;
  if (!reads.size && !modified.size) return "";
  const lines = [LEDGER_HEAD];
  if (modified.size) {
    const names = [...modified].slice(-LEDGER_MAX_MODIFIED).map(([p, n]) => (n > 1 ? `${p} ×${n}` : p));
    lines.push(`Modified (latest last): ${names.join(", ")}`);
  }
  const entries = [];
  for (const [path, obs] of reads) {
    const current = state.files.get(path) ?? 0;
    const labels = new Map();
    for (const o of obs) {
      labels.delete(o.label);
      labels.set(o.label, o.file === current && o.global === state.global ? "current" : "stale");
    }
    entries.push(`- ${path}: ${[...labels].slice(-LEDGER_MAX_RANGES).map(([l, s]) => `${l} ${s}`).join("; ")}`);
  }
  if (entries.length) lines.push("Read (latest last):");
  const head = lines.join("\n");
  const cap = LEDGER_MAX_CHARS - reserve;
  const kept = [];
  let size = cpLen(head);
  for (const entry of [...entries].reverse()) {
    if (size + 1 + cpLen(entry) > cap - 40) break;
    kept.push(entry);
    size += 1 + cpLen(entry);
  }
  if (kept.length < entries.length) kept.push(`- (${entries.length - kept.length} earlier files not listed)`);
  return cpSlice([head, ...kept.reverse()].join("\n"), 0, cap);
}

/** A ledger block's identity (ledger.core_id). */
export const coreId = sha16;

/** One line for a repeat the view cannot point at, or a test rerun (ledger.nudge_line). */
export function nudgeLine(repeat) {
  if ("test" in repeat) return `- \`${cpSlice(repeat.test, 0, 200)}\` already ran with no file change since; rerun tests only for new evidence.`;
  return `- ${repeat.path} (${repeat.label}) was read before and is unchanged since; that earlier output is no longer shown in full.`;
}
