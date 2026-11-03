#!/usr/bin/env python3
"""Did a smart trial actually need what its trim removed?

A smart trial with `trims>0, recall_calls=0` reads the same three ways, and the run must not
have to guess which one it got:

  1. not needed    - the dropped content was never used again (masking was free)
  2. re-derived    - the agent read/grepped its way back to it from the repo (masking cost work)
  3. unreachable   - the tools were never callable (a broken run, see summarize.py)

This separates 1 from 2 by looking for post-trim tool calls against targets the trial had
already touched before the trim: content that left the context and had to be fetched again.
Re-derivation is not recall, but it is proof the content mattered.

  ./recall_audit.py hard45-haiku-60k-v2
  ./recall_audit.py hard45-haiku-60k-v2 --arm smart --verbose
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
PATH_TOKEN_RE = re.compile(r"[\w./-]*\.(?:py|txt|md|json|cfg|toml|ini|rst|c|h|pyx|pyx.in|sh|yml|yaml)\b")
READISH = {"read", "grep", "glob", "bash", "shell", "execute"}


def open_copy(db: Path, tag: str) -> sqlite3.Connection:
    """Read a live/copied WAL db off a writable copy: reading in place can demand recovery."""
    dst = Path(tempfile.gettempdir()) / f"recall-audit-{tag}.db"
    for suffix in ("", "-wal", "-shm"):
        if (db.parent / (db.name + suffix)).exists():
            shutil.copy(db.parent / (db.name + suffix), dst.with_name(dst.name + suffix))
    return sqlite3.connect(dst)


def store_facts(trial_dir: Path) -> tuple[list[int], dict]:
    """Trim event times (epoch ms) and the masked-content profile from the plugin's own store.

    The profile matters: file reads the agent can redo are cheap to lose, masked `tool_result`
    rows (test output, command runs) are not re-derivable from the repo at all.
    """
    trims, profile = [], {"masked_chars": 0, "masked_tool_result_chars": 0, "masked_rows": 0}
    for db in sorted((trial_dir / "state/context/sessions").glob("*.db")):
        try:
            con = open_copy(db, db.name)
            for (at,) in con.execute("select at from events where kind = 'trim' order by rowid"):
                trims.append(int(dt.datetime.fromisoformat(str(at).replace("Z", "+00:00")).timestamp() * 1000))
            try:
                rows = con.execute("select kind, count(*), coalesce(sum(length(text)), 0) from messages "
                                   "where in_context = 0 group by kind").fetchall()
                for kind, count, chars in rows:
                    profile["masked_rows"] += count
                    profile["masked_chars"] += chars
                    if kind == "tool_result":
                        profile["masked_tool_result_chars"] += chars
            except Exception:  # noqa: BLE001 - older stores may not carry the messages table
                pass
            con.close()
        except Exception as e:  # noqa: BLE001 - audit is read-only best effort
            print(f"warn: {db}: {type(e).__name__}", file=sys.stderr)
    return trims, profile


def calls(trial_dir: Path) -> list[dict]:
    """Every tool call in order: (when, tool, targets, output chars)."""
    db = trial_dir / "home/.local/share/opencode/opencode.db"
    if not db.is_file():
        return []
    out = []
    con = open_copy(db, f"{trial_dir.parent.name}-{trial_dir.name}")
    for when, blob in con.execute("select time_created, data from session_message order by seq"):
        raw = blob.decode() if isinstance(blob, (bytes, bytearray)) else blob
        try:
            msg = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        for part in (msg.get("content") or []):
            if not isinstance(part, dict) or part.get("type") != "tool":
                continue
            state = part.get("state") or {}
            inp = state.get("input") or {}
            text = json.dumps(inp)
            targets = set(PATH_TOKEN_RE.findall(text))
            for key in ("path", "filePath"):
                if isinstance(inp.get(key), str):
                    targets.add(inp[key])
            out.append({
                "at": when or 0,
                "tool": part.get("name"),
                "targets": targets,
                # OpenCode keeps a tool's reply in state.content (parts), not state.output.
                "chars": len(json.dumps(state.get("content") if state.get("content") is not None
                                        else state.get("output") or "")),
            })
    con.close()
    return out


def audit(trial_dir: Path) -> dict:
    trims, profile = store_facts(trial_dir)
    events = calls(trial_dir)
    result = {"trims": len(trims), "calls": len(events), "recall_calls": 0, "rederived": 0,
              "rederive_chars": 0, "post_trim_calls": 0, **profile}
    meta = trial_dir / "meta.json"
    if meta.is_file():
        try:
            result["recall_calls"] = json.loads(meta.read_text()).get("recall_calls") or 0
        except Exception:  # noqa: BLE001
            pass
    for i, trim_at in enumerate(trims):
        seen: set[str] = set()
        for ev in events:
            if ev["at"] <= trim_at:
                if ev["tool"] in READISH:
                    seen |= ev["targets"]
                continue
            if i != len(trims) - 1 and ev["at"] > trims[i + 1]:
                continue  # attribute this window only
            result["post_trim_calls"] += 1
            overlap = ev["targets"] & seen
            if ev["tool"] in READISH and overlap:
                result["rederived"] += 1
                result["rederive_chars"] += ev["chars"]
            if ev["tool"] in READISH:
                seen |= ev["targets"]  # edits/writes must not make a later read look like recovery
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("run_id")
    ap.add_argument("--arm", default="smart")
    ap.add_argument("--dir", default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    run_dir = (Path(args.dir) if args.dir else RESULTS) / args.run_id
    trials = sorted((run_dir / args.arm).glob("*/meta.json")) if (run_dir / args.arm).is_dir() else []
    if not trials:
        print(f"no {args.arm} trials under {run_dir}")
        return 1
    print(f"{'trial':<32} {'trims':>5} {'calls':>6} {'post':>5} {'rederived':>10} {'rederived_chars':>16} {'recall':>7}  read")
    needed = unused = 0
    for meta in trials:
        trial_dir = meta.parent
        r = audit(trial_dir)
        if r["trims"] == 0:
            verdict = "no trim"
        elif r["recall_calls"]:
            verdict = "recalled"
            needed += 1
        elif r["rederived"]:
            verdict = "re-derived (masking cost)"
            needed += 1
        else:
            verdict = "dropped content unused"
            unused += 1
        print(f"{trial_dir.name:<32} {r['trims']:>5} {r['calls']:>6} {r['post_trim_calls']:>5} "
              f"{r['rederived']:>10} {r['rederive_chars']:>16} {r['recall_calls']:>7}  {verdict}")
    print(f"\ntrials where the trim removed content the trial later needed: {needed}/{len(trials)}"
          f"   unused: {unused}/{len(trials)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
