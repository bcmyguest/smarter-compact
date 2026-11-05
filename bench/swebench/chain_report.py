#!/usr/bin/env python3
"""Report a chained run by position: does the session actually grow, and what does growth cost?

A flat SWE-bench summary answers "did the arm resolve". A chain has a second axis — where in the
session a task sat — and that is the whole point of running it: the first tasks are the control and
the tasks past context saturation are the measurement. Read it position by position, or the run
looks like a flat run that happened to be slow.

  venv/bin/python chain_report.py --run-id chain-django8
"""
import argparse
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"


def load(run_dir: Path) -> dict:
    trials = defaultdict(list)
    for f in sorted(run_dir.glob("*/*/meta.json")):
        try:
            m = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if m.get("chain") is None:
            continue
        m["_arm"] = f.parent.parent.name
        trials[m["_arm"]].append(m)
    for arm in trials:
        trials[arm].sort(key=lambda m: (m.get("chain") or "", m.get("chain_pos") or 0))
    return dict(trials)


def peak(m: dict) -> int:
    return m.get("max_prompt_tokens") or 0


def row(m: dict) -> str:
    tokens = m.get("tokens") or {}
    resolved = {True: "PASS", False: "fail", None: "-"}[m.get("resolved")]
    return (f"  {str(m.get('chain_pos')):>2} {m['instance_id'].split('-', 1)[-1]:>10} "
            f"{peak(m):>8,} {tokens.get('steps', 0):>4} "
            f"{str(m.get('trims')) + '/' + str(m.get('trims_total')):>9} "
            f"{str(m.get('recall_calls')) + '/' + str(m.get('recall_total')):>7} "
            f"{m.get('agent_seconds') or 0:>6.0f} {m.get('cost_usd') or 0:>6.2f} {resolved:>4}"
            f"{'  resume' if m.get('chain_resumed') else '  NEW-SESSION'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--saturate", type=int, default=0,
                    help="peak-token level that counts as saturated (endpoint window)")
    args = ap.parse_args()

    run_dir = RESULTS / args.run_id
    trials = load(run_dir)
    if not trials:
        print(f"{args.run_id}: no chained trials (meta.json needs a `chain` field)")
        return
    print(f"{args.run_id}\n{'pos':>4} {'instance':>10} {'peak ctx':>9} {'steps':>5} "
          f"{'trims':>9} {'recall':>7} {'secs':>6} {'usd':>6} res")
    for arm, metas in trials.items():
        print(f"\n{arm}: {len(metas)} tasks")
        for m in metas:
            print(row(m))
        peaks = [peak(m) for m in metas if peak(m)]
        if not peaks:
            print("  no peak-token readings: the stream carried no token reports")
            continue
        print(f"  context: first {peaks[0]:,} -> last {peaks[-1]:,} "
              f"(median {st.median(peaks):,.0f}, max {max(peaks):,})")
        if len(peaks) > 1 and peaks[-1] <= peaks[0]:
            print("  WARNING peak context did not grow across the chain: the session is not "
                  "continuing, this is a flat run")
        res = [m.get("resolved") for m in metas if m.get("resolved") is not None]
        if res:
            print(f"  resolved {sum(res)}/{len(res)}"
                  + (f" | unresolved: {[m['instance_id'] for m in metas if m.get('resolved') is False]}"
                     if not all(res) else ""))
        cut = args.saturate or 0
        if cut:
            early = [m.get("resolved") for m in metas if peak(m) < cut and m.get("resolved") is not None]
            late = [m.get("resolved") for m in metas if peak(m) >= cut and m.get("resolved") is not None]
            if early and late:
                print(f"  under {cut // 1000}k: {sum(early)}/{len(early)}   "
                      f"at/over: {sum(late)}/{len(late)}")
        if any(m.get("chain_resumed") is None and (m.get("chain_pos") or 0) > 0 for m in metas):
            print("  WARNING a task after the first did not resume the chain session")
        deferred = sum(1 for m in metas if m.get("score_deferred"))
        if deferred:
            print(f"  {deferred} task(s) scored out-of-band (--defer-score); run --score-only")


if __name__ == "__main__":
    main()
