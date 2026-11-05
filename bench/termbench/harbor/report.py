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

ARMS = ("Smart", "Mask", "Native")


def arm_of(trial):
    path = json.load(open(os.path.join(trial, "config.json")))["agent"]["import_path"]
    return "Smart" if "Smarter" in path else "Mask" if "Mask" in path else "Native"


def stream_stats(path):
    """Peak prompt (input + cache read + cache write of one request), recall calls and errors."""
    peak = recall = recall_err = econn = 0
    ids = set()
    if not os.path.exists(path):
        return peak, recall, recall_err, econn
    for line in open(path, errors="ignore"):
        if "ECONNREFUSED" in line:
            econn += 1
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        body = msg.get("message") if isinstance(msg.get("message"), dict) else {}
        usage = body.get("usage") or {}
        if msg.get("type") == "assistant" and usage:
            peak = max(peak, sum(usage.get(k) or 0 for k in
                                 ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")))
        for block in body.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and "smart_compact" in block.get("name", ""):
                recall += 1
                ids.add(block["id"])
            if block.get("type") == "tool_result" and block.get("tool_use_id") in ids and block.get("is_error"):
                recall_err += 1
    return peak, recall, recall_err, econn


def trim_stats(trial):
    trims = dropped = 0
    for db in glob.glob(os.path.join(trial, "agent/smarter-compact/context/sessions/*.db")):
        row = sqlite3.connect(db).execute(
            "SELECT count(*), coalesce(sum(dropped), 0) FROM events WHERE kind = 'trim'").fetchone()
        trims, dropped = trims + row[0], dropped + row[1]
    return trims, dropped


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
        peak, recall, recall_err, econn = stream_stats(os.path.join(trial, "agent/claude-code.txt"))
        trims, dropped = trim_stats(trial)
        watchdog = os.path.join(trial, "agent/proxy-watchdog.log")
        rows.append({
            "arm": arm, "task": task,
            "reward": ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward"),
            "exc": (result.get("exception_info") or {}).get("exception_type") or "",
            "in": agent.get("n_input_tokens") or 0, "out": agent.get("n_output_tokens") or 0,
            "cost": agent.get("cost_usd") or 0.0, "peak": peak, "trims": trims, "dropped": dropped,
            "recall": recall, "recall_err": recall_err, "econn": econn,
            "restarts": open(watchdog).read().count("restarting") if os.path.exists(watchdog) else 0,
        })
    return rows, running


def totals(rows):
    out = collections.defaultdict(collections.Counter)
    for r in rows:
        t = out[r["arm"]]
        t["n"] += 1
        t["pass"] += r["reward"] == 1.0
        for k in ("in", "out", "cost", "trims", "recall", "recall_err", "restarts"):
            t[k] += r[k]
        t["trimmed_tasks"] += r["trims"] > 0
        t["econn_trials"] += r["econn"] > 0
    return out


def print_totals(title, rows):
    print(f"\n{title}")
    print(f"{'arm':7} {'pass/n':>7} {'input':>11} {'output':>9} {'cost':>8} {'trims':>6} {'trimmed':>8} "
          f"{'recall':>7} {'rec_err':>8} {'restarts':>9} {'econn':>6}")
    t = totals(rows)
    for arm in ARMS:
        a = t[arm]
        print(f"{arm:7} {a['pass']:>3}/{a['n']:<3} {a['in']:>11,} {a['out']:>9,} {a['cost']:>8.3f} {a['trims']:>6} "
              f"{a['trimmed_tasks']:>8} {a['recall']:>7} {a['recall_err']:>8} {a['restarts']:>9} {a['econn_trials']:>6}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("job")
    ap.add_argument("--trim-at", type=int, default=95000)
    args = ap.parse_args()
    rows, running = load(args.job)

    print(f"{'task':34} {'arm':6} {'rew':>4} {'peak':>8} {'input':>10} {'output':>8} {'cost':>7} "
          f"{'trims':>5} {'drop':>5} {'recall':>6} exc")
    for r in sorted(rows, key=lambda r: (r["task"], r["arm"])):
        print(f"{r['task'][:34]:34} {r['arm']:6} {r['reward'] if r['reward'] is not None else '-':>4} {r['peak']:>8,} "
              f"{r['in']:>10,} {r['out']:>8,} {r['cost']:>7.3f} {r['trims']:>5} {r['dropped']:>5} {r['recall']:>6} "
              f"{r['exc']}")

    both = {r["task"] for r in rows if r["arm"] == "Smart"} & {r["task"] for r in rows if r["arm"] == "Native"}
    paired = [r for r in rows if r["task"] in both]
    long_tasks = {r["task"] for r in paired if r["arm"] == "Native" and r["peak"] >= args.trim_at}
    trimmed = {r["task"] for r in paired if r["arm"] == "Smart" and r["trims"]}
    print_totals(f"All finished trials ({len(rows)})", rows)
    print_totals(f"Paired tasks, both arms finished ({len(both)})", paired)
    print_totals(f"Long tasks: native peak >= {args.trim_at:,} ({len(long_tasks)}: {', '.join(sorted(long_tasks))})",
                 [r for r in paired if r["task"] in long_tasks])
    print_totals(f"Check: long or smart trimmed ({len(long_tasks | trimmed)})",
                 [r for r in paired if r["task"] in long_tasks | trimmed])
    print(f"\nrunning: {', '.join(running) or 'none'}")


if __name__ == "__main__":
    main()
