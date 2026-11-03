#!/usr/bin/env python3
"""Summarize a bench run from its per-trial meta.json.

Why this exists as a file rather than an ad-hoc query: the recall bug was a *measurement*
failure as much as a wiring failure, and ad-hoc per-run queries are where a miscount hides.
Any number read off this table is the same number every other reader sees.

  ./summarize.py hard45-haiku-60k-v2
  ./summarize.py hard45-haiku-60k-v2 --by-instance
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import statistics
import sys
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"

MCP_CONNECTED_RE = re.compile(rb'message="mcp connected" server=openmem tools=(\d+)')


def tools_offered(meta: dict, trial_dir: Path) -> int:
    """Recall tools OpenCode confirmed for this trial. Falls back to the opencode log so runs
    scored before `recall_tools_offered` existed (or scored at all) still answer the question."""
    value = meta.get("recall_tools_offered")
    if isinstance(value, int):
        return value
    log_file = trial_dir / "home" / ".local/share/opencode/log/opencode.log"
    if not log_file.is_file():
        return 0
    try:
        found = MCP_CONNECTED_RE.findall(log_file.read_bytes())
    except OSError:
        return 0
    return max((int(n) for n in found), default=0)


def load_trials(run_dir: Path) -> list[dict]:
    trials = []
    for meta_file in sorted(run_dir.glob("*/*/meta.json")):
        try:
            meta = json.loads(meta_file.read_text())
        except Exception as e:  # noqa: BLE001 - a torn write must not hide the rest
            print(f"warn: unreadable {meta_file}: {type(e).__name__}", file=sys.stderr)
            continue
        meta["_arm"] = meta_file.parent.parent.name
        meta["_instance"] = meta_file.parent.name
        meta["_dir"] = str(meta_file.parent)
        trials.append(meta)
    return trials


def rate(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100.0 * part / whole:.0f}%)" if whole else "0/0 (-)"


def mean(values: list) -> str:
    nums = [v for v in values if isinstance(v, (int, float))]
    return f"{statistics.mean(nums):.1f}" if nums else "-"


def report(run_dir: Path, by_instance: bool) -> int:
    trials = load_trials(run_dir)
    if not trials:
        print(f"no trials under {run_dir}")
        return 1
    arms = sorted({t["_arm"] for t in trials})
    per = collections.defaultdict(list)
    for t in trials:
        per[(t["_arm"], t["_instance"])].append(t)  # repeats: keep the last attempt below

    print(f"run: {run_dir.name}  trials: {len(trials)}  arms: {', '.join(arms)}")
    header = f"{'arm':<7} {'trials':>7} {'resolved':>12} {'secs':>7} {'patch':>8} {'trims':>7} {'recall':>7} {'cost$':>7}"
    print(header)
    unused: list[str] = []
    unoffered: list[str] = []
    for arm in arms:
        rows = [v[-1] for (a, _), v in per.items() if a == arm]
        resolved = sum(1 for r in rows if r.get("resolved"))
        offered_rows = [r for r in rows if tools_offered(r, Path(r["_dir"])) > 0]
        trimmed = [r for r in rows if (r.get("trims") or 0) > 0]
        recalled = [r for r in rows if (r.get("recall_calls") or 0) > 0]
        cost = sum(r.get("cost_usd") or 0 for r in rows)
        print(
            f"{arm:<7} {len(rows):>7} {rate(resolved, len(rows)):>12} "
            f"{mean([r.get('agent_seconds') for r in rows]):>7} "
            f"{mean([r.get('patch_bytes') for r in rows]):>8} "
            f"{mean([r.get('trims') for r in rows]):>7} "
            f"{mean([r.get('recall_calls') for r in rows]):>7} {cost:>7.2f}"
        )
        # A smart trial that trimmed but recalled nothing is either a masking result
        # (offered, unused) or a broken run (never offered). They mean opposite things.
        if arm == "smart":
            for r in trimmed:
                if not (r.get("recall_calls") or 0):
                    unused.append(r["_instance"])
                    if not tools_offered(r, Path(r["_dir"])):
                        unoffered.append(r["_instance"])
        print(f"{'':<7} offered: {rate(len(offered_rows), len(rows))}   trimmed: {rate(len(trimmed), len(rows))}   recalled: {rate(len(recalled), len(rows))}")

    if by_instance:
        print("\nper instance (last attempt):")
        print(f"{'instance':<34} " + " ".join(f"{a:>16}" for a in arms))
        for iid in sorted({i for (_, i) in per}):
            cells = []
            for arm in arms:
                rows = per.get((arm, iid))
                if not rows:
                    cells.append(f"{'-':>16}")
                    continue
                r = rows[-1]
                mark = "R" if (r.get("recall_calls") or 0) else " "
                kb = int(r.get("patch_bytes") or 0) // 1000
                cells.append(f"{str(bool(r.get('resolved'))):>4} {str(r.get('trims')):>4}{mark:>3}{kb:>4}k")
            print(f"{iid:<34} " + " ".join(cells))

    if unused:
        print(f"\ntrimmed but never recalled ({len(unused)}/{len(trials)}): {', '.join(sorted(set(unused)))}")
    if unoffered:
        print(f"BROKEN smart trials — recall tools never connected ({len(unoffered)}): "
              f"{', '.join(sorted(set(unoffered)))}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_id", help="results/<run_id> to summarize")
    ap.add_argument("--dir", default=None, help="results root (defaults alongside this script)")
    ap.add_argument("--by-instance", action="store_true", help="per-instance matrix; R marks a trial that used recall")
    args = ap.parse_args()
    run_dir = (Path(args.dir) if args.dir else RESULTS) / args.run_id
    return report(run_dir, args.by_instance)


if __name__ == "__main__":
    raise SystemExit(main())
