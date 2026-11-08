"""Per-trial and per-arm report for a Harbor job of ScClaude arms, with the long-task subset.

  python3 report.py JOB_DIR [--trim-at 95000]

The subset keeps tasks whose native (Baseline) peak prompt reached --trim-at: chosen by the arm the
treatment cannot influence, so both arms are compared on the same tasks.
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import sqlite3

ARMS = ("Smart", "Mask-run", "Mask-10", "ServerClear", "Native")
# Agent class -> arm label (report names only: ScClaudeMask masks older turns, ScClaudeClear all but the newest
# 10 results); any other ScClaude<Name> class reports as <Name> (NoStrip, Prose, Trim50, ...).
ARM_LABELS = {"ScClaudeSmarterCompact": "Smart", "ScClaudeMask": "Mask-run", "ScClaudeServerClear": "ServerClear",
              "ScClaudeClear": "Mask-10", "ScClaudeBaseline": "Native"}
# The scratchpad tool (CONTEXT_SCRATCHPAD); its calls count apart from recall.
SCRATCHPAD_TOOL = "smart_compact_scratchpad"
# Start of core.mask_text, which a masking trim appends to its pointer note.
MASK_NOTE = "Older tool outputs in your context read"


def arm_of(trial):
    cls = json.load(open(os.path.join(trial, "config.json")))["agent"]["import_path"].rsplit(":", 1)[-1]
    return ARM_LABELS.get(cls) or cls.removeprefix("ScClaude")


def arms_in(rows):
    """The ARMS present first, then every other arm present, in first-seen order."""
    present = [r["arm"] for r in rows]
    return list(dict.fromkeys([*(a for a in ARMS if a in present), *present]))


def stream_stats(path):
    """Peak prompt (input + cache read + cache write of one request), recall calls and errors, connection
    refusals, Claude Code compactions, whether the init event offered a smart_compact tool, and scratchpad calls."""
    peak = recall = recall_err = econn = compacts = pad_calls = 0
    offered = False
    ids = set()
    if not os.path.exists(path):
        return peak, recall, recall_err, econn, compacts, offered, pad_calls
    for line in open(path, errors="ignore"):
        if "ECONNREFUSED" in line:
            econn += 1
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("type") == "system" and msg.get("subtype") == "compact_boundary":
            compacts += 1
        if msg.get("type") == "system" and msg.get("subtype") == "init":
            offered = offered or any("smart_compact" in t for t in msg.get("tools") or [] if isinstance(t, str))
        body = msg.get("message") if isinstance(msg.get("message"), dict) else {}
        usage = body.get("usage") or {}
        if msg.get("type") == "assistant" and usage:
            peak = max(peak, sum(usage.get(k) or 0 for k in
                                 ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
        for block in body.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name", "").endswith(SCRATCHPAD_TOOL):
                pad_calls += 1
            elif block.get("type") == "tool_use" and "smart_compact" in block.get("name", ""):
                recall += 1
                ids.add(block["id"])
            if block.get("type") == "tool_result" and block.get("tool_use_id") in ids and block.get("is_error"):
                recall_err += 1
    return peak, recall, recall_err, econn, compacts, offered, pad_calls


def trim_stats(trial):
    """Trims, turns dropped, scratchpad nudges sent, and trims whose note carried a pad (0 on pre-scratchpad DBs)."""
    trims = dropped = nudges = padded = 0
    for db in glob.glob(os.path.join(trial, "agent/smarter-compact/context/sessions/*.db")):
        conn = sqlite3.connect(db)
        try:
            row = conn.execute("SELECT count(*), coalesce(sum(dropped), 0) FROM events WHERE kind = 'trim'").fetchone()
            trims, dropped = trims + row[0], dropped + row[1]
            nudges += conn.execute("SELECT count(*) FROM events WHERE kind = 'nudge'").fetchone()[0]
            if any(c[1] == "scratchpad_chars" for c in conn.execute("PRAGMA table_info(events)")):
                padded += conn.execute("SELECT count(*) FROM events WHERE kind = 'trim' AND scratchpad_chars > 0"
                                       ).fetchone()[0]
        finally:
            conn.close()
    return trims, dropped, nudges, padded


def partial(trial):
    """(passed, total) verifier tests from the trial's CTRF report; None without one."""
    try:
        s = json.load(open(os.path.join(trial, "verifier/ctrf.json")))["results"]["summary"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return (s.get("passed", 0), s.get("tests", 0)) if s.get("tests") else None


def mechanism(trial, arm):
    """Whether the arm's own mechanism fired: "yes", "no", or "-" for arms without one.

    Mask-10: a trim set a clear bound (trim.json thread "clear_fp"). Mask-run: a trim note carries the mask recovery text.
    ServerClear: Anthropic reported a non-empty context_management.applied_edits on some request.
    Scratchpad: a trim note carried the agent's pad. ScratchpadClear: both the pad and a Mask-10 clear bound."""
    ctx = os.path.join(trial, "agent/smarter-compact/context/sessions")
    if arm in ("Mask-10", "Mask-run"):
        for path in glob.glob(os.path.join(ctx, "*.trim.json")):
            try:
                threads = (json.load(open(path)).get("threads") or {}).values()
            except (OSError, ValueError):
                continue
            if any(t.get("clear_fp") if arm == "Mask-10" else MASK_NOTE in (t.get("note") or "") for t in threads):
                return "yes"
        return "no"
    if arm == "ServerClear":
        for db in glob.glob(os.path.join(ctx, "*.db")):
            conn = sqlite3.connect(db)
            try:
                if conn.execute("SELECT 1 FROM events WHERE kind = 'usage' AND applied_edits NOT IN ('', '[]') "
                                "LIMIT 1").fetchone():
                    return "yes"
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        return "no"
    if arm == "Scratchpad":
        return "yes" if trim_stats(trial)[3] else "no"
    if arm == "ScratchpadClear":
        return "yes" if trim_stats(trial)[3] and mechanism(trial, "Mask-10") == "yes" else "no"
    return "-"


def load(job):
    rows, running = [], []
    for trial in sorted(glob.glob(os.path.join(job, "*__*/"))):
        arm, task = arm_of(trial), os.path.basename(trial.rstrip("/")).split("__")[0]
        try:
            result = json.load(open(os.path.join(trial, "result.json")))
        except (OSError, ValueError):
            running.append(f"{arm}:{task}")
            continue
        agent = result.get("agent_result") or {}
        peak, recall, recall_err, econn, compacts, offered, pad_calls = stream_stats(
            os.path.join(trial, "agent/claude-code.txt"))
        trims, dropped, nudges, padded = trim_stats(trial)
        watchdog = os.path.join(trial, "agent/proxy-watchdog.log")
        rows.append({
            "arm": arm, "task": task,
            "reward": ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward"),
            "exc": (result.get("exception_info") or {}).get("exception_type") or "",
            "in": agent.get("n_input_tokens") or 0, "out": agent.get("n_output_tokens") or 0,
            "cost": agent.get("cost_usd") or 0.0, "peak": peak, "trims": trims, "dropped": dropped,
            "recall": recall, "recall_err": recall_err, "econn": econn, "compacts": compacts, "offered": offered,
            "nudges": nudges, "pad_calls": pad_calls, "padded": padded, "mech": mechanism(trial, arm),
            "restarts": open(watchdog).read().count("restarting") if os.path.exists(watchdog) else 0,
            "partial": partial(trial),
        })
    return rows, running


def totals(rows):
    out = collections.defaultdict(collections.Counter)
    for r in rows:
        t = out[r["arm"]]
        t["n"] += 1
        t["pass"] += r["reward"] == 1.0
        for k in ("in", "out", "cost", "trims", "recall", "recall_err", "restarts", "compacts", "nudges", "pad_calls",
                  "padded"):
            t[k] += r[k]
        t["trimmed_tasks"] += r["trims"] > 0
        t["econn_trials"] += r["econn"] > 0
        t["offered"] += r["offered"]
        if r["partial"]:
            t["part_sum"] += r["partial"][0] / r["partial"][1]
            t["part_n"] += 1
    return out


def has_scratchpad(rows):
    """Whether any trial saw the scratchpad: its columns print only then."""
    return any(r["nudges"] or r["pad_calls"] or r["padded"] for r in rows)


def print_totals(title, rows, arms=None, pad=False):
    print(f"\n{title}")
    print(f"{'arm':11} {'pass/n':>7} {'part':>5} {'input':>11} {'output':>9} {'cost':>8} {'trims':>6} {'trimmed':>8} "
          f"{'recall':>7} {'rec_err':>8} {'restarts':>9} {'econn':>6} {'compacts':>9} {'tools':>6}"
          + (f" {'nudges':>6} {'padcall':>7} {'padded':>6}" if pad else ""))
    t = totals(rows)
    for arm in arms or arms_in(rows):
        a = t[arm]
        part = f"{a['part_sum'] / a['part_n']:.2f}" if a["part_n"] else "-"
        print(f"{arm:11} {a['pass']:>3}/{a['n']:<3} {part:>5} {a['in']:>11,} {a['out']:>9,} {a['cost']:>8.3f} {a['trims']:>6} "
              f"{a['trimmed_tasks']:>8} {a['recall']:>7} {a['recall_err']:>8} {a['restarts']:>9} {a['econn_trials']:>6} "
              f"{a['compacts']:>9} {a['offered']:>6}"
              + (f" {a['nudges']:>6} {a['pad_calls']:>7} {a['padded']:>6}" if pad else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("--trim-at", type=int, default=95000)
    args = ap.parse_args()
    rows, running = load(args.job)
    pad = has_scratchpad(rows)

    # pad: scratchpad nudges sent / scratchpad tool calls / trims that carried a pad.
    # part: verifier tests passed / run (CTRF); the mean of that fraction in the totals.
    print(f"{'task':34} {'arm':11} {'rew':>4} {'part':>5} {'peak':>8} {'input':>10} {'output':>8} {'cost':>7} "
          f"{'trims':>5} {'drop':>5} {'recall':>6} " + (f"{'pad':>7} " if pad else "") + f"{'mech':>4} exc")
    for r in sorted(rows, key=lambda r: (r["task"], r["arm"])):
        cell = f"{r['nudges']}/{r['pad_calls']}/{r['padded']}"
        part = f"{r['partial'][0]}/{r['partial'][1]}" if r["partial"] else "-"
        print(f"{r['task'][:34]:34} {r['arm']:11} {r['reward'] if r['reward'] is not None else '-':>4} {part:>5} {r['peak']:>8,} "
              f"{r['in']:>10,} {r['out']:>8,} {r['cost']:>7.3f} {r['trims']:>5} {r['dropped']:>5} {r['recall']:>6} "
              + (f"{cell:>7} " if pad else "") + f"{r['mech']:>4} {r['exc']}")

    both = {r["task"] for r in rows if r["arm"] == "Smart"} & {r["task"] for r in rows if r["arm"] == "Native"}
    paired = [r for r in rows if r["task"] in both]
    long_tasks = {r["task"] for r in paired if r["arm"] == "Native" and r["peak"] >= args.trim_at}
    trimmed = {r["task"] for r in paired if r["arm"] == "Smart" and r["trims"]}
    arms = arms_in(rows)
    print_totals(f"All finished trials ({len(rows)})", rows, arms, pad=pad)
    print_totals(f"Paired tasks, both arms finished ({len(both)})", paired, arms, pad=pad)
    print_totals(f"Long tasks: native peak >= {args.trim_at:,} ({len(long_tasks)}: {', '.join(sorted(long_tasks))})",
                 [r for r in paired if r["task"] in long_tasks], arms, pad=pad)
    print_totals(f"Check: long or smart trimmed ({len(long_tasks | trimmed)})",
                 [r for r in paired if r["task"] in long_tasks | trimmed], arms, pad=pad)
    # The arms share the request until the first trim, so trimming is not yet the treatment: masking vs plain trimming.
    cut = [r for r in rows if r["trims"]]
    print_totals(f"Trimmed trials only ({len(cut)}; Mask-* rows include trims that masked nothing)", cut,
                 [a for a in arms if any(r["arm"] == a for r in cut)], pad=pad)
    print(f"\nrunning: {', '.join(running) or 'none'}")
    missed = [f"{r['arm']}:{r['task']}" for r in rows if r["mech"] == "no"]
    print(f"mechanism did not fire: {', '.join(missed) or 'none'}")


if __name__ == "__main__":
    main()
