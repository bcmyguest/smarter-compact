"""Lifecycle eviction (CONTEXT_EVICTION, docs/eviction.md): tool results an agent no longer needs, by rule.

Pure functions of the request's items through the adapter interface (is_assistant, tool_calls, tool_results), so
the OpenCode 2 / Pi trim can mirror them. A segment is one tool call and its result;
the evictable set is recomputed only at batch boundaries (core.py) and only grows.
"""

import json
import re
import shlex

# Tool name (lowercased) -> how it touches a file: Claude Code's file tools and OpenCode's equivalents.
READ_TOOLS = ("read",)
WRITE_TOOLS = ("edit", "write", "multiedit", "notebookedit")
PATH_KEYS = ("file_path", "filePath", "notebook_path", "path")
SHELL_TOOLS = ("bash",)
# Distinctive tokens of a result: paths, identifiers of 6+ characters, numbers of 4+ digits.
_PATH_RE = re.compile(r"(?:~|\.{0,2})/[\w.@+-]+(?:/[\w.@+-]+)*")
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{5,}")
_NUM_RE = re.compile(r"(?<![\d.])\d{4,}(?![\d.])")
# A Bash command that only prints files whole: `cat <path>...` with no pipe, redirect, chain or substitution.
_SHELL_META = set("|;&><`$")
RULE_FILE, RULE_COMMAND, RULE_UNREFERENCED = "file", "command", "unreferenced"
# Commands whose output a later identical run supersedes: they read state and change none.
READ_ONLY_COMMANDS = frozenset(("cat", "head", "tail", "less", "more", "wc", "ls", "find", "tree", "stat", "file",
                                "grep", "egrep", "fgrep", "rg", "ag", "pwd", "which"))
READ_ONLY_GIT = frozenset(("status", "diff", "log", "show", "branch"))
# `git branch` options that only list; any other option or a name creates, renames or deletes a branch.
GIT_BRANCH_LIST = frozenset(("-a", "-r", "-v", "-vv", "--all", "--remotes", "--verbose", "--list", "--show-current"))
# find actions that run commands, delete or write files.
FIND_ACTIONS = frozenset(("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"))
# Separators between simple commands; each side must be read-only on its own.
_CHAIN = frozenset(("|", "&&", "||", ";"))
_PUNCT = set("|&;()<>")
# A `sed -n` script that only prints: optional line or /regex/ addresses, then `p`.
_SED_PRINT_RE = re.compile(r"^(?:(?:\d+|\$|/[^/]*/)(?:,(?:\d+|\$|/[^/]*/))?)?p$")


def distinctive_tokens(text):
    return set(_PATH_RE.findall(text)) | set(_IDENT_RE.findall(text)) | set(_NUM_RE.findall(text))


def cat_paths(command):
    """Paths a plain `cat <path>...` prints whole; [] for any other command."""
    if any(ch in _SHELL_META for ch in command):
        return []
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    if not words or words[0] != "cat":
        return []
    return [w for w in words[1:] if not w.startswith("-")]


def _short_flag(arg, letter):
    return arg.startswith("-") and not arg.startswith("--") and letter in arg[1:]


def _sed_read_only(args):
    """`sed -n '<addr>p' <file>...` without an in-place flag."""
    if any(a.startswith("--in-place") or _short_flag(a, "i") for a in args):
        return False
    if not any(a in ("--quiet", "--silent") or _short_flag(a, "n") for a in args):
        return False
    scripts = [a for a in args if not a.startswith("-")]
    return bool(scripts) and _SED_PRINT_RE.match(scripts[0]) is not None


def _git_read_only(args):
    while args and args[0].startswith("-"):
        args = args[2:] if args[0] in ("-C", "-c") else args[1:]
    if not args or args[0] not in READ_ONLY_GIT or any(a.startswith("--output") for a in args):
        return False
    return args[0] != "branch" or all(a in GIT_BRANCH_LIST for a in args[1:])


def _simple_read_only(words):
    """True when one simple command (no separators) only reads."""
    name, args = words[0], words[1:]
    match name:
        case "sed":
            return _sed_read_only(args)
        case "git":
            return _git_read_only(args)
        case "env":
            return not args
        case "find":
            return not any(a in FIND_ACTIONS for a in args)
    return name in READ_ONLY_COMMANDS


def read_only(command):
    """True when a shell command only reads: allowlisted commands joined by |, &&, || or ;, with no redirection,
    background job, subshell, substitution or in-place edit. Unknown commands are not read-only."""
    if "$(" in command or "`" in command or ">" in command:
        return False
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        return False
    simple, words = [], []
    for token in tokens:
        if token in _CHAIN:
            if not words:
                return False
            simple.append(words)
            words = []
        elif set(token) <= _PUNCT:
            return False
        else:
            words.append(token)
    if words:
        simple.append(words)
    return bool(simple) and all(_simple_read_only(w) for w in simple)


def step_count(adapter, items):
    """Assistant steps in items: the unit a batch counts."""
    return sum(1 for x in items if adapter.is_assistant(x))


def _segment(call_id, step, name, args):
    """What a call touches: files (with Read's range) or a normalized shell command."""
    seg = {"id": call_id, "step": step, "files": (), "read": None, "write": False, "command": None}
    tool = (name or "").lower()
    if tool in READ_TOOLS or tool in WRITE_TOOLS:
        path = next((args[k] for k in PATH_KEYS if isinstance(args.get(k), str) and args[k]), None)
        if path:
            seg["files"] = (path,)
            seg["write"] = tool in WRITE_TOOLS
            if tool in READ_TOOLS:
                seg["read"] = (args.get("offset"), args.get("limit"))
    elif tool in SHELL_TOOLS and isinstance(args.get("command"), str):
        # Only a read-only command supersedes or is superseded: a stateful one (`make`, `mario step`) changes state.
        if read_only(args["command"]):
            seg["command"] = " ".join(args["command"].split())
        paths = cat_paths(args["command"])
        if paths:
            seg["files"], seg["read"] = tuple(paths), (None, None)
    return seg


def segments(adapter, items):
    """(segments in result order, refs per step): refs[s] holds the distinctive tokens of step s's text and tool
    inputs; each segment's `new` holds its result's tokens seen nowhere before it."""
    calls, segs, refs = {}, [], []
    seen = set()
    step = -1
    for j, item in enumerate(items):
        if adapter.is_assistant(item):
            step += 1
            tokens = _text_tokens(item)
            for call_id, name, args in adapter.tool_calls(item):
                tokens |= distinctive_tokens(json.dumps(args, sort_keys=True))
                calls[call_id] = _segment(call_id, step, name, args)
            refs.append(tokens)
            seen |= tokens
            continue
        seen |= _text_tokens(item)
        for rid, (_, text) in adapter.tool_results(item).items():
            seg = calls.pop(rid, None)
            tokens = distinctive_tokens(text or "")
            if seg is not None:
                segs.append({**seg, "index": j, "new": tokens - seen})
            seen |= tokens
    return segs, refs


def _text_tokens(item):
    """Tokens of an item's text blocks (not its tool calls or results)."""
    content = item.get("content")
    if isinstance(content, str):
        return distinctive_tokens(content)
    return distinctive_tokens("\n".join(str(b.get("text", "")) for b in content or ()
                                        if isinstance(b, dict) and b.get("type") == "text"))


def evictable(adapter, items, batch, keep, first=None):
    """{call id: rule} of the results the rules evict now. Results of the newest `batch` steps and of the first user
    turn never are. One pass from the newest result back, so a long session costs linear time.

    file: a later call wrote the same file, or read at least as much of it (the whole file, or the same range).
    command: the same read-only shell command (`read_only`) ran again later. unreferenced: `keep` or more tool results came after it, and
    none of its new distinctive tokens appear in any later assistant text or tool input."""
    segs, refs = segments(adapter, items)
    last_ref = {}
    for step, tokens in enumerate(refs):
        for token in tokens:
            last_ref[token] = step
    newest = len(refs) - batch
    written, whole, ranges, commands = set(), set(), set(), set()
    out = {}
    for pos in range(len(segs) - 1, -1, -1):
        seg = segs[pos]
        if seg["step"] < newest and seg["index"] != first:
            if any(f in written or f in whole or (f, seg["read"]) in ranges for f in seg["files"]):
                out[seg["id"]] = RULE_FILE
            elif seg["command"] in commands:
                out[seg["id"]] = RULE_COMMAND
            elif len(segs) - pos - 1 >= keep and not any(last_ref.get(t, -1) > seg["step"] for t in seg["new"]):
                out[seg["id"]] = RULE_UNREFERENCED
        for f in seg["files"]:
            if seg["write"]:
                written.add(f)
            elif seg["read"] == (None, None):
                whole.add(f)
            elif seg["read"] is not None:
                ranges.add((f, seg["read"]))
        if seg["command"]:
            commands.add(seg["command"])
    return out
