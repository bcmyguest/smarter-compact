"""Request rewriting: tool-result compression, sticky trim.

Pure stdlib. The LiteLLM hook calls process_request(data, call_type) before each request and
process_usage(kwargs, response) after each success; tests call them directly.
"""

import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
from collections import OrderedDict
from pathlib import Path

from . import embed as embed_mod
from . import evict as evict_mod
from . import ledger as ledger_mod
from . import sessiondb
from . import status as status_mod
from . import ttl as ttl_mod
from .adapters import SHORT_TTL_SECONDS, AnthropicAdapter, UnsupportedCallType, adapter_for
from .compress import (
    DEDUP_MIN_CHARS,
    DEDUP_PREFIX,
    INPUT_MASK_PREFIX,
    MASK_PREFIX,
    REUSE_PREFIX,
    blob_id,
    cleared_inputs,
    compressed_blob_id,
    dedup_placeholder,
    mask_placeholder,
    reduce_log,
    result_blob_id,
)
from .session import resolve_session_id
from .settings import Settings
from .store import Store, session_lock
from .tokens import (
    DEFAULT_CHARS_PER_TOKEN,
    estimate_prompt_tokens,
    fingerprint,
    record_usage,
    request_chars,
    signed_chars,
)

MAX_THREADS = 32
# Requests awaiting their usage report, by litellm_call_id: (sid, thread root, request chars, usage event fields).
# Streams' applied context edits wait in _stream_edits under the same bound.
MAX_PENDING = 256
# events.trimmed on a usage row: the request went up whole, carried an earlier cut, or made a new cut.
TRIM_NONE, TRIM_KEPT, TRIM_NEW = 0, 1, 2
USAGE_FIELDS = ("input_tokens", "cache_read_tokens", "cache_creation_tokens", "output_tokens")
# Anthropic server-side tool-result clearing (CONTEXT_SERVER_CLEAR=1); the API wants clear_thinking edits first.
SERVER_CLEAR_EDIT = "clear_tool_uses_20250919"
# A stream's applied context edits arrive in its final message_delta; this much of the stream's tail is scanned.
STREAM_TAIL_BYTES = 65536
# Tool exchanges a cut inside the in-flight turn always keeps; older ones stay while the request fits under
# the trim target (trim_to_for, capped at the trim threshold).
MIN_TAIL_STEPS = 3
# Added to the system prompt of every request while trimming is on, before any trim. The tool NAMES come from
# the request: a coding agent namespaces MCP tools by server (OpenCode `smarter-compact.smart_compact_search`,
# Claude Code `mcp__smarter-compact__smart_compact_search`), and ordering the model to call a bare name it was never
# offered is an instruction it cannot follow. Names can appear once the MCP server finishes connecting, so this
# text is not constant across a session: it settles (and the cached prefix changes) once, seconds in. Same text
# as standingInstruction() in plugin/lib/context-trim.mjs.
STANDING_MARKER = "Context management: to fit your context window"
# CONTEXT_REUSE_RULES: static don't-repeat-work rules ending the standing instruction, after Hu et al.,
# arXiv 2609.30725 §4.3 / A.4.2 (DevSkills). Same text as REUSE_RULES in plugin/lib/context-trim.mjs.
REUSE_RULES = (
    "Don't repeat work: before reading a file or running a command, check whether its output is already in your "
    "context and reuse it; re-read only when it may have changed, and say why.\n"
    "Rerun tests or builds only after the code changed or to get new evidence; save long output to a file and "
    "filter that instead of rerunning.\n"
    "Revise an existing script instead of writing a near-copy; when repeated actions bring no new evidence, stop "
    "and take a different approach."
)
# CONTEXT_DEDUP_RESULTS: dedup decisions kept per session (trim state "dedup"), oldest dropped first.
DEDUP_MAX = 512
# Any namespace separator, because that is what the harnesses actually use: OpenCode namespaces MCP
# tools with a dot (`smarter-compact.smart_compact_search`), Claude Code with `mcp__server__tool`, others with
# an underscore. The character class (not `_`) keeps `xsmart_compact_search` from matching.
_RECALL_TOOL_RE = re.compile(r"(?:^|[^a-z0-9])(smart_compact_search|smart_compact_sql|smart_compact_get|context_status)$")
# Claude Code defers MCP tools behind ToolSearch, so the request carries neither recall tool; the model must load
# them by their mcp__<server>__<tool> names first, or the bare name it is told to call fails "No such tool".
TOOL_SEARCH = "ToolSearch"
CLAUDE_CODE_PREFIX = "mcp__smarter-compact__"
# CONTEXT_PROGRESS_NOTE: the note's mechanical progress section, built from the trimmed tool calls.
PROGRESS_NOTE_MAX_CHARS = 2000
PROGRESS_MAX_FILES = 20
PROGRESS_MAX_COMMANDS = 5
PROGRESS_COMMAND_MAX_CHARS = 200
# CONTEXT_SCRATCHPAD: the pad's cap (the MCP tool's too), and its tool name with any namespace separator.
SCRATCHPAD_MAX_CHARS = 2500
SCRATCHPAD_TOOL = "smart_compact_scratchpad"
# Heads the pad in every trim note; the pad persists across trims, so it may predate recent work.
SCRATCHPAD_LABEL = "Your scratchpad, kept across trims (possibly stale; update it if the goal, plan or a key fact changed):"
_SCRATCHPAD_TOOL_RE = re.compile(r"(?:^|[^a-z0-9])smart_compact_scratchpad$")
# Start of the message in which Claude Code names the MCP tools it defers behind ToolSearch.
DEFERRED_TOOLS_HEAD = "deferred tools are now available via"
# Tool name -> input keys naming the file it writes (Claude Code's file tools).
EDIT_TOOL_PATHS = {"Write": ("file_path",), "Edit": ("file_path",), "MultiEdit": ("file_path",),
                   "NotebookEdit": ("notebook_path", "file_path")}
SHELL_TOOLS = ("Bash",)
# A shell command that builds or tests: a word of it starts one of these.
_BUILD_RE = re.compile(
    r"(?:^|[\s;&|(/])(?:py\.?test|tox|nox|make|cmake|ctest|ninja|cargo (?:build|test|check)|go (?:build|test|vet)"
    r"|(?:npm|yarn|pnpm)(?: run)? (?:test|build)|node --test|npx (?:jest|vitest|tsc)|jest|vitest|tsc|mvn|gradlew?"
    r"|javac|gcc|g\+\+|clang|rustc|dotnet (?:build|test)|rspec|rake|python[0-9.]* -m (?:pytest|unittest|build))"
    r"(?=\s|$|;)")
_EXIT_RE = re.compile(r"\bexit(?:ed with)?(?: code| status)?:? (-?\d+)", re.IGNORECASE)
# A thread entry's CONTEXT_EVICTION state: the sticky id set, its batch number, and the item it was computed on.
EVICT_KEYS = ("evict", "evict_batch", "evict_at", "evict_fp")
# A thread entry's CONTEXT_STRIP_REASONING_EVERY state: the reasoning bound, its fingerprint, the last batch checked.
REASON_KEYS = ("reason", "reason_fp", "reason_batch")
# Keys a thread entry may hold without a cut: the scratchpad nudge, eviction and batched reasoning state.
SIDE_KEYS = frozenset(("nudge_fp", "evict_fp", "reason_batch"))
# CONTEXT_LOG_REDUCE_OVER_CHARS decisions kept per session (trim state "logs"), oldest dropped first.
LOG_MAX = 512
# CONTEXT_COLD_TRIM / CONTEXT_CACHE_TTL_SPLIT: per thread root, the last request's arrival (epoch seconds) and the idle
# gaps over 5 minutes so far (trim state "last_request").
LAST_REQUEST_KEY = "last_request"
# CONTEXT_KEEP_ERRORS keeps an error result whole only up to this many characters; a longer one clears as usual.
KEEP_ERRORS_MAX_CHARS = 2000
# A tool result that reports a failure (CONTEXT_KEEP_ERRORS, and no ledger observation): a nonzero exit, a traceback,
# an exception line, a missing command or file, a test or compiler failure line. Matched per line, as in the plugin.
_ERROR_RE = re.compile(
    r"^Exit code [1-9]|Traceback \(most recent call last\)|^[A-Za-z_.]*(?:Error|Exception): |command not found"
    r"|No such file or directory|^(?:FAILED|FAIL|ERROR)(?![A-Za-z0-9_])|^error(?:\[E[0-9]+\])?: ")


def recall_tool_names(tools):
    """The search/sql names as this request actually offers them; bare names when not offered at all."""
    found = []
    deferred = False
    for tool in tools or []:
        name = tool.get("name") if isinstance(tool, dict) else tool
        if isinstance(name, str) and _RECALL_TOOL_RE.search(name):
            found.append(name)
        deferred = deferred or name == TOOL_SEARCH
    offered_pad = next((n for n in request_tool_names({"tools": tools}) if _SCRATCHPAD_TOOL_RE.search(n)), None)
    pad = offered_pad or (CLAUDE_CODE_PREFIX + SCRATCHPAD_TOOL if deferred else SCRATCHPAD_TOOL)
    # The scratchpad tool, whether the request offers it, and how to load it when deferred ("" when offered or never
    # loadable).
    scratchpad = {"scratchpad": pad, "scratchpad_offered": offered_pad is not None,
                  "scratchpad_load": f'{TOOL_SEARCH} with query "select:{pad}"' if deferred and not offered_pad else ""}
    if not found and deferred:
        search, sql = CLAUDE_CODE_PREFIX + "smart_compact_search", CLAUDE_CODE_PREFIX + "smart_compact_sql"
        return {"search": search, "sql": sql, "offered": found,
                "load": f'{TOOL_SEARCH} with query "select:{search},{sql}"', **scratchpad}

    def pick(suffix):
        return next((n for n in found if n.endswith(suffix)), suffix)

    return {"search": pick("smart_compact_search"), "sql": pick("smart_compact_sql"), "offered": found, "load": "",
            **scratchpad}


def _availability(names):
    """How the pointer says the tools can be reached: offered outright, or loaded through ToolSearch first."""
    if names.get("load"):
        return f"The tools {names['search']} and {names['sql']} are deferred: load them first by calling {names['load']}. "
    return f"The tools {names['search']} and {names['sql']} are available to you in this request. "


def counted_tools(tools):
    """Tools without defer_loading: a deferred tool costs no prompt tokens until a tool_reference loads it."""
    if not isinstance(tools, list):
        return tools
    return [t for t in tools if not (isinstance(t, dict) and t.get("defer_loading") is True)]


def request_tool_names(data):
    """Tool names from an Anthropic- or OpenAI-format request body."""
    tools = data.get("tools") if isinstance(data, dict) else None
    names = []
    for tool in tools or []:
        if isinstance(tool, dict):
            name = tool.get("name") or (tool.get("function") or {}).get("name")
            if isinstance(name, str):
                names.append(name)
        elif isinstance(tool, str):
            names.append(tool)
    return names


def standing_instruction(names=None, reuse=True):
    names = names or recall_tool_names([])
    return (
        STANDING_MARKER + ", older turns may be removed and replaced by an "
        "[smarter-compact: …] pointer. Removed content is saved, not forgotten. Before answering anything that depends on "
        "earlier content you cannot see in your context — values, ids, keys, counts, decisions — call "
        f"{names['search']} (use `pattern` for exact ids/keys, `context` for neighbouring rows; `line_matches` gives "
        f"exact counts) or {names['sql']} for counts and aggregates over the session store; never guess or answer "
        'from memory you cannot see. This trim never summarizes removed content — a remembered "summary of earlier '
        "context\" is not its record; cite a search result instead."
        + (f" The recall tools are deferred: load them first by calling {names['load']}." if names.get("load") else "")
        + (f"\n{REUSE_RULES}" if reuse else "")
    )


STANDING_INSTRUCTION = standing_instruction()


_pending = OrderedDict()
_stream_edits = OrderedDict()
_pending_guard = threading.Lock()


def code_hash(directory):
    """First 12 hex of SHA-256 over this package's *.py files (name, NUL, bytes, NUL, sorted by name): the running
    code's identity. cli/context-management.mjs hashes the checkout the same way to spot a stale proxy."""
    digest = hashlib.sha256()
    for path in sorted(Path(directory).glob("*.py")):
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:12]


# The events table's version column for rows this proxy writes.
VERSION = f"proxy:{code_hash(Path(__file__).parent)}"


def _where(exc):
    tb = traceback.extract_tb(exc.__traceback__)
    return f"{tb[-1].filename.rsplit('/', 1)[-1]}:{tb[-1].lineno}" if tb else "?"


def _log_error(exc):
    """One stderr line: exception type and location only, never message content."""
    print(f"smarter-compact: fail-open {type(exc).__name__} at {_where(exc)}", file=sys.stderr, flush=True)


def _log_shape(sid, call_type, data, items, adapter):
    """CONTEXT_DEBUG_SHAPES=1: one stderr line per request with its shape as received; counts only, never content."""
    system = data.get("system")
    system_chars = len(system) if isinstance(system, str) else len(json.dumps(system)) if system else 0
    tools = data.get("tools")
    roots = [x for x in items if not adapter.is_pinned(x)]
    roles = "".join((x.get("role") or x.get("type") or "?")[0] for x in items[-12:])
    print(f"smarter-compact: shape sid={sid} call={call_type} items={len(items)} pinned={len(items) - len(roots)} "
          f"system_chars={system_chars} tools={len(tools) if isinstance(tools, list) else 0} "
          f"root={fingerprint(roots[0])[:8] if roots else '-'} tail_roles={roles}", file=sys.stderr, flush=True)


def _record_event(store, sid, kind, **fields):
    """Append an events row to sessions/<sid>.db (docs/context-store.md, "Events"). Never raises or logs: a database
    that cannot take the row already failed, and logged, while storing the request's messages or before."""
    path = store.db_path(sid)
    try:
        store.ensure_sessions()
        sessiondb.open_session(path).add_event(kind, _utc_now(), VERSION, **fields)
    except Exception:
        sessiondb.drop_session(path)


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# No tool list at all (a caller that predates name resolution) keeps the historical wording; an
# explicit empty list means the tools genuinely are not in this request, which is said plainly.
_LEGACY_NAMES = recall_tool_names(["smart_compact_search", "smart_compact_sql"])


# No tool list at all (a caller predating name resolution) falls back to the bare names, which is
# also the historical text; an offered list resolves to the names the model can actually call. A
# request that offers no recall tool is NOT announced as unsearchable: the pointer's job is to
# route the model to the store, and telling it the door is shut is how recall stops being used.
# Whether the tools were really offered is measured, not asserted (bench/swebench/request_capture.py).
_LEGACY_NAMES = recall_tool_names(["smart_compact_search", "smart_compact_sql"])


def pointer_text(sid, dropped, names=None):
    names = names or _LEGACY_NAMES
    return (
        f"[smarter-compact: continuing session {sid}; {dropped} earlier messages are NOT in your context. They were "
        f"delivered to you and later removed to fit your context window, so a gap in what you see (missing "
        f"batches, lines, keys) is in them. "
        f"Nothing here is or was ever summarized: this trim wrote no summary of the dropped messages, so do not "
        f"treat a \"summary of earlier context\" you seem to remember as their record. The only complete record of "
        f"them is this session's store. "
        + _availability(names) +
        f'You MUST call {names["search"]}(session_id="{sid}", query=... or pattern=...) before answering '
        f'anything that depends on them, and never answer "not found", "never received" or "no search tool" '
        f"without calling it. "
        f"Dropped user turns may hold instructions or corrections that still apply: search them "
        f'({names["search"]} with role="user") before changing course. '
        f"To recover a file you read earlier, search for the text you need instead of reading it again, "
        f"and re-read only the lines you still lack (offset/limit). "
        f"Every message of this session is stored and queryable with "
        f'{names["search"]} and {names["sql"]}(session_id="{sid}", sql=...).]'
    )


def reasoning_text(sid):
    """The note of a trim that only stripped earlier reasoning (CONTEXT_STRIP_OLD_REASONING): no message is gone."""
    return (f"[smarter-compact: session {sid}] Your thinking from earlier steps was removed to fit your context "
            f"window; every message, tool call and tool result is still here.]")


def mask_text(sid, names=None):
    """How to get back a result shown as a mask_placeholder: its own smart_compact_get call, else search then sql."""
    names = names or _LEGACY_NAMES
    return (
        f"Older tool outputs in your context read \"{MASK_PREFIX} smart_compact_get(id=\"<id>\") returns it]\". "
        f"Each names its own id: smart_compact_get with that id returns the full output. If that fails, find its call with "
        f'{names["search"]}(session_id="{sid}", query=<words from the call>); its output is the first tool_result '
        f'row of the same tool after that seq: {names["sql"]}(session_id="{sid}", sql="SELECT text FROM messages '
        f"WHERE kind = 'tool_result' AND name = '<tool>' AND seq > <call seq> ORDER BY seq LIMIT 1\")."
    )


def scratchpad_nudge(sid, names):
    """The once-per-trim-cycle nudge to save task state; fixed for a session and tool name (CONTEXT_SCRATCHPAD)."""
    load = f" (load it first by calling {names['scratchpad_load']})" if names.get("scratchpad_load") else ""
    return (
        f"<compact-scratchpad-nudge>Your context will be trimmed soon: older turns will be removed. If you have no "
        f"scratchpad yet, create one with {names['scratchpad']}(session_id=\"{sid}\", command=\"create\", "
        f"file_text=...){load}, at most {SCRATCHPAD_MAX_CHARS} characters. Write, tersely:\n"
        f"- goal and how it is checked\n- status: what works, what fails\n- decisions and why\n"
        f"- facts you would otherwise re-discover (paths, commands, values)\n"
        f"- files you wrote (paths) and what each holds\n- the exact next step\n"
        f"No logs, code or command output. It is kept and shown after every trim; if you already have one, update it "
        f"(str_replace) only when the goal, plan or a key fact changed.</compact-scratchpad-nudge>"
    )


def scratchpad_available(names, data):
    """True when the request offers the scratchpad tool, or Claude Code's deferred-tool list names it."""
    if names.get("scratchpad_offered"):
        return True
    if not names.get("scratchpad_load"):
        return False
    texts = [data.get("system")] + [x.get("content") for x in data.get("messages") or [] if isinstance(x, dict)]
    for content in texts:
        for text in [content] if isinstance(content, str) else [b.get("text") for b in content or []
                                                                 if isinstance(b, dict)]:
            if isinstance(text, str) and DEFERRED_TOOLS_HEAD in text and names["scratchpad"] in text:
                return True
    return False


def scratchpad_block(text):
    """The trim note's labelled block carrying the agent's pad, capped at SCRATCHPAD_MAX_CHARS."""
    return f"{SCRATCHPAD_LABEL}\n<compact-scratchpad>\n{text[:SCRATCHPAD_MAX_CHARS]}\n</compact-scratchpad>"


def _nudged(adapter, items, at, text):
    """items with the nudge appended to item `at`; items itself when there is none to show."""
    if at is None or not text:
        return items
    item = adapter.appended_note(items[at], text) if hasattr(adapter, "appended_note") else None
    return items if item is None else [*items[:at], item, *items[at + 1:]]


def add_standing_instruction(adapter, data, reuse=True):
    """Add the standing instruction, naming the tools this request offers, unless one is already there."""
    text = standing_instruction(recall_tool_names(request_tool_names(data)), reuse)
    if STANDING_MARKER not in adapter.system_text(data):
        adapter.add_instruction(data, text)


def _dedup_found(items, decisions):
    """{index: [decision]} for each recorded CONTEXT_DEDUP_RESULTS decision whose item is still in items: at its
    index when the fingerprint matches there, else the last item with that fingerprint."""
    out, fps, index = {}, {}, None
    for d in decisions:
        if not isinstance(d, dict):
            continue
        at, fp = d.get("at"), d.get("fp")
        j = None
        if isinstance(at, int) and 0 <= at < len(items):
            if at not in fps:
                fps[at] = fingerprint(items[at])
            j = at if fps[at] == fp else None
        if j is None:
            if index is None:
                index = {fingerprint(x): i for i, x in enumerate(items)}
            j = index.get(fp)
        if j is not None:
            out.setdefault(j, []).append(d)
    return out


def _deduped(adapter, items, found):
    """items with each found decision's result shown as its dedup_placeholder, or a CONTEXT_LEDGER reuse decision's
    recorded pointer; items itself when none changes."""
    out = None
    for j, decisions in found.items():
        x = items[j]
        for d in decisions:
            text = adapter.result_texts(x).get(d.get("id"))
            if text is not None and result_blob_id(text) == d.get("bid"):
                # A reuse decision recorded before reuse became byte-identical-only keeps its recorded pointer.
                ptr = d["ptr"] if d.get("kind") == "reuse" and "ptr" in d else dedup_placeholder(text)
                if isinstance(ptr, str):
                    x = adapter.replaced_result(x, d["id"], ptr)
        if x is not items[j]:
            out = out or list(items)
            out[j] = x
    return items if out is None else out


def _dedup_new(adapter, items, shown, found):
    """[(decision, text)] for each new tool result (after the last assistant item) of at least DEDUP_MIN_CHARS that
    is byte-identical to an earlier result the view (shown) carries in full; the newer copy alone is replaced."""
    last = max((j for j, x in enumerate(items) if adapter.is_assistant(x)), default=None)
    if last is None:
        return []
    seen, new = {}, []
    for j in sorted(shown):
        decided = {d.get("id") for d in found.get(j, ())}
        whole = adapter.result_texts(items[j]) if j > last else {}
        orig = adapter.result_texts(items[j])
        for rid, now in adapter.result_texts(shown[j]).items():
            # An earlier result shown in full plus a ledger block appended to its text counts as its own text.
            full = orig.get(rid)
            text = full if j <= last and full is not None and now.startswith(full) else now
            if j > last and rid not in decided and text in seen and len(text) >= DEDUP_MIN_CHARS \
                    and whole.get(rid) == text and not text.startswith((DEDUP_PREFIX, MASK_PREFIX, REUSE_PREFIX)):
                new.append(({"at": j, "fp": fingerprint(items[j]), "id": rid, "src": seen[text],
                             "bid": result_blob_id(text)}, text))
            else:
                seen.setdefault(text, rid)
    return new


def _last_assistant(adapter, items):
    return max((j for j, x in enumerate(items) if adapter.is_assistant(x)), default=None)


def _newest_unpinned(adapter, items):
    """Index of the newest item that is not pinned: Claude Code ends each request with a system message."""
    return next((j for j in range(len(items) - 1, -1, -1) if not adapter.is_pinned(items[j])), len(items) - 1)


def _is_error(is_error, text):
    """Whether a tool result reports a failure: the error flag, else an _ERROR_RE line."""
    return bool(is_error) or any(_ERROR_RE.search(line) for line in (text or "").split("\n"))


def _log_reduced(adapter, original, items, decisions, settings, store):
    """(items with each recorded CONTEXT_LOG_REDUCE_OVER_CHARS decision's result, and each new one's, shown as its
    reduce_log form, then compressed as usual; new decisions). A new decision is a text tool result after the last
    assistant item, over the setting, that reduce_log takes; a recorded one renders whatever the setting now is."""
    found = _dedup_found(original, decisions) if decisions else {}
    last = _last_assistant(adapter, original)
    new = []
    for j in range(last + 1 if last is not None else len(original), len(original)):
        if j in found:
            continue
        for rid, text in adapter.result_texts(original[j]).items():
            if reduce_log(text, settings.log_reduce_over_chars):
                d = {"at": j, "fp": fingerprint(original[j]), "id": rid, "bid": blob_id(text)}
                new.append(d)
                found.setdefault(j, []).append(d)
    out = None
    for j, ds in found.items():
        x = original[j]
        for d in ds:
            text = adapter.result_texts(x).get(d.get("id"))
            reduced = reduce_log(text, 1) if text is not None and blob_id(text) == d.get("bid") else None
            if reduced:
                store.write_blob(reduced[1], text)
                x = adapter.replaced_result(x, d["id"], reduced[0])
        if x is not original[j]:
            if settings.compress_over_tokens > 0:
                x = adapter.compress_item(x, settings.compress_over_tokens, store.write_blob)
            out = out or list(items)
            out[j] = x
    return (items if out is None else out), new


class _Exempt:
    """The adapter, except no result or input of a call in keep is ever masked (CONTEXT_KEEP_ERRORS)."""

    def __init__(self, adapter, keep):
        self._adapter, self._keep = adapter, frozenset(keep)

    def __getattr__(self, name):
        return getattr(self._adapter, name)

    def masked_results(self, item, ids):
        return self._adapter.masked_results(item, set(ids) - self._keep)

    def masked_inputs(self, item, ids, over):
        return self._adapter.masked_inputs(item, set(ids) - self._keep, over)


def _exempting(adapter, keep):
    """adapter wrapped by _Exempt when keep names a call and the adapter masks results; adapter itself otherwise."""
    return _Exempt(adapter, keep) if keep and hasattr(adapter, "masked_results") else adapter


def _error_ids(adapter, items, k, shown):
    """Call ids of the k newest error tool results (_is_error) of at most KEEP_ERRORS_MAX_CHARS the view (shown) still
    carries whole, newest first: a result already cleared stays cleared, so the exempt set only moves forward."""
    out = []
    for j in range(len(items) - 1, -1, -1):
        results = adapter.tool_results(items[j]) if j in shown and hasattr(adapter, "tool_results") else {}
        now = adapter.tool_results(shown[j]) if results else {}
        for rid, (is_error, text) in reversed(list(results.items())):
            if len(text or "") <= KEEP_ERRORS_MAX_CHARS and _is_error(is_error, text) and rid in now \
                    and now[rid][1] == text:
                out.append(rid)
                if len(out) >= k:
                    return out
    return out


def _reason_stripped(adapter, items, bound):
    """items with signed reasoning stripped from every assistant item before bound (CONTEXT_STRIP_REASONING_EVERY),
    never emptying a message and never dropping an item; items itself when none changes."""
    out = None
    for j in range(min(bound, len(items)) if bound is not None else 0):
        x = items[j]
        bare = _bare(adapter, x, True) if adapter.is_assistant(x) else x
        if bare is not None and bare is not x:
            out = out or list(items)
            out[j] = bare
    return items if out is None else out


def _reason_state(thread):
    """The thread entry's CONTEXT_STRIP_REASONING_EVERY keys."""
    return {k: v for k, v in (thread or {}).items() if k in REASON_KEYS}


def _ledger_steps(adapter, items):
    """(j, result id, tool name, args, text, is_error) of each tool result whose call the items hold, in order."""
    calls, steps = {}, []
    for j, x in enumerate(items):
        for cid, name, args in adapter.calls(x):
            calls[cid] = (name, args)
        for rid, (is_error, text) in adapter.tool_results(x).items():
            if rid in calls:
                name, args = calls.pop(rid)
                steps.append((j, rid, name, args, text, _is_error(is_error, text)))
    return steps


def _visible(adapter, items, shown, j, rid):
    """Whether the view (shown) carries result rid of items[j] in full."""
    full = adapter.result_texts(items[j]).get(rid) if 0 <= j < len(items) else None
    now = adapter.result_texts(shown[j]).get(rid) if j in shown else None
    return full is not None and now is not None and now.startswith(full)


def _ledger_new(adapter, items, shown, found, fresh):
    """(ledger walk state or None, [(reuse decision, text)], nudge lines) for the new tool results (after the last
    assistant item) under CONTEXT_LEDGER. A read repeating an earlier current read that covers it becomes the
    dedup_placeholder pointer when its output is byte-identical to a covering copy still shown in full, stays as it is
    when every such copy differs, else gets a nudge line; a test rerun with no edit since, a nudge."""
    last = _last_assistant(adapter, items)
    if last is None:
        return None, [], []
    state, repeats = ledger_mod.walk(_ledger_steps(adapter, items), last + 1)
    decided = {(j, d.get("id")) for j, ds in found.items() for d in ds} | {(d["at"], d["id"]) for d, _ in fresh}
    gone = {(d["at"], d["src"]) for d, _ in fresh}
    reuse, nudges = [], []
    for r in repeats:
        if (r["j"], r["rid"]) in decided:
            continue
        if "test" in r:
            nudges.append(ledger_mod.nudge_line(r))
            continue
        shown_copies = [(j, rid) for j, rid in r["covers"] if (j, rid) not in decided and (j, rid) not in gone
                        and _visible(adapter, items, shown, j, rid)]
        text = adapter.result_texts(items[r["j"]]).get(r["rid"])
        src = next(((j, rid) for j, rid in shown_copies if adapter.result_texts(items[j]).get(rid) == text), None)
        if not shown_copies:
            nudges.append(ledger_mod.nudge_line(r))
        elif src is not None and text is not None and len(text) >= DEDUP_MIN_CHARS \
                and not text.startswith((DEDUP_PREFIX, MASK_PREFIX, REUSE_PREFIX)):
            # "kind": "reuse" ties the decision to CONTEXT_LEDGER; it renders as the dedup pointer.
            reuse.append(({"kind": "reuse", "at": r["j"], "fp": fingerprint(items[r["j"]]), "id": r["rid"],
                           "src": src[1], "bid": result_blob_id(text)}, text))
            decided.add((r["j"], r["rid"]))
    return state, reuse, nudges


def _ledgered(adapter, items, found, lo):
    """items with each recorded ledger block at or after index lo appended to its item (CONTEXT_LEDGER)."""
    for j in sorted(found):
        if lo is None or j >= lo:
            for d in found[j]:
                items = _nudged(adapter, items, j, d.get("text") if isinstance(d.get("text"), str) else None)
    return items


def _ledger_block(adapter, items, sent, state, nudges, found):
    """The ledger block decision for this request's newest unpinned item, or None: it must hold new tool results,
    carry no block yet, and the block must differ from the newest recorded one or carry nudges."""
    j = _newest_unpinned(adapter, items)
    last = _last_assistant(adapter, items)
    if state is None or last is None or j <= last or j in found or not adapter.tool_results(items[j]):
        return None
    tail = ("\nRepeats in this batch:\n" + "\n".join(nudges[:3])) if nudges else ""
    core = ledger_mod.block(state, len(tail))
    prev = found[max(found)][-1].get("core") if found else None
    if not (core or tail) or (not tail and ledger_mod.core_id(core) == prev):
        return None
    text = (core or ledger_mod.LEDGER_HEAD) + tail
    if _nudged(adapter, sent, j, text) is sent:
        return None
    return {"at": j, "fp": fingerprint(items[j]), "text": text, "core": ledger_mod.core_id(core)}


def _clock():
    """Wall-clock seconds; tests replace it."""
    return time.time()


def _tick(state, root, now):
    """(seconds since the thread's previous request or None, idle gaps over 5 minutes so far, this one included);
    records now as the thread's last request in state."""
    seen = state.get(LAST_REQUEST_KEY) if isinstance(state.get(LAST_REQUEST_KEY), dict) else {}
    prev = seen.pop(root, None)
    prev = prev if isinstance(prev, dict) else {}
    at = prev.get("at") if type(prev.get("at")) is int else None
    gaps = prev.get("long") if type(prev.get("long")) is int else 0
    gap = now - at if at is not None else None
    if gap is not None and gap > SHORT_TTL_SECONDS:
        gaps += 1
    seen[root] = {"at": now, "long": gaps}
    while len(seen) > MAX_THREADS:
        seen.pop(next(iter(seen)))
    state[LAST_REQUEST_KEY] = seen
    return gap, gaps


def _split_ttl(adapter, data):
    """data's messages with CONTEXT_CACHE_TTL_SPLIT applied (AnthropicAdapter.split_cache_ttl), set in place."""
    out = adapter.split_cache_ttl(data)
    if out is not data:
        data["messages"] = out["messages"]
    return data


def first_user_index(adapter, items):
    """Index of the first real user turn (pinned through every trim), or None."""
    for i, item in enumerate(items):
        if adapter.is_first_user(item):
            return i
    return None


def prev_reply_index(adapter, items, cut, first):
    """Index of the agent reply just before the cut turn (kept with it, so the turn reads in context), or None."""
    j = cut - 1
    if j < 0 or j == first or adapter.is_pinned(items[j]) or not adapter.is_reply(items[j]):
        return None
    return j


def kept_indices(adapter, items, cut, gap=None, first=None):
    """Indices the view keeps: pinned items, the first user turn, the reply before the cut turn, the cut turn, and
    every later item except the in-turn gap [cut + 1, gap). Everything when cut is None."""
    if cut is None:
        return set(range(len(items)))
    head = {first, prev_reply_index(adapter, items, cut, first), cut}
    return {j for j, x in enumerate(items)
            if j in head or adapter.is_pinned(x) or (j > cut and (gap is None or j >= gap))}


def _droppable(adapter, items, cut, gap, first):
    keep = kept_indices(adapter, items, cut, gap, first)
    return [x for j, x in enumerate(items) if j not in keep]


def _pairs_intact(adapter, items, keep):
    """Whether the kept and dropped items share no call/result pair."""
    def ids(fn, inside):
        return set().union(*(fn(x) for j, x in enumerate(items) if (j in keep) == inside)) - {None}
    return not (ids(adapter.call_ids, False) & ids(adapter.result_ids, True)
                or ids(adapter.call_ids, True) & ids(adapter.result_ids, False))


def choose_cut(adapter, items, after, first=None):
    """Index of the last real user turn > after whose kept part shares no call/result pair with the dropped part."""
    for i in range(len(items) - 1, after, -1):
        if not adapter.is_turn_start(items[i]):
            continue
        keep = kept_indices(adapter, items, i, None, first)
        if len(keep) == len(items):
            return None
        if _pairs_intact(adapter, items, keep):
            return i
    return None


def last_turn_index(adapter, items):
    return next((i for i in range(len(items) - 1, -1, -1) if adapter.is_turn_start(items[i])), None)


def choose_gap(adapter, items, cut, after, first, fits):
    """Start of the kept tail for a cut inside the turn at `cut`, > after; None when no gap is possible.

    The tail keeps the newest MIN_TAIL_STEPS tool exchanges, then older ones while fits(gap) holds (the request
    stays under the trim target: trim_to_for, capped at the trim threshold). An exchange is never split: the tail
    starts at an adapter step start.
    """
    starts = [j for j in range(cut + 2, len(items)) if adapter.is_step_start(items, j)]
    best = None
    for n, g in enumerate(reversed(starts), 1):
        if g <= after:
            break
        if n < MIN_TAIL_STEPS:
            continue
        if n > MIN_TAIL_STEPS and not fits(g):
            break
        if _pairs_intact(adapter, items, kept_indices(adapter, items, cut, g, first)):
            best = g
    return best


def render(adapter, items, cut, note, first=None, gap=None, strip=None, prose=False, shown=None, clear=None,
           evict=None, inputs=0, old=False):
    """The kept items (kept_indices) in order, the cut turn carrying the note, with signed reasoning stripped from
    kept assistant items before `strip` (default and cap: strip_top, with `old` the latest assistant message, else the
    latest assistant run; see strip_reasoning), and with prose
    their tool call/result pairs too (prose_ids), or with prose == "mask" those results masked instead; every tool
    result before `clear` is masked too (CONTEXT_CLEAR_KEEP_RESULTS), and every result whose call id is in `evict`
    (CONTEXT_EVICTION). Wherever a result is masked so are its call's input strings over `inputs` chars
    (CONTEXT_CLEAR_INPUTS_OVER_CHARS). shown, when given, gets the strip_reasoning index map."""
    if cut is None:
        view = [_evicted(adapter, x, j, evict, first, inputs) for j, x in enumerate(items)] if evict else items
        if shown is not None:
            shown.update(enumerate(view))
        return view
    keep = kept_indices(adapter, items, cut, gap, first)
    top = strip_top(adapter, items, old)
    return strip_reasoning(adapter, items, keep, top if strip is None else min(strip, top), cut, note, prose, shown,
                           clear, first, evict, inputs, old)


def latest_run_start(adapter, items):
    """Index where the trailing run of assistant items ending at the last one starts; len(items) if none."""
    last = next((j for j in range(len(items) - 1, -1, -1) if adapter.is_assistant(items[j])), None)
    if last is None:
        return len(items)
    while last > 0 and adapter.is_assistant(items[last - 1]):
        last -= 1
    return last


def latest_message_start(adapter, items):
    """Index where the latest assistant message starts (a Responses reasoning item and its calls are one step);
    len(items) if none."""
    last = next((j for j in range(len(items) - 1, -1, -1) if adapter.is_assistant(items[j])), None)
    if last is None:
        return len(items)
    while last > 0 and adapter.is_assistant(items[last - 1]) and not adapter.is_step_start(items, last):
        last -= 1
    return last


def strip_top(adapter, items, old):
    """Where a trim's reasoning strip ends: the latest assistant message with `old` (CONTEXT_STRIP_OLD_REASONING),
    else the latest assistant run."""
    return latest_message_start(adapter, items) if old else latest_run_start(adapter, items)


def _size(old, *values):
    """request_chars, plus the thinking signatures with `old`: measured at 3.1 to 3.3 characters per billed token."""
    return request_chars(*values) + (sum(signed_chars(v) for v in values) if old else 0)


def _bare(adapter, x, old):
    """x without its signed reasoning; None when nothing is left, except with `old` a message (not a bare reasoning
    item) holding only reasoning comes back whole."""
    bare = adapter.without_reasoning(x)
    return x if bare is None and old and not adapter.is_reasoning(x) else bare


def strip_reasoning(adapter, items, keep, bound, cut, note, prose=False, shown=None, clear=None, first=None,
                    evict=None, inputs=0, old=False):
    """The kept items, the cut turn carrying the note, with signed reasoning removed from assistant items before
    `bound`.

    A thinking block is signed against everything before it; a trim edits that prefix, so blocks kept from before
    the trim would no longer verify. The trim records `bound` as the start of the latest assistant run at that
    moment, which goes out unchanged, as the API requires; reasoning produced after the trim was generated on the
    trimmed view and stays. The stripped set is fixed until the next trim, so the prefix stays byte-identical for
    the cache. A message left empty is dropped (adapter.without_reasoning); with `old` (CONTEXT_STRIP_OLD_REASONING)
    it keeps its reasoning instead, and only a bare reasoning item (Responses) drops (_bare).

    With prose, the prose_ids pairs go too and adjacent stripped items of one role merge into one message. With
    prose == "mask", each prose_ids tool_use stays and its tool_result content becomes its mask_placeholder.

    Every tool_result of a kept item before `clear` (the first user turn and pinned items aside) also becomes
    its mask_placeholder; its tool_use and the item's other blocks stay (_cleared). So does every result whose call
    id is in evict (_evicted). Each masked result's call also gets its input strings over `inputs` chars masked
    (_masked_inputs).

    shown, when given, maps the index of every item that reaches the view to its stripped form (no note, unmerged).
    """
    ids = prose_ids(adapter, items, keep, bound, cut, prose) if prose else set()
    calls = _cleared_calls(adapter, items, clear, first) if inputs > 0 else set()
    out = []
    mergeable = False
    for j, x in enumerate(items):
        if j not in keep:
            continue
        region = False
        if j != cut and j < bound:
            if adapter.is_assistant(x):
                x = _bare(adapter, x, old)
                if x is None:
                    continue
            if ids and not adapter.is_pinned(x) and prose == "mask":
                x = _masked_inputs(adapter, adapter.masked_results(x, ids), ids, inputs)
            elif ids and not adapter.is_pinned(x):
                x = adapter.without_tool_calls(x, ids)
                if x is None:
                    continue
                region = True
        x = _evicted(adapter, _cleared(adapter, x, j, clear, first, inputs, calls), j, evict, first, inputs)
        if shown is not None:
            shown[j] = x
        if j == cut:
            x = adapter.prepend_note(x, note)
        elif region and mergeable and adapter.same_role(out[-1], x):
            out[-1] = adapter.merged(out[-1], x)
            continue
        out.append(x)
        mergeable = region
    return out


def _cleared(adapter, x, j, clear, first, inputs=0, calls=()):
    """x with every tool_result masked when items[j] sits before the clear bound, and the inputs of its calls in
    calls (_cleared_calls) over `inputs` chars; x itself otherwise."""
    if clear is None or j >= clear or j == first or adapter.is_pinned(x):
        return x
    return _masked_inputs(adapter, adapter.masked_results(x, adapter.result_ids(x)), calls, inputs)


def _cleared_calls(adapter, items, clear, first):
    """Call ids whose result the clear bound clears: every result of an item before it (first and pinned aside)."""
    if clear is None:
        return set()
    return set().union(*(adapter.result_ids(items[j]) for j in range(min(clear, len(items)))
                         if j != first and not adapter.is_pinned(items[j]))) - {None}


def _masked_inputs(adapter, x, ids, inputs):
    """x with the input strings over `inputs` chars of its calls in ids masked; x itself when off or none is."""
    if inputs <= 0 or not ids or not hasattr(adapter, "masked_inputs"):
        return x
    found = adapter.call_ids(x) & ids
    return adapter.masked_inputs(x, found, inputs) if found else x


def _evicted(adapter, x, j, evict, first, inputs=0):
    """x with each tool result whose call id is in evict masked (CONTEXT_EVICTION), and those calls' inputs over
    `inputs` chars; x itself when none is."""
    if not evict or j == first or adapter.is_pinned(x):
        return x
    ids = adapter.result_ids(x) & evict
    return _masked_inputs(adapter, adapter.masked_results(x, ids) if ids else x, evict, inputs)


def _eviction(adapter, items, thread, settings, first):
    """(evicted call ids, ids new this request, thread state update or None) under CONTEXT_EVICTION.

    The set is the thread's stored one while the item it was computed on (evict_at / evict_fp) is still there; it
    grows only when the request opens a new batch of eviction_batch assistant steps, so requests within a batch
    render the same bytes. A history that lost that item starts over."""
    stored = thread or {}
    located = bool(stored.get("evict_fp")) and _locate(items, stored.get("evict_at"), stored["evict_fp"]) is not None
    ids = set(stored.get("evict") or ()) if located else set()
    last = stored.get("evict_batch") if located else None
    batch = evict_mod.step_count(adapter, items) // settings.eviction_batch
    if batch == 0 or (last is not None and batch <= last):
        return ids, set(), None
    grown = ids | set(evict_mod.evictable(adapter, items, settings.eviction_batch, settings.eviction_keep, first))
    return grown, grown - ids, {"evict": sorted(grown), "evict_batch": batch, "evict_at": len(items) - 1,
                                "evict_fp": fingerprint(items[-1])}


def _evict_state(thread):
    """The thread entry's CONTEXT_EVICTION keys."""
    return {k: v for k, v in (thread or {}).items() if k in EVICT_KEYS}


def _clears_more(adapter, items, keep, old, new, first, inputs=0):
    """Whether bound new masks a result (or an input) of a kept item that bound old leaves whole."""
    calls = _cleared_calls(adapter, items, new, first) if inputs > 0 else set()
    return new is not None and any(_cleared(adapter, items[j], j, new, first, inputs, calls) is not items[j]
                                   for j in keep if old is None or j >= old)


def _trim_sizes(adapter, items, cut, mode, clear, first, inputs=0, old=False):
    """request_chars per item after prose stripping or masking (mode) and clearing before `clear`, cleared and masked
    inputs over `inputs` chars included. With `old` (CONTEXT_STRIP_OLD_REASONING) sizes carry thinking signatures
    (_size) and the reasoning the trim strips before strip_top is taken off."""
    sizes = [_size(old, x) for x in items]
    if mode:
        _prose_sizes(adapter, items, cut, sizes, mode, inputs, old)
    calls = _cleared_calls(adapter, items, clear, first) if inputs > 0 else set()
    for j in range(clear or 0):
        x = _cleared(adapter, items[j], j, clear, first, inputs, calls)
        if x is not items[j]:
            sizes[j] = min(sizes[j], _size(old, x))
    if old:
        for j in range(strip_top(adapter, items, old)):
            x = items[j]
            if j != cut and adapter.is_assistant(x):
                bare = _bare(adapter, x, old)
                sizes[j] = max(0, sizes[j] - _size(old, x) + (_size(old, bare) if bare is not None else 0))
    return sizes


def _strips_more(adapter, items, keep, cut, strip, new_strip):
    """Whether new_strip takes reasoning off a kept assistant item that the current view (cut, strip) still sends."""
    lo = strip if cut is not None and strip is not None else 0
    return any(j != cut and adapter.is_assistant(items[j]) and _bare(adapter, items[j], True) is not items[j]
               for j in keep if lo <= j < new_strip)


def clear_bound(adapter, items, keep_results):
    """Index of the item holding the keep_results-th newest tool result: it and every later item keep their results,
    earlier ones are cleared. None when no earlier item holds a result to clear."""
    seen = 0
    for j in range(len(items) - 1, -1, -1):
        seen += len(adapter.result_ids(items[j]) - {None})
        if seen >= keep_results:
            return j if any(adapter.result_ids(items[i]) - {None} for i in range(j)) else None
    return None


def _next_clear(adapter, items, keep_results, clear):
    """The clear bound a trim now records: the newer of the current one and clear_bound; None when clearing is off."""
    if keep_results <= 0 or not hasattr(adapter, "masked_results"):
        return None
    bounds = [b for b in (clear, clear_bound(adapter, items, keep_results)) if b is not None]
    return max(bounds) if bounds else None


def _prose_region(adapter, items, keep, bound, cut, mode=True):
    """Indices prose stripping (or masking) may touch: kept, unpinned, before bound, not the cut, and not the last
    such item (the message right before the latest run keeps its results, so no older assistant text merges into
    that run)."""
    kept = [j for j in range(len(items)) if j in keep]
    # A mid-conversation system message must follow a user message, so stripping skips the item before one.
    before_system = set() if mode == "mask" else {a for a, b in zip(kept, kept[1:]) if items[b].get("role") == "system"}
    region = [j for j in kept if j < bound and j != cut and j not in before_system and not adapter.is_pinned(items[j])]
    return region[:-1]


def prose_ids(adapter, items, keep, bound, cut, mode=True):
    """Tool call ids whose tool_use and tool_result both sit in the prose region; empty when the adapter cannot strip
    (or, mode "mask", mask)."""
    if not hasattr(adapter, "masked_results" if mode == "mask" else "without_tool_calls"):
        return set()
    region = _prose_region(adapter, items, keep, bound, cut, mode)
    calls = {c[0] for j in region for c in adapter.tool_calls(items[j])}
    results = set().union(*(adapter.result_ids(items[j]) for j in region))
    return (calls & results) - {None}


def _locate(items, idx, want, lo=0):
    """Index of the item with fingerprint `want` (idx when it still matches), searching from lo; None if absent."""
    if isinstance(idx, int) and lo <= idx < len(items) and fingerprint(items[idx]) == want:
        return idx
    return next((i for i in range(lo, len(items)) if fingerprint(items[i]) == want), None)


def _locate_cut(adapter, items, thread):
    """Index of the stored cut; else, for a cut placed on the trimming request's last message, the turn start at the
    same index after the same item: a side request (prompt suggestion, away recap) trims on its own last user
    message, which the main thread never sends."""
    cut = _locate(items, thread.get("cut"), thread.get("cut_fp"))
    idx, prev = thread.get("cut"), thread.get("prev_fp")
    if cut is None and prev and isinstance(idx, int) and 0 < idx < len(items) \
            and adapter.is_turn_start(items[idx]) and fingerprint(items[idx - 1]) == prev:
        thread["cut_fp"] = fingerprint(items[idx])
        return idx
    return cut


def region_mode(settings):
    """What the trim does to tool pairs in the prose region: "mask", True (strip) or False; masking wins."""
    return "mask" if settings.mask_observations else bool(settings.strip_prose)


def _with_progress(adapter, items, cut, gap, first, settings, note):
    """note plus the progress section for the items this trim drops, prose-strips or masks; note alone when empty."""
    if not hasattr(adapter, "tool_calls"):
        return note
    keep = kept_indices(adapter, items, cut, gap, first)
    sources = {j for j in range(len(items)) if j not in keep}
    if region_mode(settings):
        sources.update(_prose_region(adapter, items, keep, latest_run_start(adapter, items), cut, region_mode(settings)))
    text = progress_text(adapter, items, sorted(sources))
    return f"{note}\n{text}" if text else note


def progress_text(adapter, items, indices):
    """Files written or edited and the last build/test commands (with pass/fail when known) among the tool calls
    in items[indices]; "" when there are none. Mechanical, so one trim always renders the same bytes."""
    results = {}
    for x in items:
        results.update(adapter.tool_results(x))
    files, commands = {}, []
    for j in indices:
        for call_id, name, args in adapter.tool_calls(items[j]):
            for key in EDIT_TOOL_PATHS.get(name, ()):
                if isinstance(args.get(key), str) and args[key]:
                    files.pop(args[key], None)
                    files[args[key]] = True
                    break
            command = args.get("command") if name in SHELL_TOOLS else None
            if isinstance(command, str) and _BUILD_RE.search(command):
                commands.append((" ".join(command.split())[:PROGRESS_COMMAND_MAX_CHARS], results.get(call_id)))
    if not files and not commands:
        return ""
    lines = ["Progress before this trim (from the removed tool calls):"]
    if files:
        lines.append("Files written or edited (latest last): " + ", ".join(list(files)[-PROGRESS_MAX_FILES:]))
    if commands:
        lines.append("Last build/test commands (latest last):")
        lines += [f"- `{c}` -> {_command_status(r)}" for c, r in commands[-PROGRESS_MAX_COMMANDS:]]
    return "\n".join(lines)[:PROGRESS_NOTE_MAX_CHARS]


def _command_status(result):
    """"exit N" from the result text, else failed/ok by is_error; "no result" when none was sent."""
    if result is None:
        return "no result"
    is_error, text = result
    found = _EXIT_RE.search(text or "")
    if found:
        return f"exit {found.group(1)}"
    return "failed" if is_error else "ok"


def _prose_sizes(adapter, items, cut, sizes, mode=True, inputs=0, old=False):
    """Shrink sizes (request_chars per item; _size with `old`) to what prose stripping (or masking) leaves of each
    item before strip_top."""
    keep = set(range(len(items)))
    bound = strip_top(adapter, items, old)
    ids = prose_ids(adapter, items, keep, bound, cut, mode)
    if not ids:
        return
    for j in _prose_region(adapter, items, keep, bound, cut, mode):
        x = _masked_inputs(adapter, adapter.masked_results(items[j], ids), ids, inputs) if mode == "mask" \
            else adapter.without_tool_calls(items[j], ids)
        sizes[j] = _size(old, x) if x is not None else 0


def stored_rows(adapter, original, items, names, cut, view):
    """Database rows for every non-pinned message of the request, chained (sessiondb.chain).

    Text comes from the original messages, so a compressed tool result is stored in full with its blob_id. keep
    is False for rows the view does not carry: view maps each index that reaches the model to its stripped form
    (render's shown), so a dropped or emptied message and a prose-stripped tool block get False. A thinking block
    stripped from a message the view keeps stays True (_rows_kept).
    Returns (rows, cut_row), cut_row being the index of the cut turn's first row or None.
    """
    rows = []
    cut_row = None
    for j, item in enumerate(original):
        if adapter.is_pinned(item):
            continue
        base = adapter.rows(item, names)
        shown = base if items[j] is item else adapter.rows(items[j], names)
        if j == cut and base:
            cut_row = len(rows)
        keep = _rows_kept(adapter, shown, view.get(j), items[j], names)
        for k, (role, kind, name, text) in enumerate(base):
            blob = sessiondb.blob_ref(shown[k][3]) if shown is not base and k < len(shown) else None
            rows.append({"role": role, "kind": kind, "name": name, "text": text, "blob_id": blob,
                         "keep": k < len(keep) and keep[k]})
    return (sessiondb.chain(rows) if rows else rows), cut_row


def _rows_kept(adapter, shown, sent, item, names):
    """Per row of item (shown = its rows), whether sent, the form the view carries (None: absent), still has it;
    thinking rows of a sent message count as kept, so only dropping a message or its tool blocks, or masking a
    result (same row count, changed text), clears the flag."""
    if sent is item:
        return [True] * len(shown)
    if sent is None:
        return [False] * len(shown)
    left = adapter.rows(sent, names)
    if len(left) == len(shown):
        return [a == b or b[1] == "thinking" for a, b in zip(left, shown)]
    out, p = [], 0
    for row in shown:
        hit = p < len(left) and left[p] == row
        out.append(hit or row[1] == "thinking")
        p += hit
    return out


def _write_masked_blobs(store, adapter, sent, shown):
    """Write blobs/<id>.txt for each result the view shows as its mask_placeholder, and each tool-input string it
    shows as its input_placeholder, before the request leaves.

    sent: the items render saw; shown: render's index map. A compressed result's blob already exists."""
    if not hasattr(adapter, "masked_results"):
        return
    for j, x in shown.items():
        if x is sent[j]:
            continue
        now = adapter.tool_results(x)
        for rid, (_, text) in adapter.tool_results(sent[j]).items():
            if rid in now and now[rid][1] == mask_placeholder(text) and compressed_blob_id(text) is None:
                store.write_blob(blob_id(text), text)
        if not hasattr(adapter, "call_inputs"):
            continue
        shown_inputs = adapter.call_inputs(x)
        for cid, value in adapter.call_inputs(sent[j]).items():
            for text in cleared_inputs(value, shown_inputs[cid]) if cid in shown_inputs else ():
                store.write_blob(blob_id(text), text)


def _store_messages(store, sid, adapter, original, items, cut, view, settings):
    """Write the request's messages to sessions/<sid>.db. Never raises: a database error is logged and the
    request goes on."""
    path = store.db_path(sid)
    try:
        rows, cut_row = stored_rows(adapter, original, items, adapter.tool_names(items), cut, view)
        if not rows:
            return
        store.ensure_sessions()
        added = sessiondb.open_session(path).sync(rows, cut_row, _utc_now())
        if added and settings.embedding_model:
            embed_mod.embedder.notify(path, settings.embedding_model, settings.embedding_url,
                                      settings.embedding_api_key, settings.embedding_timeout_s,
                                      settings.embedding_max_chars)
    except Exception as exc:
        sessiondb.drop_session(path)
        _log_error(exc)


def _rewrite(data, call_type, settings, route, call_id):
    try:
        adapter = adapter_for(call_type, route)
    except UnsupportedCallType:
        return data
    original = adapter.get_items(data)
    if not isinstance(original, list) or not original or not all(isinstance(x, dict) for x in original):
        return data
    store = Store(settings.context_dir)

    items = original
    if settings.compress_over_tokens > 0:
        items = [adapter.compress_item(x, settings.compress_over_tokens, store.write_blob) for x in original]

    sid = resolve_session_id(data, adapter, original)
    if os.environ.get("CONTEXT_DEBUG_SHAPES") == "1":
        _log_shape(sid, call_type, data, original, adapter)
    try:
        return _rewrite_session(data, adapter, original, items, sid, store, settings, call_id)
    except Exception as exc:
        # The request goes on untrimmed (process_request logs and returns data); the session keeps the record.
        _record_event(store, sid, "fail_open", location=_where(exc), error=type(exc).__name__)
        raise


def _rewrite_session(data, adapter, original, items, sid, store, settings, call_id):
    roots = [x for x in items if not adapter.is_pinned(x)]
    if not roots:
        if settings.standing_instruction:
            add_standing_instruction(adapter, data, settings.reuse_rules)
        return data
    root = fingerprint(roots[0])
    first = first_user_index(adapter, items)
    context = [counted_tools(data.get(k)) if k == "tools" else data.get(k) for k in adapter.context_keys]
    # Usage event fields known before the call; counted on the incoming request, before any rewrite.
    request = {"model": data.get("model") if isinstance(data.get("model"), str) else None, "thread_root": root}
    if hasattr(adapter, "cache_breakpoints"):
        request["breakpoints"], request["breakpoint_at"] = adapter.cache_breakpoints(data)

    trim = None
    with session_lock(sid):
        state = store.load_trim(sid)
        threads = state["threads"]
        thread = threads.get(root)
        since, idle = None, 0
        if settings.cold_trim or settings.cache_ttl_split > 0:
            since, idle = _tick(state, root, int(_clock()))
            store.save_trim(sid, state)
        # CONTEXT_CACHE_TTL_SPLIT: the same decision on every request until the thread's Nth idle gap over 5 minutes.
        split = settings.cache_ttl_split > 0 and idle < settings.cache_ttl_split and hasattr(adapter, "split_cache_ttl")
        cold_ttl = None
        if settings.cold_trim and since is not None:
            # CONTEXT_COLD_TRIM: the conversation's cache lifetime as this request sends it, else the configured one.
            ttl_data = adapter.split_cache_ttl(data) if split else data
            cold_ttl = (adapter.cache_ttl_seconds(ttl_data) if hasattr(adapter, "cache_ttl_seconds") else None) \
                or settings.cold_trim_seconds
        log_new = []
        if settings.log_reduce_over_chars > 0:
            # CONTEXT_LOG_REDUCE_OVER_CHARS: decided on a result's first request, recorded, rendered on every later one.
            logs = state.get("logs") if isinstance(state.get("logs"), list) else []
            items, log_new = _log_reduced(adapter, original, items, logs, settings, store)
            if log_new:
                state["logs"] = (logs + log_new)[-LOG_MAX:]
                store.save_trim(sid, state)
        # CONTEXT_DEDUP_RESULTS: recorded pointers render on every request; fingerprints stay on the original items.
        # CONTEXT_LEDGER reuse pointers share the list ("kind": "reuse").
        raw = state.get("dedup") if (settings.dedup_results or settings.ledger) \
            and hasattr(adapter, "result_texts") else None
        raw = raw if isinstance(raw, list) else []
        dedup = [d for d in raw if isinstance(d, dict)
                 and (settings.ledger if d.get("kind") == "reuse" else settings.dedup_results)]
        found = _dedup_found(items, dedup) if dedup else {}
        ledgers = state.get("ledger") if settings.ledger and isinstance(state.get("ledger"), list) else []
        lfound = _dedup_found(items, ledgers) if ledgers else {}
        every = settings.strip_reasoning_every
        rbound = None
        if every > 0 and thread and thread.get("reason_fp"):
            rbound = _locate(items, thread.get("reason"), thread["reason_fp"])
        reason_located = rbound is not None or not (thread or {}).get("reason_fp")

        def make_base(decided):
            return _reason_stripped(adapter, _deduped(adapter, items, decided), rbound)

        base = make_base(found)
        cut, gap, strip, note, clear, keep, llo = None, None, None, None, None, (), None
        evicting = settings.eviction and hasattr(adapter, "masked_results") and hasattr(adapter, "tool_calls")
        nudge_at = None
        if thread and thread.get("nudge_fp"):
            nudge_at = _locate(items, thread.get("nudge_at"), thread["nudge_fp"])
        # A thread holding only a scratchpad nudge, eviction or batched reasoning state has no cut to locate.
        if thread and (thread.get("cut_fp") or not (SIDE_KEYS & thread.keys())):
            cut_fp = thread.get("cut_fp")
            cut = _locate_cut(adapter, items, thread)
            if cut is None:
                threads.pop(root, None)
                carried = {**(_evict_state(thread) if evicting else {}), **_reason_state(thread)}
                if carried:
                    threads[root] = {"format": adapter.name, "updated": _utc_now(), **carried}
                    store.save_trim(sid, state)
            else:
                if thread.get("cut_fp") != cut_fp:
                    store.save_trim(sid, state)
                note = thread.get("note")
                if thread.get("gap_fp"):
                    gap = _locate(items, thread.get("gap"), thread["gap_fp"], cut + 2)
                if thread.get("strip_fp"):
                    # From 0: a trim on the last user turn records strip as the reply before the cut.
                    strip = _locate(items, thread.get("strip"), thread["strip_fp"])
                if thread.get("clear_fp"):
                    clear = _locate(items, thread.get("clear"), thread["clear_fp"])
                if settings.keep_errors > 0 and isinstance(thread.get("keep_errors"), list):
                    keep = tuple(thread["keep_errors"])
                if settings.ledger and thread.get("ledger_from_fp"):
                    llo = _locate(items, thread.get("ledger_from"), thread["ledger_from_fp"])
        # CONTEXT_KEEP_ERRORS: the error results the last trim exempted stay whole until the next trim.
        radapter = _exempting(adapter, keep)
        shown = {}
        mode = region_mode(settings)
        evict, evicted, eviction = (_eviction(adapter, items, threads.get(root), settings, first) if evicting
                                    else (None, set(), None))
        if evict and found:
            # A result a pointer names stays shown: evicting it would leave neither copy in view.
            evict = evict - {d.get("src") for ds in found.values() for d in ds}
        if eviction:
            entry = threads.setdefault(root, {"format": adapter.name, "updated": _utc_now()})
            entry.update(eviction)
            while len(threads) > MAX_THREADS:
                threads.pop(next(iter(threads)))
            store.save_trim(sid, state)
        # The items as sent: the recorded nudge stays on its item, byte-identical, until the next trim, and so do the
        # recorded ledger blocks from llo on (CONTEXT_LEDGER).
        nudge_pos, nudge_text = nudge_at, threads.get(root, {}).get("nudge")

        def dress(b):
            return _ledgered(adapter, _nudged(adapter, b, nudge_pos, nudge_text), lfound, llo)

        sent = dress(base)
        inputs = settings.clear_inputs_over_chars
        old = settings.strip_old_reasoning
        view = render(radapter, sent, cut, note, first, gap, strip, mode, shown, clear, evict, inputs, old)
        force = store.force_requested(sid)
        ratio = settings.chars_per_token if settings.chars_per_token > 0 else DEFAULT_CHARS_PER_TOKEN
        base_chars = request_chars(*context)
        tokens = estimate_prompt_tokens(base_chars + _size(old, view), state.get("usage"), root, ratio)
        reason_event = None
        if every > 0:
            # CONTEXT_STRIP_REASONING_EVERY: at each new batch of `every` assistant steps the reasoning bound may move
            # to the latest assistant message, when that strips at least strip_reasoning_min_tokens.
            batch = evict_mod.step_count(adapter, items) // every
            last_batch = (thread or {}).get("reason_batch") if reason_located else None
            if batch > 0 and (not isinstance(last_batch, int) or batch > last_batch):
                entry = threads.setdefault(root, {"format": adapter.name, "updated": _utc_now()})
                if not reason_located:
                    for k in REASON_KEYS:
                        entry.pop(k, None)
                entry["reason_batch"] = batch
                top = latest_message_start(adapter, items)
                if top < len(items) and (rbound is None or top > rbound):
                    moved_base = _reason_stripped(adapter, base, top)
                    moved_shown = {}
                    rsent = _ledgered(adapter, _nudged(adapter, moved_base, nudge_pos, nudge_text), lfound, llo)
                    rview = render(radapter, rsent, cut, note, first, gap, strip, mode, moved_shown, clear, evict,
                                   inputs, old)
                    freed = estimate_prompt_tokens(base_chars + _size(True, view), state.get("usage"), root, ratio) \
                        - estimate_prompt_tokens(base_chars + _size(True, rview), state.get("usage"), root, ratio)
                    if moved_base is not base and freed >= settings.strip_reasoning_min_tokens:
                        entry.update(reason=top, reason_fp=fingerprint(items[top]))
                        rbound, base, sent, view, shown = top, moved_base, rsent, rview, moved_shown
                        after = estimate_prompt_tokens(base_chars + _size(old, view), state.get("usage"), root, ratio)
                        reason_event = {"tokens_before": tokens, "tokens_after": after, "dropped": 0}
                        tokens = after
                while len(threads) > MAX_THREADS:
                    threads.pop(next(iter(threads)))
                store.save_trim(sid, state)
        evict_event = None
        if evicted:
            before = render(radapter, sent, cut, note, first, gap, strip, mode, None, clear, evict - evicted, inputs,
                            old)
            evict_event = {"tokens_before": estimate_prompt_tokens(base_chars + _size(old, before),
                                                                   state.get("usage"), root, ratio),
                           "tokens_after": tokens, "dropped": len(evicted)}
        trim_at = settings.trim_at_for(request["model"])
        # CONTEXT_COLD_TRIM: the prompt cache expired since the thread's last request, so a trim to the target costs no
        # extra cache write now, below the threshold too.
        cold = cold_ttl is not None and not force and not evicting and since > cold_ttl \
            and min(settings.trim_to_for(request["model"]), trim_at) < tokens < trim_at
        # Eviction replaces the threshold trim; a forced trim (/compact) still runs.
        if force or (not evicting and tokens >= trim_at) or cold:
            new_clear = _next_clear(adapter, items, settings.clear_keep_results, clear)
            # CONTEXT_KEEP_ERRORS: the trim exempts the newest error results the view still shows whole.
            new_keep = tuple(_error_ids(adapter, items, settings.keep_errors, shown)) if settings.keep_errors > 0 \
                else ()
            tadapter = _exempting(adapter, new_keep)
            # CONTEXT_LEDGER: a trim keeps only the newest recorded ledger block.
            new_llo = max(lfound) if lfound else llo
            tbase = _ledgered(adapter, base, lfound, new_llo)
            # Reasoning the batched strip already took off counts as gone (CONTEXT_STRIP_REASONING_EVERY).
            ritems = _reason_stripped(adapter, items, rbound)
            # A target at or above the threshold could not land a trim under it, so the threshold caps it.
            target = min(settings.trim_to_for(request["model"]), trim_at)
            new_cut, new_gap = None, None
            anchor, anchor_gap = (cut, gap) if cut is not None else (first, None)
            # With `old`, stripping earlier reasoning settles a trim like clearing; a forced trim still drops turns.
            strips = old and not force and first is not None and _strips_more(
                adapter, ritems, kept_indices(adapter, items, anchor, anchor_gap, first), cut, strip,
                strip_top(adapter, items, old))
            if (new_clear is not None or strips) and first is not None:
                # Clearing (and stripping) first: the current cut, or the first user turn (dropping nothing).
                sizes = _trim_sizes(tadapter, base, anchor, mode, new_clear, first, inputs, old)
                chars = base_chars + sum(sizes[j] for j in kept_indices(adapter, items, anchor, anchor_gap, first))
                if estimate_prompt_tokens(chars, state.get("usage"), root, ratio) < target:
                    new_cut, new_gap = anchor, anchor_gap
            if new_cut is None:
                new_cut = choose_cut(adapter, items, -1 if cut is None else cut, first)
                if new_cut is None:
                    new_cut = cut if cut is not None else last_turn_index(adapter, items)
                if new_cut is not None:
                    sizes = _trim_sizes(tadapter, base, new_cut, mode, new_clear, first, inputs, old)

                    def fits(g):
                        chars = base_chars + sum(sizes[j] for j in kept_indices(adapter, items, new_cut, g, first))
                        return estimate_prompt_tokens(chars, state.get("usage"), root, ratio) < target

                    after = gap if new_cut == cut and gap is not None else new_cut + 1
                    new_gap = choose_gap(adapter, items, new_cut, after, first, fits)
                    if new_gap is None and new_cut == cut:
                        new_gap = gap
            dropped = len(_droppable(adapter, items, new_cut, new_gap, first)) if new_cut is not None else 0
            moved = bool(dropped) and (new_cut, new_gap) != (cut, gap)
            new_strip = strip_top(adapter, items, old)
            if new_cut is not None and not moved:
                # A newer clear bound alone moves the trim when it masks a result the current bound left whole.
                kept = kept_indices(adapter, items, new_cut, new_gap, first)
                moved = _clears_more(tadapter, base, kept, clear, new_clear, first, inputs)
                if not moved and old:
                    # So does a strip bound that takes reasoning off a message the current view still sends.
                    moved = _strips_more(adapter, ritems, kept, cut, strip, new_strip)
            if moved:
                new_shown = {}
                bare = render(tadapter, tbase, new_cut, "", first, new_gap, new_strip, mode, new_shown, new_clear,
                              evict, inputs, old)
                gain = max(settings.trim_min_gain_tokens, settings.cold_trim_min_gain_tokens) if cold \
                    else settings.trim_min_gain_tokens
                if not force and gain > 0:
                    # A cut freeing fewer estimated tokens (pointer note not counted) waits for a bigger one.
                    freed = tokens - estimate_prompt_tokens(base_chars + _size(old, bare), state.get("usage"), root, ratio)
                    moved = freed >= gain
            if moved:
                # The pointer counts every message absent from the view: dropped, or emptied by the strip.
                dropped = len(items) - len(new_shown)
                names = recall_tool_names(request_tool_names(data))
                masked = (mode == "mask" or new_clear is not None) and any(
                    x is not tbase[j] and (MASK_PREFIX in str(x.get("content")) or INPUT_MASK_PREFIX in str(x))
                    for j, x in new_shown.items())
                if old and not dropped and not masked:
                    # Stripping earlier reasoning alone drops and masks nothing.
                    note = reasoning_text(sid)
                elif dropped or not masked:
                    note = pointer_text(sid, dropped, names)
                    if settings.progress_note:
                        note = _with_progress(adapter, items, new_cut, new_gap, first, settings, note)
                    if masked:
                        note = f"{note}\n{mask_text(sid, names)}"
                else:
                    # Clearing alone drops no message: the note only says how to fetch a cleared result back.
                    note = f"[smarter-compact: session {sid}] {mask_text(sid, names)}"
                pad = store.read_scratchpad(sid) if settings.scratchpad else ""
                if pad:
                    note = f"{scratchpad_block(pad)}\n{note}"  # first, ahead of the recall instructions
                previous = threads.pop(root, None)
                carried = {**_evict_state(previous), **_reason_state(previous)}
                threads[root] = {"cut": new_cut, "cut_fp": fingerprint(items[new_cut]), "note": note,
                                 "format": adapter.name, "updated": _utc_now(), **carried}
                if new_keep:
                    threads[root]["keep_errors"] = list(new_keep)
                if new_llo is not None and settings.ledger:
                    threads[root].update(ledger_from=new_llo, ledger_from_fp=fingerprint(items[new_llo]))
                if 0 < new_cut == len(items) - 1:  # cut on the last message: how a side request trims
                    threads[root]["prev_fp"] = fingerprint(items[new_cut - 1])
                if new_gap is not None:
                    threads[root].update(gap=new_gap, gap_fp=fingerprint(items[new_gap]))
                strip = new_strip
                if strip < len(items):
                    threads[root].update(strip=strip, strip_fp=fingerprint(items[strip]))
                if new_clear is not None:
                    threads[root].update(clear=new_clear, clear_fp=fingerprint(items[new_clear]))
                while len(threads) > MAX_THREADS:
                    threads.pop(next(iter(threads)))
                store.save_trim(sid, state)
                if force:
                    store.clear_force(sid)
                shown = {}
                view = render(tadapter, tbase, new_cut, note, first, new_gap, strip, mode, shown, new_clear, evict,
                              inputs, old)
                cut, gap, clear = new_cut, new_gap, new_clear
                after = estimate_prompt_tokens(base_chars + _size(old, view), state.get("usage"), root, ratio)
                trim = {"tokens_before": tokens, "tokens_after": after, "dropped": dropped}
                if cold:
                    trim["location"] = "cold"
                if settings.scratchpad:
                    trim["scratchpad_chars"] = min(len(pad), SCRATCHPAD_MAX_CHARS)
                sent, radapter, nudge_pos, llo = tbase, tadapter, None, new_llo
            elif thread and cut is None and root not in threads:
                store.save_trim(sid, state)
        elif thread and cut is None and root not in threads:
            store.save_trim(sid, state)
        nudge = None
        pad_names = recall_tool_names(request_tool_names(data)) if settings.scratchpad else None
        # No nudge while evicting: its text announces a trim that eviction never makes.
        if settings.scratchpad and not evicting and not trim and not (threads.get(root) or {}).get("nudge_fp") \
                and tokens >= trim_at * settings.scratchpad_nudge_at and scratchpad_available(pad_names, data):
            text = scratchpad_nudge(sid, pad_names)
            if _nudged(adapter, base, len(items) - 1, text) is not base:
                # Recorded on the newest item, so every later request re-renders it unchanged.
                entry = threads.setdefault(root, {"format": adapter.name, "updated": _utc_now()})
                entry.update(nudge=text, nudge_at=len(items) - 1, nudge_fp=fingerprint(items[-1]))
                while len(threads) > MAX_THREADS:
                    threads.pop(next(iter(threads)))
                store.save_trim(sid, state)
                nudge_pos, nudge_text = len(items) - 1, text
                sent, shown = dress(base), {}
                view = render(radapter, sent, cut, note, first, gap, strip, mode, shown, clear, evict, inputs, old)
                nudge = {"tokens_before": tokens}
        new = _dedup_new(adapter, items, shown, found) if settings.dedup_results and hasattr(adapter, "result_texts") \
            else []
        lstate, reuse, lnudges = _ledger_new(adapter, items, shown, found, new) if settings.ledger \
            and hasattr(adapter, "result_texts") else (None, [], [])
        if new or reuse:
            # Recorded before the request leaves, so every later request renders the same pointer.
            for d, text in new + reuse:
                if compressed_blob_id(text) is None:
                    store.write_blob(d["bid"], text)
            state["dedup"] = (raw + [d for d, _ in new + reuse])[-DEDUP_MAX:]
            store.save_trim(sid, state)
            if settings.ledger:
                # Rebuilt in the order every later request renders: pointers, then the nudge and ledger blocks.
                found = _dedup_found(items, dedup + [d for d, _ in new + reuse])
                base = make_base(found)
                sent, shown = dress(base), {}
            else:
                sent, shown = _deduped(adapter, sent, _dedup_found(items, [d for d, _ in new])), {}
            view = render(radapter, sent, cut, note, first, gap, strip, mode, shown, clear, evict, inputs, old)
            if new:
                _record_event(store, sid, "dedup", dropped=len(new))
            if reuse:
                _record_event(store, sid, "reuse", dropped=len(reuse))
        entry = _ledger_block(adapter, items, sent, lstate, lnudges, lfound) if settings.ledger else None
        if entry:
            # Recorded on the newest item, so every later request re-renders the block unchanged.
            state["ledger"] = (ledgers + [entry])[-ledger_mod.LEDGER_MAX:]
            store.save_trim(sid, state)
            lfound = {**lfound, entry["at"]: [entry]}
            sent, shown = dress(base), {}
            view = render(radapter, sent, cut, note, first, gap, strip, mode, shown, clear, evict, inputs, old)
        _write_masked_blobs(store, adapter, sent, shown)
        _store_messages(store, sid, adapter, original, sent, cut, shown, settings)
        if trim:
            _record_event(store, sid, "trim", **trim)
        if nudge:
            _record_event(store, sid, "nudge", **nudge)
        if evict_event:
            _record_event(store, sid, "evict", **evict_event)
        if reason_event:
            _record_event(store, sid, "strip_batch", **reason_event)
        if log_new:
            _record_event(store, sid, "log_reduce", dropped=len(log_new))
    request["trimmed"] = TRIM_NEW if trim else TRIM_KEPT if cut is not None else TRIM_NONE

    # Measured like the estimate, before the standing instruction, so calibration compares like with like.
    _await_usage(data.get("litellm_call_id") or call_id, sid, root,
                 request_chars(*context) + _size(settings.strip_old_reasoning, view), request)
    if not (len(view) == len(original) and all(a is b for a, b in zip(view, original))):
        data[adapter.items_key] = view
    if settings.server_clear and isinstance(adapter, AnthropicAdapter) and _is_claude(data.get("model")):
        add_server_clear(data, settings)
    if settings.standing_instruction:
        # After trimming, so the cut, the token estimate and the session id never see it.
        add_standing_instruction(adapter, data, settings.reuse_rules)
    if split:
        _split_ttl(adapter, data)
    return data


def _await_usage(call_id, sid, root, chars, request=None):
    if not isinstance(call_id, str) or not call_id:
        return
    with _pending_guard:
        _pending.pop(call_id, None)
        _pending[call_id] = (sid, root, chars, request or {})
        while len(_pending) > MAX_PENDING:
            _pending.popitem(last=False)


def add_server_clear(data, settings):
    """Append a clear_tool_uses_20250919 edit to an Anthropic request's context_management, after any edits the
    client sent (Claude Code sends clear_thinking_20251015, which must stay first); a client edit of the same type
    wins. LiteLLM adds the context-management-2025-06-27 beta header for a body that carries the field."""
    current = data.get("context_management")
    if current is not None and not (isinstance(current, dict) and isinstance(current.get("edits", []), list)):
        return
    edits = list((current or {}).get("edits", []))
    if any(isinstance(e, dict) and e.get("type") == SERVER_CLEAR_EDIT for e in edits):
        return
    edit = {"type": SERVER_CLEAR_EDIT,
            "trigger": {"type": "input_tokens", "value": settings.server_clear_trigger_tokens},
            "keep": {"type": "tool_uses", "value": settings.server_clear_keep_tool_uses}}
    if settings.server_clear_at_least_tokens > 0:
        edit["clear_at_least"] = {"type": "input_tokens", "value": settings.server_clear_at_least_tokens}
    data["context_management"] = {**(current or {}), "edits": edits + [edit]}


def _is_claude(model):
    return isinstance(model, str) and "claude" in model.lower()


def applied_edits(response):
    """Compact JSON of a response's context_management.applied_edits (Anthropic body, LiteLLM's
    provider_specific_fields, or those of a ModelResponse's first message), else None."""
    choices = _field(response, "choices")
    message = _field(choices[0], "message") if isinstance(choices, list) and choices else None
    for obj in (response, _field(response, "provider_specific_fields"), _field(message, "provider_specific_fields")):
        edits = _field(_field(obj, "context_management"), "applied_edits") if obj is not None else None
        if isinstance(edits, list):
            return json.dumps(edits, separators=(",", ":"), sort_keys=True)
    return None


def stream_applied_edits(tail):
    """applied_edits of the last message_delta event in an Anthropic SSE stream's tail (bytes or str), else None."""
    text = tail.decode("utf-8", "replace") if isinstance(tail, (bytes, bytearray)) else str(tail)
    found = None
    for line in text.splitlines():
        if not line.startswith("data:") or "context_management" not in line:
            continue
        try:
            event = json.loads(line[5:])
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "message_delta":
            found = applied_edits(event) or found
    return found


def note_stream_edits(call_id, tail):
    """Hold a finished stream's applied context edits for its usage event; nothing when it reported none."""
    edits = stream_applied_edits(tail) if isinstance(call_id, str) and call_id else None
    if edits is None:
        return
    with _pending_guard:
        _stream_edits[call_id] = edits
        while len(_stream_edits) > MAX_PENDING:
            _stream_edits.popitem(last=False)


def _field(obj, name):
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _count(value):
    return value if type(value) is int and value >= 0 else None


def prompt_tokens_from_usage(usage):
    """Prompt tokens in a usage object, cache reads and writes included, or None.

    LiteLLM Usage (chat, and /v1/messages logged as a ModelResponse) has prompt_tokens, which already includes
    Anthropic cache tokens. Anthropic's own usage has input_tokens without them plus cache_read_input_tokens and
    cache_creation_input_tokens. Responses API usage has input_tokens, cached tokens included.
    """
    if usage is None:
        return None
    prompt = _count(_field(usage, "prompt_tokens"))
    if prompt:
        return prompt
    base = _count(_field(usage, "input_tokens"))
    if base is None:
        return None
    total = base + (_count(_field(usage, "cache_read_input_tokens")) or 0) \
        + (_count(_field(usage, "cache_creation_input_tokens")) or 0)
    return total or None


def usage_counts(usage):
    """{input_tokens, cache_read_tokens, cache_creation_tokens, output_tokens} from a usage object; a count the
    provider did not report is None. input_tokens excludes cache reads and writes.

    LiteLLM Usage: prompt_tokens (cache included), cache_read_input_tokens / cache_creation_input_tokens (Anthropic),
    else prompt_tokens_details.cached_tokens / cache_creation_tokens (alias cache_write_tokens), and completion_tokens. Anthropic's own usage:
    input_tokens without cache, cache_read_input_tokens, cache_creation_input_tokens, output_tokens. Responses API:
    input_tokens with cache, input_tokens_details.cached_tokens, output_tokens.
    """
    if usage is None:
        return None
    details = _field(usage, "prompt_tokens_details")
    read = _count(_field(usage, "cache_read_input_tokens"))
    creation = _count(_field(usage, "cache_creation_input_tokens"))
    prompt = _count(_field(usage, "prompt_tokens"))
    if prompt is not None:
        if read is None and details is not None:
            read = _count(_field(details, "cached_tokens"))
        if creation is None and details is not None:
            creation = _count(_field(details, "cache_creation_tokens"))
            if creation is None:
                creation = _count(_field(details, "cache_write_tokens"))
        uncached = prompt - (read or 0) - (creation or 0)
        out = {"input_tokens": uncached if uncached >= 0 else None, "output_tokens": _count(_field(usage, "completion_tokens"))}
    else:
        base = _count(_field(usage, "input_tokens"))
        if read is None and creation is None and base is not None:
            details = _field(usage, "input_tokens_details")
            read = _count(_field(details, "cached_tokens")) if details is not None else None
            if read is not None:
                base = base - read if base >= read else None
        out = {"input_tokens": base, "output_tokens": _count(_field(usage, "output_tokens"))}
    out.update(cache_read_tokens=read, cache_creation_tokens=creation)
    return out if any(v is not None for v in out.values()) else None


def response_usage_counts(response, kwargs):
    """usage_counts of a finished call's response (or its .response, for a Responses stream's completed event),
    else standard_logging_object's prompt and completion tokens; all None when none is reported."""
    for obj in (response, _field(response, "response")):
        if obj is not None:
            counts = usage_counts(_field(obj, "usage"))
            if counts:
                return counts
    slo = kwargs.get("standard_logging_object") if isinstance(kwargs, dict) else None
    counts = usage_counts({"prompt_tokens": _field(slo, "prompt_tokens"), "completion_tokens": _field(slo, "completion_tokens")}) \
        if slo is not None else None
    return counts or dict.fromkeys(USAGE_FIELDS)


def cache_write_split(response):
    """{cache_creation_1h_tokens, cache_creation_5m_tokens} from the response usage's cache_creation object
    (Anthropic) or LiteLLM's prompt_tokens_details.cache_creation_token_details, else {}."""
    for obj in (response, _field(response, "response")):
        usage = _field(obj, "usage") if obj is not None else None
        split = _field(usage, "cache_creation") if usage is not None else None
        if split is None and usage is not None:
            # LiteLLM's ModelResponse keeps it at prompt_tokens_details.cache_creation_token_details.
            split = _field(_field(usage, "prompt_tokens_details"), "cache_creation_token_details")
        if split is not None:
            return {"cache_creation_1h_tokens": _count(_field(split, "ephemeral_1h_input_tokens")),
                    "cache_creation_5m_tokens": _count(_field(split, "ephemeral_5m_input_tokens"))}
    return {}


def response_prompt_tokens(response, kwargs):
    """Prompt tokens LiteLLM reports for a finished call: the response's usage (or its .response's, for a Responses
    stream's completed event), else standard_logging_object.prompt_tokens; None when none is reported."""
    for obj in (response, _field(response, "response")):
        if obj is not None:
            tokens = prompt_tokens_from_usage(_field(obj, "usage"))
            if tokens:
                return tokens
    slo = kwargs.get("standard_logging_object") if isinstance(kwargs, dict) else None
    tokens = _count(_field(slo, "prompt_tokens")) if slo is not None else None
    return tokens or None


def process_usage(kwargs, response, settings=None):
    """Record a finished call's usage event and calibrate its session from the reported prompt tokens. Never raises."""
    try:
        call_id = kwargs.get("litellm_call_id") if isinstance(kwargs, dict) else None
        if not isinstance(call_id, str):
            return
        with _pending_guard:
            entry = _pending.pop(call_id, None)
        if entry is None:
            return
        settings = settings or Settings.from_env()
        if not settings.enabled:
            return
        sid, root, chars, request = entry
        with _pending_guard:
            streamed = _stream_edits.pop(call_id, None)
        store = Store(settings.context_dir)
        _record_event(store, sid, "usage", **request, **response_usage_counts(response, kwargs),
                      **cache_write_split(response), applied_edits=applied_edits(response) or streamed)
        tokens = response_prompt_tokens(response, kwargs)
        if tokens is None:
            return
        with session_lock(sid):
            state = store.load_trim(sid)
            if record_usage(state, root, chars, tokens):
                store.save_trim(sid, state)
    except Exception as exc:
        _log_error(exc)


def startup(settings=None):
    """Proxy start: status file, then TTL cleanup of old sessions (at most once a day), on a daemon thread."""
    def run():
        try:
            s = settings or Settings.from_env()
            status_mod.write(s.context_dir)
            if s.enabled:
                ttl_mod.maybe_sweep(s.context_dir, s.ttl_days)
        except Exception as exc:
            _log_error(exc)
    thread = threading.Thread(target=run, name="smarter-compact-ttl", daemon=True)
    thread.start()
    return thread


def process_request(data, call_type, settings=None, route=None, call_id=None):
    """Rewrite data in place and return it; on any error return it unmodified.

    route is the request path (selects the adapter for a pass-through call). call_id is the litellm_call_id when
    data does not carry one: a pass-through body is forwarded upstream as is, so the id stays out of it.
    """
    try:
        settings = settings or Settings.from_env()
        status_mod.write(settings.context_dir, request=True)
        if not settings.enabled or not isinstance(data, dict):
            return data
        return _rewrite(data, call_type, settings, route, call_id)
    except Exception as exc:
        _log_error(exc)
        return data
