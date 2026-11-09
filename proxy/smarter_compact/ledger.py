"""Execution ledger (CONTEXT_LEDGER, docs/clearing.md "Ledger"): files read (with line ranges) and changed, from tool
calls, after the runtime state view of arXiv 2608.00808 §3.3-3.4.

Pure functions of (name, args, result text) triples, so every adapter and the OpenCode 2 / Pi plugin
(plugin/lib/context-trim.mjs) derive the same ledger. An observation is current while neither its file's edit counter
nor the global counter (a shell command that may change any file) moved since it was made.
"""

import hashlib
import re
import shlex

from .compress import is_compressed
from .evict import PATH_KEYS, WRITE_TOOLS, read_only

# The ledger block's cap, and the paths and ranges it lists at most.
LEDGER_MAX_CHARS = 1500
LEDGER_MAX_MODIFIED = 12
LEDGER_MAX_RANGES = 4
# Ledger blocks recorded per session (trim state "ledger"), oldest dropped first.
LEDGER_MAX = 512
LEDGER_HEAD = ("[smarter-compact ledger, from your tool calls: current = unchanged since you read it, stale = may have "
               "changed since]")
READ_TOOLS = ("read", "read_file", "view")
LEDGER_WRITE_TOOLS = WRITE_TOOLS + ("write_file", "create_file")
PATCH_TOOLS = ("apply_patch", "patch")
SHELL_TOOLS = ("bash", "shell", "exec_command", "local_shell", "run_terminal_cmd")
LEDGER_PATH_KEYS = PATH_KEYS + ("file",)
# Regexes use explicit character classes and match "\n"-split lines, so the plugin's JavaScript port agrees.
# An apply_patch file header line.
_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:(?:Update|Add|Delete) File|Move to): (.+)$")
# A command that runs tests: it changes no source file (no global-counter move), and a rerun with no edit since is
# nudged (§3.4: "A test command repeated without an intervening edit receives Nudge").
TEST_RE = re.compile(
    r"(?:^|[ \t\n;&|(/])(?:py\.?test|tox|nox|ctest|cargo test|go test|(?:npm|yarn|pnpm)(?: run)? test|node --test"
    r"|npx (?:jest|vitest)|jest|vitest|rspec|dotnet test|python[0-9.]* -m (?:pytest|unittest))(?=[ \t\n;]|$)")
# A result cut short by the harness or by compression: it does not show every line it covers.
TRUNCATED_RE = re.compile(r"truncated|output too large|<persisted-output>|exceeds maximum|has more lines",
                          re.IGNORECASE)
# A Read result's numbered line (cat -n style: "   12\t..." or "12→...").
_NUMBERED_RE = re.compile(r"^ *([0-9]+)(?:\t|→)")
_DIGITS_RE = re.compile(r"[0-9]+")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")
_SED_RANGE_RE = re.compile(r"^([0-9]+)(?:,([0-9]+))?p$")
_HEAD_N_RE = re.compile(r"^-n?([0-9]+)$")
_SHELL_META = "|;&><`$\n"
# Shell operators (shlex punctuation runs) whose next word is a file the command writes, or a word that is no file.
_WRITE_OPS = frozenset((">", ">>", ">|", "&>", "&>>"))
_SKIP_OPS = frozenset((">&", "<", "<<", "<<<", "<&", "<>", "&>&"))
# A recorded written path: plain path characters only, so code, quoted text and globs never count as a file.
_PATH_RE = re.compile(r"^[A-Za-z0-9_./~+@%-]+$")
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def norm_path(path):
    """path with leading "./" and doubled slashes removed."""
    path = re.sub(r"/{2,}", "/", path)
    while path.startswith("./"):
        path = path[2:]
    return path


def _int(value):
    if type(value) is int:
        return value
    return int(value) if isinstance(value, str) and _DIGITS_RE.fullmatch(value) else None


def shell_command(args):
    """A shell call's command string: `command` or `cmd`, a list joined (its script for `bash -lc <script>`)."""
    value = args.get("command", args.get("cmd"))
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        if len(value) == 3 and value[0] in ("bash", "sh", "zsh") and value[1] in ("-c", "-lc"):
            return value[2]
        return shlex.join(value)
    return value if isinstance(value, str) else None


def _words(command):
    try:
        return shlex.split(command)
    except ValueError:
        return None


def _shell_reads(command):
    """[(path, lo, hi, kind)] a single read-only cat / sed -n / head / tail prints; [] for anything else."""
    if any(ch in command for ch in _SHELL_META):
        return []
    words = _words(command)
    if not words:
        return []
    name, args = words[0], words[1:]
    match name:
        case "cat":
            return [(norm_path(w), 1, None, "cat") for w in args if not w.startswith("-")]
        case "sed":
            flags = [a for a in args if a.startswith("-")]
            rest = [a for a in args if not a.startswith("-")]
            m = _SED_RANGE_RE.match(rest[0]) if rest else None
            if flags != ["-n"] or m is None or len(rest) != 2:
                return []
            lo = int(m.group(1))
            return [(norm_path(rest[1]), lo, int(m.group(2) or lo), "sed")]
        case "head" | "tail":
            n, paths, k = 10, [], 0
            while k < len(args):
                a = args[k]
                if a == "-n" and k + 1 < len(args) and _DIGITS_RE.fullmatch(args[k + 1]):
                    n, k = int(args[k + 1]), k + 2
                    continue
                m = _HEAD_N_RE.match(a)
                if m:
                    n = int(m.group(1))
                elif a.startswith("-"):
                    return []
                else:
                    paths.append(norm_path(a))
                k += 1
            if len(paths) != 1:
                return []
            return [(paths[0], 1, n, "head")] if name == "head" else [(paths[0], None, n, "tail")]
    return []


def _shell_text(command):
    """command with heredoc bodies, comments and line continuations removed and each unquoted newline a ";": what
    shlex can split into simple commands. Quotes are tracked, so text inside a quoted string never changes."""
    out, pending, i, n, quote = [], [], 0, len(command), None
    while i < n:
        c = command[i]
        if quote == "'":
            out.append(c)
            quote = None if c == "'" else quote
            i += 1
        elif quote == '"':
            out.append(command[i:i + 2] if c == "\\" else c)
            quote = None if c == '"' else quote
            i += 2 if c == "\\" else 1
        elif c == "\\":
            out.append("" if command[i + 1:i + 2] == "\n" else command[i:i + 2])
            i += 2
        elif c in "'\"":
            out.append(c)
            quote = c
            i += 1
        elif c == "#" and (i == 0 or command[i - 1] in " \t\n;&|()"):
            while i < n and command[i] != "\n":
                i += 1
        elif c == "\n":
            out.append(" ; ")
            i += 1
            for delim, dash in pending:
                while i < n:
                    end = command.find("\n", i)
                    end = n if end < 0 else end
                    line, i = command[i:end], end + 1
                    if (line.lstrip("\t") if dash else line) == delim:
                        break
            pending = []
        elif command.startswith("<<", i) and not command.startswith("<<<", i):
            j = i + 2
            dash = command[j:j + 1] == "-"
            j += dash
            while j < n and command[j] in " \t":
                j += 1
            k = j
            while k < n and command[k] not in " \t\n;&|()<>":
                k += 1
            delim = re.sub(r"[\"'\\]", "", command[j:k])
            if delim:
                pending.append((delim, dash))
            out.append(f"<< {delim} ")
            i = k
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _argv_writes(words):
    """Files a simple command's words name as written: `tee` files, `sed -i` files, the `cp`/`mv` destination."""
    while words and _ASSIGN_RE.match(words[0]):
        words = words[1:]
    if not words:
        return []
    name, args = words[0], words[1:]
    plain = [w for w in args if not w.startswith("-")]
    if name == "tee":
        return plain
    if name == "sed" and any(w.startswith("-i") or w.startswith("--in-place") for w in args):
        return plain[1:]
    if name in ("cp", "mv"):
        for k, w in enumerate(args):
            if w == "-t" and k + 1 < len(args):
                return [args[k + 1]]
            if w.startswith("--target-directory="):
                return [w.split("=", 1)[1]]
        return plain[-1:] if len(plain) >= 2 else []
    return []


def _plausible(path):
    """Whether a written-file candidate looks like a path: plain path characters, not an option or a device."""
    return bool(_PATH_RE.match(path)) and not path.startswith(("-", "/dev/")) and bool(_ALNUM_RE.search(path))


def _shell_writes(command):
    """Paths a shell command visibly writes: redirection targets, `tee`, `sed -i` and `cp`/`mv` destinations of each
    simple command after shell-quote parsing (never from inside a quoted string or a heredoc body), plus apply_patch
    headers."""
    try:
        lex = shlex.shlex(_shell_text(command), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""
        tokens = list(lex)
    except ValueError:
        tokens = []
    out, words, k = [], [], 0
    while k <= len(tokens):
        token = tokens[k] if k < len(tokens) else ";"
        op = set(token) <= set("();<>|&")
        if op and token in _WRITE_OPS | _SKIP_OPS:
            if words and _DIGITS_RE.fullmatch(words[-1]):
                words.pop()  # the fd of 2> or 2>&1
            nxt = tokens[k + 1] if k + 1 < len(tokens) else ""
            if token in _WRITE_OPS and not set(nxt) <= set("();<>|&"):
                out.append(nxt)
            k += 2
            continue
        if op:
            out += _argv_writes(words)
            words = []
        else:
            words.append(token)
        k += 1
    return [norm_path(p) for p in out if _plausible(p)] + _patch_files(command)


def _patch_files(text):
    return [norm_path(m.group(1).strip(" \t\r")) for line in text.split("\n") if (m := _PATCH_FILE_RE.match(line))]


def effects(name, args):
    """What one tool call does: [("read", path, lo, hi, kind)], [("edit", path)...], ("global",) for a shell command
    that may change any file, ("test", command) for a test run."""
    tool = (name or "").lower()
    args = args if isinstance(args, dict) else {}
    path = next((args[k] for k in LEDGER_PATH_KEYS if isinstance(args.get(k), str) and args[k]), None)
    if tool in READ_TOOLS:
        if not path:
            return []
        offset, limit = _int(args.get("offset")), _int(args.get("limit"))
        if offset is None and limit is None:
            return [("read", norm_path(path), 1, None, tool)]
        lo = max(offset or 1, 1)
        return [("read", norm_path(path), lo, lo + limit - 1 if limit else None, tool)]
    if tool in LEDGER_WRITE_TOOLS:
        return [("edit", norm_path(path))] if path else []
    if tool in PATCH_TOOLS:
        return [("edit", p) for v in args.values() if isinstance(v, str) for p in _patch_files(v)]
    if tool not in SHELL_TOOLS:
        return []
    command = shell_command(args)
    if not command:
        return []
    if read_only(command):
        return [("read", p, lo, hi, kind) for p, lo, hi, kind in _shell_reads(command)]
    out = [("edit", p) for p in _shell_writes(command)]
    return out + ([("test", " ".join(command.split()))] if TEST_RE.search(command) else [("global",)])



def _range_label(lo, hi, kind):
    if kind == "tail":
        return f"last {hi} lines"
    if lo == 1 and hi is None:
        return "whole"
    return f"lines {lo}-{hi}" if hi is not None else f"lines {lo}-"


def _shown_range(text, lo, hi):
    """(lo, hi) the result text really shows: its first and last numbered line when it has them."""
    nums = [m.group(1) for line in text.split("\n") if (m := _NUMBERED_RE.match(line))]
    return (int(nums[0]), int(nums[-1])) if nums else (lo, hi)


def _covers(o, r):
    """Whether observation o shows every line read r asks for (§3.4: a partial read is reusable only when its range
    is fully covered by an earlier observation)."""
    if r["kind"] == "tail" or o["kind"] == "tail" or o["truncated"]:
        return False
    if r["hi"] is None:
        # To the end: only an untruncated read to the end, or the same whole read by the same tool.
        return (o["hi"] is None and o["lo"] <= r["lo"]) or (r["whole"] and o["whole"] and o["kind"] == r["kind"])
    return o["lo"] is not None and o["lo"] <= r["lo"] and (o["hi"] is None or r["hi"] <= o["hi"])


def walk(steps, new_from):
    """The ledger of steps [(j, rid, name, args, text, is_error)] in result order, and for each new step (j >=
    new_from) its repeat: {"rid", "j", "path", "label", "covers": [earlier (j, rid) current and covering, newest
    first]} for a read, {"rid", "j", "test": command} for a test rerun with no edit since.

    Returns (state, repeats); state = {"reads": {path: [obs]}, "modified": {path: edits}, "order": [paths]}."""
    files, total, glob = {}, 0, 0
    reads, modified, tests, repeats = {}, {}, {}, []
    for j, rid, name, args, text, is_error in steps:
        for effect in effects(name, args):
            match effect[0]:
                case "read":
                    _, path, lo, hi, kind = effect
                    if is_error:
                        continue
                    shown = _shown_range(text or "", lo, hi) if kind in READ_TOOLS else (lo, hi)
                    r = {"j": j, "rid": rid, "kind": kind, "lo": shown[0] if kind != "tail" else lo,
                         "hi": shown[1] if kind != "tail" else hi, "whole": lo == 1 and hi is None,
                         "label": _range_label(lo, hi, kind), "file": files.get(path, 0), "global": glob,
                         "truncated": bool(TRUNCATED_RE.search(text or "")) or is_compressed(text or "")}
                    if j >= new_from:
                        covers = [(o["j"], o["rid"]) for o in reversed(reads.get(path, ()))
                                  if o["file"] == r["file"] and o["global"] == glob and _covers(o, r)]
                        if covers:
                            repeats.append({"rid": rid, "j": j, "path": path, "label": r["label"], "covers": covers})
                    reads.setdefault(path, []).append(r)
                    reads[path] = reads.pop(path)
                case "edit":
                    path = effect[1]
                    files[path] = files.get(path, 0) + 1
                    modified.pop(path, None)
                    modified[path] = files[path]
                    total += 1
                case "global":
                    glob += 1
                    total += 1
                case "test":
                    command = effect[1]
                    if j >= new_from and tests.get(command) == total and not is_error:
                        repeats.append({"rid": rid, "j": j, "test": command})
                    tests[command] = total
    return {"reads": reads, "modified": modified, "files": files, "global": glob}, repeats


def block(state, reserve=0):
    """The ledger block for state (walk), at most LEDGER_MAX_CHARS less reserve; "" when it lists nothing. Paths are
    listed latest last; the oldest go first when over the cap."""
    reads, modified = state["reads"], state["modified"]
    if not reads and not modified:
        return ""
    lines = [LEDGER_HEAD]
    if modified:
        names = [f"{p} ×{n}" if n > 1 else p for p, n in list(modified.items())[-LEDGER_MAX_MODIFIED:]]
        lines.append("Modified (latest last): " + ", ".join(names))
    entries = []
    for path, obs in reads.items():
        current = state["files"].get(path, 0)
        labels = {}
        for o in obs:
            labels.pop(o["label"], None)
            labels[o["label"]] = "current" if o["file"] == current and o["global"] == state["global"] else "stale"
        parts = [f"{label} {status}" for label, status in list(labels.items())[-LEDGER_MAX_RANGES:]]
        entries.append(f"- {path}: " + "; ".join(parts))
    head = "\n".join(lines + (["Read (latest last):"] if entries else []))
    cap = LEDGER_MAX_CHARS - reserve
    kept, size = [], len(head)
    for entry in reversed(entries):
        if size + 1 + len(entry) > cap - 40:
            break
        kept.append(entry)
        size += 1 + len(entry)
    if len(kept) < len(entries):
        kept.append(f"- ({len(entries) - len(kept)} earlier files not listed)")
    return "\n".join([head, *reversed(kept)])[:cap]


def core_id(text):
    """A ledger block's identity, to skip a block that repeats the last one."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def nudge_line(repeat):
    """One line for a repeat the view cannot point at (its earlier copy is not shown in full), or a test rerun."""
    if "test" in repeat:
        return (f"- `{repeat['test'][:200]}` already ran with no file change since; rerun tests only for new "
                f"evidence.")
    return (f"- {repeat['path']} ({repeat['label']}) was read before and is unchanged since; that earlier output is "
            f"no longer shown in full.")
