"""Cache-thinking probe: does the proxy's cut invalidate Claude's prompt cache, thinking on vs off? (README.md)"""

import argparse
import json
import os
import random
import shutil
import sqlite3
import subprocess
import time
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
ROOT = Path(os.environ.get("PROBE_ROOT", "/tmp/sc-probe-cache"))
PORT = int(os.environ.get("PROBE_PORT", "4121"))
MODEL = os.environ.get("PROBE_MODEL", "claude-haiku-5-5")
TRIM_AT = os.environ.get("CONTEXT_TRIM_AT_TOKENS", "55000")
COMPOSE = REPO / "bench/probe/constraint/compose.probe.yaml"
PROJECT = "sc-probe-cache"
# Each filler file is about 5K tokens, so the session crosses TRIM_AT a few turns after Claude Code's ~35K prefix.
FILLER_BYTES = 16_000
FILLERS = 9
TURN_TIMEOUT_S = 600
ARMS = {"think": {"alwaysThinkingEnabled": True}, "nothink": {"alwaysThinkingEnabled": False}}
# MAX_THINKING_TOKENS=0 with alwaysThinkingEnabled false left Haiku 5.5 thinking on 2.1.280 (README.md, known issue).
ARM_ENV = {"think": {}, "nothink": {"MAX_THINKING_TOKENS": "0", "CLAUDE_CODE_DISABLE_THINKING": "1"}}
WORDS = ("amber basil cobalt delta ember fjord garnet harbor indigo juniper kestrel lumen meadow nickel "
         "onyx pepper quartz russet saffron tundra umber violet willow xenon yarrow zephyr").split()
TURN_FIRST = "List the files in this directory. Reply in one line. Do not change anything."
TURN_FILLER = ("Read notes_{k}.txt in full with the Read tool, then reply with only its last word and the number of "
               "lines. Do not change anything.")


def filler(k):
    rng = random.Random(k)
    lines, size = [], 0
    while size < FILLER_BYTES:
        line = f"{k}-{len(lines)}: " + " ".join(rng.choice(WORDS) for _ in range(12))
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines) + "\n"


def master_key():
    env = Path(os.environ.get("CONTEXT_PROXY_ENV_FILE", Path.home() / ".config/smarter-compact/proxy.env"))
    for line in env.read_text().splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit(f"LITELLM_MASTER_KEY not in {env}")


def compose(state, *args):
    env = {**os.environ, "PROBE_SRC": str(REPO), "PROBE_STATE": str(state), "PROBE_PORT": str(PORT),
           "HOST_UID": str(os.getuid()), "HOST_GID": str(os.getgid()), "CONTEXT_TRIM_AT_TOKENS": TRIM_AT}
    subprocess.run(["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE), *args], env=env, check=True)


def wait_ready(timeout_s=120):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health/liveliness", timeout=2):
                return
        except OSError:
            time.sleep(2)
    raise SystemExit(f"proxy on {PORT} not ready")


def make_config(cfg, arm):
    """Fresh CLAUDE_CONFIG_DIR: credentials only, plus the arm's thinking setting."""
    cfg.mkdir(parents=True)
    shutil.copy(Path.home() / ".claude/.credentials.json", cfg / ".credentials.json")
    env = {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{PORT}",
           "ANTHROPIC_CUSTOM_HEADERS": f"x-litellm-api-key: Bearer {master_key()}", **ARM_ENV[arm]}
    (cfg / "settings.json").write_text(json.dumps({"env": env, **ARMS[arm]}, indent=2))
    return env


def turn(run_dir, work, cfg, env, sid, n, prompt):
    cmd = ["claude", "-p", prompt, "--model", MODEL, "--output-format", "json", "--allowedTools", "Read,Glob,Bash(ls:*)",
           "--strict-mcp-config", *(["--session-id", sid] if n == 1 else ["--resume", sid])]
    penv = {**os.environ, **env, "CLAUDE_CONFIG_DIR": str(cfg)}
    out = subprocess.run(cmd, cwd=work, env=penv, capture_output=True, text=True, timeout=TURN_TIMEOUT_S)
    (run_dir / f"turn{n}.json").write_text(out.stdout + out.stderr)
    try:
        result = json.loads(out.stdout)
    except ValueError:
        result = {}
    print(f"  turn {n}: session cost=${result.get('total_cost_usd', 0):.4f} {str(result.get('result'))[:60]!r}", flush=True)
    # Claude Code's total_cost_usd is the resumed session's running total, not this turn's.
    return result.get("total_cost_usd") or 0.0


def rows(state, sid):
    """Usage events of the session's main thread (the most frequent thread_root), in order."""
    db = next((state / "context" / "sessions").glob(f"*{sid}*.db"))
    with sqlite3.connect(db) as c:
        events = c.execute("SELECT id, thread_root, trimmed, input_tokens, cache_read_tokens, cache_creation_tokens, "
                           "output_tokens FROM events WHERE kind='usage' ORDER BY id").fetchall()
        thinking = c.execute("SELECT count(*) FROM messages WHERE kind='thinking'").fetchone()[0]
    main = Counter(e[1] for e in events).most_common(1)[0][0]
    keys = ("id", "trimmed", "input", "cache_read", "cache_creation", "output")
    return [dict(zip(keys, (e[0], *e[2:]))) for e in events if e[1] == main], thinking


def transcript_blocks(cfg, sid):
    """Assistant content block types in Claude Code's own transcript: thinking blocks show thinking was on."""
    counts = Counter()
    for path in cfg.glob(f"projects/*/{sid}.jsonl"):
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            content = (entry.get("message") or {}).get("content") if entry.get("type") == "assistant" else None
            counts.update(b.get("type") for b in content or [] if isinstance(b, dict))
    return dict(counts)


def window(events):
    """For each new cut: the request before it, the cut request and the next two."""
    out = []
    for i, e in enumerate(events):
        if e["trimmed"] == 2:
            out.append(events[max(i - 1, 0):i + 3])
    return out


def run(arm, n):
    run_dir = ROOT / "runs" / f"{arm}-{n}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    work, cfg = run_dir / "work", run_dir / "cfg"
    work.mkdir(parents=True)
    for k in range(1, FILLERS + 1):
        (work / f"notes_{k}.txt").write_text(filler(k))
    env = make_config(cfg, arm)
    sid = str(uuid.uuid4())
    print(f"{arm}-{n} session {sid}", flush=True)
    cost = turn(run_dir, work, cfg, env, sid, 1, TURN_FIRST)
    for k in range(1, FILLERS + 1):
        cost = max(cost, turn(run_dir, work, cfg, env, sid, k + 1, TURN_FILLER.format(k=k)))
    events, thinking = rows(ROOT / "state", sid)
    blocks = transcript_blocks(cfg, sid)
    summary = {"arm": arm, "model": MODEL, "trim_at": int(TRIM_AT), "session": sid, "cost_usd": round(cost, 4),
               "thinking_rows": thinking, "transcript_blocks": blocks, "events": events, "cuts": window(events)}
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps({k: summary[k] for k in ("arm", "cost_usd", "thinking_rows", "transcript_blocks")}), flush=True)
    for cut in summary["cuts"]:
        print("  " + " | ".join(f"t{e['trimmed']} in={e['input']} read={e['cache_read']} write={e['cache_creation']}"
                               for e in cut), flush=True)
    return cost


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="think,nothink")
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--budget", type=float, default=2.0)
    args = ap.parse_args()
    state = ROOT / "state"
    state.mkdir(parents=True, exist_ok=True)
    compose(state, "up", "-d")
    wait_ready()
    spent = 0.0
    try:
        for i in range(1, args.n + 1):
            for arm in args.arms.split(","):
                if spent >= args.budget:
                    print(f"budget ${args.budget} reached", flush=True)
                    return
                spent += run(arm, i)
    finally:
        compose(state, "down")
        print(f"total ${spent:.4f}", flush=True)


if __name__ == "__main__":
    main()
