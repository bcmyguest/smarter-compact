#!/usr/bin/env python3
"""Terminal-Bench runner for the smarter-compact arms.

Terminal-Bench replaces SWE-bench where the arms need headroom: hard45 runs 93-100% resolved
with this model, so an arm difference there is unmeasurable, while TB's 241 local tasks carry an
author-declared `difficulty` per task and pytest graders that never touch a git tree.

This file deliberately does NOT reimplement the trial. Container mounts, the trial-local
opencode.json (provider/compaction/plugin/MCP), token extraction, the trim + recall ledger read,
the request forwarder and cost all come from run_bench.py, because that path is the one with a
measured history: 26/43 haiku trials trimmed through it and the recall tool was proven in the
outbound request. Only what TB genuinely does differently lives here -- task.yaml prompts, a
docker build per task, and scoring by the task's own run-tests.sh ($TEST_DIR/test_outputs.py,
exit 0 == resolved) instead of a SWE-bench harness in a second container.

  venv/bin/python tb_bench.py --run-id tb-probe --sample easy=4,medium=4,hard=4 --trim-at 95000
"""
import argparse
import concurrent.futures as cf
import json
import os
import random
import re
import shutil
import statistics as st
import sys
import time
from pathlib import Path

import docker
import yaml

HERE = Path(__file__).resolve().parent
SWEB = HERE.parent / "swebench"
sys.path.insert(0, str(SWEB))
import run_bench as rb  # noqa: E402  (path set above; the whole point of this file)

RESULTS = HERE / "results"
DEFAULT_TASKS = HERE / "tb" / "original-tasks"

# TB's instruction is a terminal task, not an issue report: no <issue> wrapper, and the agent must
# know the grader runs after it stops (it otherwise spends the last minutes writing its own tests).
PROMPT = """{instruction}

Work in the current directory. Automated tests grade this task after you stop, so finish the change itself \
rather than describing it, and do not add or edit test files. Say DONE when finished.
"""

# Tasks whose grading needs a GPU, hours of compilation, or a third-party download will fail for
# reasons that have nothing to do with context management, so they are out of the sampled pool.
EXCLUDE = re.compile(
    r"kernel|qemu|video|youtube|train|cartpole|mle|hugging|hf-model|gpt2|zork|cuda|gpu|"
    r"docker|kubernetes|k8s|postgres|mysql|mongo|whisper|ollama|sphinx-build", re.I)


def load_task(tdir: Path) -> dict:
    spec = yaml.safe_load((tdir / "task.yaml").read_text())
    instruction = (spec.get("instruction") or "").strip()
    if not instruction and (tdir / "instructions.md").exists():
        instruction = (tdir / "instructions.md").read_text().strip()
    tests = tdir / "tests"
    return {
        "name": tdir.name, "dir": tdir, "instruction": instruction,
        "difficulty": str(spec.get("difficulty") or "unknown"),
        "category": str(spec.get("category") or ""),
        "agent_timeout": int(spec.get("max_agent_timeout_sec") or 900),
        "test_timeout": int(spec.get("max_test_timeout_sec") or 300),
        # The grader is pytest on test_outputs.py; a task without it cannot be scored here.
        "gradable": (tests / "test_outputs.py").exists(),
    }


def sample(tasks_dir: Path, plan: str, seed: int) -> list:
    """Pick tasks per difficulty from the light, gradable, offline-buildable pool."""
    want = {}
    for part in plan.split(","):
        k, _, n = part.partition("=")
        want[k.strip()] = int(n or 4)
    rng = random.Random(seed)
    pool, picked = [], []
    for tdir in sorted(tasks_dir.iterdir()):
        if not (tdir / "task.yaml").is_file():
            continue
        t = load_task(tdir)
        if not t["gradable"] or EXCLUDE.search(t["name"]):
            continue
        # No Dockerfile means the task builds through docker-compose with a context we don't
        # reproduce here; skip it rather than guess (conda-env-conflict-resolution is one).
        dfile = tdir / "Dockerfile"
        if not dfile.is_file() or "FROM" not in dfile.read_text(errors="replace"):
            continue
        pool.append(t)
    for diff, n in want.items():
        cand = [t for t in pool if t["difficulty"] == diff]
        rng.shuffle(cand)
        picked.extend(cand[:n])
    print(f"pool={len(pool)} gradable/light tasks; picked {len(picked)} "
          f"({', '.join(f'{d}:{want[d]}' for d in want)})", flush=True)
    return picked


def ensure_image(client, task: dict) -> str:
    tag = f"tb-sc-{task['name']}:latest"
    try:
        client.images.get(tag)
        return tag
    except docker.errors.ImageNotFound:
        pass
    # pull=False: the two t-bench bases are pre-pulled, and 190 of 241 tasks start from one of them.
    client.images.build(path=str(task["dir"]), tag=tag, rm=True, pull=False)
    return tag


def run_trial(client, task: dict, arm: str, args, run_dir: Path) -> dict:
    name = task["name"]
    trial_dir = run_dir / arm / name
    (trial_dir / "logs").mkdir(parents=True, exist_ok=True)
    # The grader rides in through the trial mount, so no `docker cp` and no image rebuild.
    # Note run-tests.sh is a SIBLING of tests/, not inside it (TB keeps it at the task root), so
    # copying only tests/ leaves the runner with nothing to execute and exit 127 as the only
    # "result" -- which reads like a failed task and is really an unscored one.
    if not (trial_dir / "tests").exists():
        shutil.copytree(task["dir"] / "tests", trial_dir / "tests")
        for helper in ("run-tests.sh", "run-tests-incomplete.sh"):
            src = task["dir"] / helper
            if src.is_file():
                shutil.copy2(src, trial_dir / "tests" / helper)
    grader = "tests/run-tests.sh" if (trial_dir / "tests" / "run-tests.sh").is_file() else "pytest"
    agent_home, agent_state = trial_dir / "home", trial_dir / "state"
    agent_home.mkdir(parents=True, exist_ok=True)
    agent_state.mkdir(parents=True, exist_ok=True)

    meta = {"instance_id": name, "task": name, "arm": arm, "difficulty": task["difficulty"],
            "category": task["category"], "code_sha": rb.CODE_SHA,
            # Recorded so a peak can be read against the threshold it ran under (see run_trial).
            "trim_at": args.trim_at, "native_at": args.native_at,
            "context_limit": args.context_limit}
    log = lambda msg: rb.log(f"{name} arm={arm} {msg}")  # noqa: E731
    t0 = time.time()
    container = None
    log("start")
    try:
        image = ensure_image(client, task)
        meta["image"] = image
        cname = f"tbsc-{name}-{arm}-{args.run_id}"
        try:
            client.containers.get(cname).remove(force=True)
        except docker.errors.NotFound:
            pass
        container = client.containers.run(
            image, command=["sleep", str(args.timeout + 900)], detach=True, name=cname,
            network="host",
            volumes={
                str(args.opencode_bin_dir): {"bind": "/opt/opencodebin", "mode": "ro"},
                str(args.node_bin_dir): {"bind": "/opt/node", "mode": "ro"},
                str(rb.REPO_ROOT): {"bind": rb.REPO_MOUNT, "mode": "ro"},
                str(trial_dir): {"bind": "/opt/trial", "mode": "rw"},
                str(agent_home): {"bind": "/opt/agent/home", "mode": "rw"},
                str(agent_state): {"bind": "/opt/agent/state", "mode": "rw"},
            },
        )
        # Open the evidence dirs while the agent works (artifacts land root-owned): a watcher on the
        # host gets EACCES otherwise, and a 5s period matters because a trial killed at 15s would
        # otherwise leave the trim ledger in a 700 directory no host-side read can open.
        container.exec_run(
            ["sh", "-c", "while :; do chmod -R a+rX /opt/trial/requests /opt/trial/logs "
                          "/opt/agent/state 2>/dev/null; sleep 5; done"], detach=True)

        models = [args.model]
        base_url = None
        upstream = args.provider_cfg["options"]["baseURL"]
        if os.environ.get("BENCH_CAPTURE", "1") == "1":
            port = rb.CAPTURE_PORT_DEFAULT or rb.free_port()
            if rb.start_capture(container, upstream, port):
                route = re.match(r"https?://[^/]+(/.*)?$", upstream)
                base_url = f"http://127.0.0.1:{port}{(route.group(1) if route and route.group(1) else '')}"
                meta["capture_port"] = port
        rb.write_agent_config(agent_home, args.provider_cfg, models, arm, rb.REPO_MOUNT,
                              trim_at=args.trim_at, context_limit=args.context_limit,
                              native_at=args.native_at, base_url=base_url)
        image_env = dict(e.split("=", 1) for e in (client.images.get(image).attrs
                                                   .get("Config", {}).get("Env") or []) if "=" in e)
        env = {
            "HOME": "/opt/agent/home",
            "XDG_CONFIG_HOME": "/opt/agent/home/.config",
            "XDG_DATA_HOME": "/opt/agent/home/.local/share",
            "XDG_CACHE_HOME": "/opt/agent/home/.cache",
            "PATH": f"/opt/node:/opt/opencodebin:{image_env.get('PATH', '/usr/local/bin:/usr/bin:/bin')}",
            "SMARTER_COMPACT_STATE_DIR": "/opt/agent/state",
            "SMARTER_COMPACT_AUTO_PROXY": "0",
            "CONTEXT_TRIM_AT_TOKENS": str(args.trim_at),
        }
        # TB images set their own WORKDIR (/app in the t-bench bases); the grader refuses to run
        # from "/", so trust the image and record what it was.
        workdir = client.images.get(image).attrs.get("Config", {}).get("WorkingDir") or "/app"
        meta["workdir"] = workdir
        (trial_dir / "prompt.txt").write_text(PROMPT.format(instruction=task["instruction"]))
        oc = ["/opt/opencodebin/opencode", "run", "--standalone", "--print-logs", "--auto",
              "--format", "json", "--model", f"local/{args.model}", "-"]
        inner = (f"cd {workdir} && " + " ".join(rb.shlex.quote(c) for c in oc)
                 + " < /opt/trial/prompt.txt > /opt/trial/logs/agent_stream.jsonl 2>&1")
        agent_timeout = min(args.timeout, task["agent_timeout"])

        # The agent redirects its stream to a file under the bind mount, so a trial that hits its
        # timeout keeps everything it wrote: the pipe form lost a whole 952s trial to Node's
        # block-buffered stdout (see rb.exec_stream).
        exit_code = rb.exec_stream(
            container,
            ["env"] + [f"{k}={v}" for k, v in env.items()] + ["sh", "-c", inner],
            trial_dir / "logs" / "agent_stream.jsonl", timeout=agent_timeout, durable=True)
        if exit_code is None:
            meta["agent_exit"] = -1
            meta["error"] = "agent: timeout"
        else:
            meta["agent_exit"] = exit_code
        meta["agent_seconds"] = round(time.time() - t0, 1)
        meta["tokens"] = rb.extract_tokens(trial_dir / "logs" / "agent_stream.jsonl")
        meta["session_id"] = meta["tokens"].pop("session_id", None)
        meta["max_prompt_tokens"] = meta["tokens"].pop("max_prompt_tokens", 0)
        meta["model"] = args.model
        meta["cost_usd"] = rb.cost_or_none(args.model, meta["tokens"])

        # Grading: the task's own run-tests.sh, in the same container and workdir the agent used
        # (TB runs tests in the client container; $TEST_DIR points at the mounted tests/).
        if exit_code is not None:
            cmd = (["bash", "/opt/trial/tests/run-tests.sh"] if grader == "tests/run-tests.sh" else
                   ["sh", "-c", "python -m pytest /opt/trial/tests/test_outputs.py -rA"])
            meta["grader"] = grader
            try:
                with cf.ThreadPoolExecutor(max_workers=1) as ex:
                    tr = ex.submit(container.exec_run,
                                   ["env"] + [f"{k}={v}" for k, v in env.items()] +
                                   [f"TEST_DIR=/opt/trial/tests"] + cmd,
                                   workdir=workdir, stdout=True, stderr=True, tty=False,
                                   socket=False, demux=False).result(timeout=args.test_timeout)
                meta["test_exit"] = tr.exit_code
                # 126/127 means the grader itself could not start -- a harness bug, not a model
                # failure. Record those as unscored (None) so they never land in the pass rate.
                meta["resolved"] = None if tr.exit_code in (126, 127) else tr.exit_code == 0
                if meta["resolved"] is None:
                    meta["error"] = f"grader did not run (exit {tr.exit_code})"
                (trial_dir / "logs" / "tests.log").write_bytes(
                    tr.output if isinstance(tr.output, bytes) else tr.output.encode())
            except cf.TimeoutError:
                meta["test_exit"] = -1
                meta["error"] = "tests: timeout"
        if exit_code is not None:
            container.exec_run(["sh", "-c", "chmod -R a+rX /opt/trial /opt/agent/state /opt/agent/home "
                                            "2>/dev/null; true"], stdout=False, stderr=False)
            stats = rb.context_stats_in_container(container)
            meta["trims_source"] = "in-container"
        else:
            # Killed container: the ledger is on the bind mount, and the trial's chmod watcher plus
            # exec_stream's pre-kill chmod already opened it, so read the counts from the host
            # instead of an exec that cannot run. Recorded so a degraded count is never mistaken
            # for a live one.
            stats = rb.context_stats_from_dir(trial_dir)
            meta["trims_source"] = "host-dir"
        recall = stats.get("recall_ledger") or {}
        meta["trims"] = stats.get("trims")
        meta["recall_calls"] = stats.get("recall_calls") or 0
        meta["recall_tools"] = recall
        meta["recall_server_calls"] = stats.get("recall_server_calls") or {}
        meta["trims_dropped"] = stats.get("trims_dropped")
        meta["trims_peak_before"] = stats.get("trims_peak_before")
        meta["ledger_found"] = stats.get("ledger_found")
        meta["ledger_error"] = stats.get("ledger_error")
        meta["ledger_unreadable"] = bool(stats.get("ledger_unreadable"))
        if stats.get("usage_peak_ctx"):
            meta["peak_ctx_store"] = stats["usage_peak_ctx"]
        if arm == "smart":
            try:
                req = rb.request_facts(trial_dir)
                for k, v in req.items():
                    meta[f"req_{k}"] = v
                offered = req["recall_tools_offered"] or rb.mcp_tools_offered(trial_dir)
            except Exception:  # noqa: BLE001 - capture is optional evidence
                offered = rb.mcp_tools_offered(trial_dir)
            meta["recall_tools_offered"] = offered
            meta["recall_available"] = offered > 0 or meta["recall_calls"] > 0
            if meta["trims"] and not meta["recall_calls"]:
                meta["recall_warning"] = f"{offered} recall tools offered but unused after trimming"
    except Exception as e:  # noqa: BLE001 - one task must not kill the run
        meta.setdefault("error", f"{type(e).__name__}: {e}")
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001
                pass
    (trial_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    log(f"done exit={meta.get('agent_exit')} secs={meta.get('agent_seconds')} "
        f"peak={meta.get('max_prompt_tokens')} trims={meta.get('trims')} "
        f"recall={meta.get('recall_calls')} resolved={meta.get('resolved')} {meta.get('error', '')}")
    return meta


def summarize(run_dir: Path, model: str, trim_at: int) -> None:
    metas = [json.loads(p.read_text()) for p in sorted(run_dir.glob("*/*/meta.json"))]
    if not metas:
        print("no trials")
        return
    print(f"\n{run_dir.name}  model={model}  trials={len(metas)}")
    print(f"{'arm':6} {'difficulty':11} {'n':>3} {'resolved':>9} {'peak ctx':>10} {'med secs':>9} trims recall")
    by = {}
    for m in metas:
        by.setdefault((m["arm"], m.get("difficulty")), []).append(m)
    for (arm, diff), rows in sorted(by.items()):
        pk = [r.get("max_prompt_tokens") or 0 for r in rows]
        sc = [r for r in rows if r.get("resolved") is not None]
        print(f"{arm:6} {diff:11} {len(rows):>3} "
              f"{sum(1 for r in sc if r['resolved']):>4}/{len(sc):<4} "
              f"{max(pk) if pk else 0:>10,} {st.median([r.get('agent_seconds') or 0 for r in rows]):>9.0f} "
              f"{sum(r.get('trims') or 0 for r in rows):>5} {sum(r.get('recall_calls') or 0 for r in rows):>6}")
    pk = [m.get("max_prompt_tokens") or 0 for m in metas]
    print(f"peak context across run: median {st.median(pk):,.0f} max {max(pk):,}")
    if max(pk) < trim_at:
        print(f"NOTE no trial reached the trim threshold ({trim_at:,}): nothing trimmed, so "
              f"this run cannot separate the arms. Lower --trim-at or pick longer tasks.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--tasks-dir", default=str(DEFAULT_TASKS))
    ap.add_argument("--tasks", help="comma-separated task names (overrides --sample)")
    ap.add_argument("--sample", default="easy=4,medium=4,hard=4")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--arms", default="smart")
    ap.add_argument("--provider", default="cairn2")
    ap.add_argument("--model", required=True)
    ap.add_argument("--display-name", default="qwen-3.8-flash-next")
    ap.add_argument("--trim-at", type=int, default=95000)
    ap.add_argument("--native-at", type=int, default=95000)
    ap.add_argument("--context-limit", type=int, default=200000)
    ap.add_argument("--parallel", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--test-timeout", type=int, default=600)
    ap.add_argument("--opencode-bin-dir", default=str(Path.home() / ".opencode" / "bin"))
    ap.add_argument("--node-bin-dir", default=None)
    args = ap.parse_args()

    args.provider_cfg = rb.host_provider_config(args.provider)
    if not args.node_bin_dir:
        import subprocess
        node = subprocess.run(["which", "node"], capture_output=True, text=True).stdout.strip()
        args.node_bin_dir = str(Path(node).resolve().parent) if node else ""
    tasks_dir = Path(args.tasks_dir)
    if args.tasks:
        tasks = [load_task(tasks_dir / n.strip()) for n in args.tasks.split(",")]
    else:
        tasks = sample(tasks_dir, args.sample, args.seed)
    ungradable = [t["name"] for t in tasks if not t["gradable"]]
    if ungradable:
        sys.exit(f"tasks without tests/test_outputs.py (cannot score): {ungradable}")

    run_dir = RESULTS / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    arms = args.arms.split(",")
    rb.log(f"run {args.run_id}: {len(tasks)} tasks x arms {arms} model={args.model} "
           f"trim_at={args.trim_at} native_at={args.native_at} context={args.context_limit}")
    client = docker.from_env()
    jobs = [(t, a) for a in arms for t in tasks]
    with cf.ThreadPoolExecutor(max_workers=args.parallel) as ex:
        list(ex.map(lambda ja: run_trial(client, ja[0], ja[1], args, run_dir), jobs))
    summarize(run_dir, args.model, args.trim_at)


if __name__ == "__main__":
    main()
