#!/usr/bin/env python3
"""SWE-bench Verified benchmark for OpenCode 2 with and without smarter-compact.

Mirrors the arms of "The Complexity Trap" (JetBrains Research, arXiv 2508.21433)
on SWE-bench Verified: ``none`` (agent keeps everything; only OpenCode's native
auto-compaction) vs ``smart`` (smarter-compact context hook + search tools).

Each trial runs the OpenCode CLI inside the instance's official
``swebench/sweb.eval.x86_64.*`` image, collects ``git diff`` as the patch, and
scores it with the swebench harness. Model/provider are passed in; nothing in
this file encodes where the endpoint runs.

Usage (from the repo root):
  bench/swebench/venv/bin/python bench/swebench/run_bench.py \
      --model Qwen/Qwen3.8-Flash-Next-FP8 --display-name qwen-3.8-flash-next \
      --provider cairn2 --arms none,smart --parallel 2

Results land in bench/swebench/results/<run-id>/.

Live dashboard: `docker compose -f bench/docker-compose.yml up -d` then add
BENCH_MLFLOW=1 — trials stream to http://localhost:5000 as they finish (REST only,
no client lib; override endpoint with BENCH_MLFLOW_URI). Old runs: --backfill-mlflow.

Cost: every trial records cost_usd, priced from the token counts the endpoint
reports against the list prices in PRICING (self-hosted endpoints price at $0).
Price a finished or in-flight run with --cost-report --run-id X. The number is
notional when the key is a Claude plan key (sk-ant-usr…, no per-token invoice)
and actual spend when it is an API key (sk-ant-api03…).
"""

from __future__ import annotations

import argparse
import collections
import glob as globmod
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

import docker

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCH_DIR = Path(__file__).resolve().parent
# Where a trial sees its bind mounts and writes state (kept in one place because the
# recall MCP server config in opencode.json must name the same paths the container runs with).
REPO_MOUNT = "/opt/smarter-compact"
CONTAINER_HOME = "/opt/trial/home"
CONTAINER_STATE = "/opt/trial/state"
# The repo is bind-mounted live into every trial, so editing the plugin mid-run silently changes
# behaviour for the next trial and nothing in the artifacts says which code a trial ran. Stamp it.
def _code_sha() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - a stamp must never break a sweep
        return "unknown"


CODE_SHA = _code_sha()
DEFAULT_INSTANCES = [
    "psf__requests-1142",
    "psf__requests-1766",
    "psf__requests-2931",
    "pylint-dev__pylint-4970",
    "pylint-dev__pylint-6528",
    "pylint-dev__pylint-6903",
]

PROBLEM_LOCK = """<issue>
{problem_statement}
</issue>

The repository at /testbed is checked out at the buggy commit. Read the issue \
above and fix it by editing the source under /testbed. You may inspect files \
and run tests, but do not create new top-level scripts, do not commit, and do \
not modify files outside /testbed. Implement the fix in code, not in prose. \
Stop as soon as the fix is complete and verified by the narrowest test you can \
run."""

print_lock = threading.Lock()


def log(msg: str) -> None:
    with print_lock:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def image_for(instance_id: str) -> str:
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest".lower()


def load_instances(instance_ids: list[str], data_dir: Path) -> list[dict]:
    cache = data_dir / "swe-bench-verified.json"
    if cache.is_file():
        rows = json.loads(cache.read_text())
    else:
        from datasets import load_dataset  # provided by bench/swebench/venv

        # SWE-bench/ namespace: rows carry `image` + `eval_script` for the v5 harness.
        ds = load_dataset("SWE-bench/SWE-bench_Verified", split="test")
        rows = [dict(x) for x in ds]
        data_dir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(rows))
    by_id = {r["instance_id"]: r for r in rows}
    missing = [i for i in instance_ids if i not in by_id]
    if missing:
        sys.exit(f"unknown instance ids: {missing}")
    out = []
    for i in instance_ids:
        inst = dict(by_id[i])
        inst["image"] = image_for(i)
        out.append(inst)
    return out


def _dotenv_value(key: str) -> str:
    """First value for `key` in the repo-root .env (empty when absent)."""
    env_file = REPO_ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text(errors="replace").splitlines():
            name, _, val = line.partition("=")
            if name.strip() == key:
                return val.strip().strip('"').strip("'")
    return ""


BUILTIN_PROVIDERS = {
    # Not present in the host opencode.json; keys come from the environment or .env
    # (CLAUDE_API_KEY in the repo-root .env is the Claude subscription token fallback).
    "anthropic": {
        "npm": "@ai-sdk/anthropic",
        "name": "anthropic",
        "options": {
            "baseURL": "https://api.anthropic.com/v1",
            "apiKey": os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY")
            or _dotenv_value("CLAUDE_API_KEY"),
        },
    },
}


def host_provider_config(provider_id: str) -> dict:
    """Copy the provider block (baseURL + key) from the host OpenCode config."""
    cfg_path = Path.home() / ".config" / "opencode" / "opencode.json"
    text = cfg_path.read_text()
    text = re.sub(r"^\s*//.*$", "", text, flags=re.MULTILINE)  # strip // comments
    text = re.sub(r",(\s*[}\]])", r"\1", text)  # strip trailing commas
    cfg = json.loads(text)
    prov = cfg.get("provider", {}).get(provider_id)
    if not prov or not prov.get("options", {}).get("baseURL"):
        prov = BUILTIN_PROVIDERS.get(provider_id)
        if not prov or not prov["options"].get("apiKey"):
            sys.exit(f"no provider.{provider_id} in host opencode.json and no usable built-in "
                     f"(anthropic needs ANTHROPIC_API_KEY exported)")
    return prov


def write_agent_config(
    home: Path, prov_cfg: dict, models: list[str], arm: str, repo_mount: str,
    trim_at: int = 30000, context_limit: int = 102400, output_limit: int = 16384,
    native_at: int | None = None, base_url: str | None = None,
) -> None:
    """Write the trial-local opencode.json (provider exposed as `local`)."""
    cfg_dir = home / ".config" / "opencode"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    # OpenCode compacts natively when context exceeds (limit - reserved); pin the
    # limits so the local provider knows the window, and set native triggers that
    # make the arms comparable: none compacts natively at native_at, smart trims
    # via smarter-compact at trim_at with native at native_at as overflow guard.
    if native_at:
        native_trigger = native_at
    else:
        native_trigger = trim_at if arm == "none" else min(context_limit - output_limit, int(trim_at * 1.5))
    reserved = max(output_limit, context_limit - native_trigger)
    cfg: dict = {
        "$schema": "https://opencode.ai/config.json",
        # Offline benchmark: the agent must not depend on web search (no key in-container).
        "permission": {"websearch": "deny"},
        "compaction": {"auto": True, "reserved": reserved},
        "provider": {
            "local": {
                "npm": prov_cfg.get("npm", "@ai-sdk/openai-compatible"),
                "name": "local",
                "options": {
                    "baseURL": base_url or prov_cfg["options"]["baseURL"],
                    "apiKey": prov_cfg["options"].get("apiKey", "local"),
                },
                "models": {
                    m: {"limit": {"context": context_limit, "output": output_limit}}
                    for m in models
                },
            }
        },
    }
    if arm == "smart":
        cfg["plugins"] = [repo_mount]
        # The recall tools are configured here, not only by the plugin's ctx.mcp.add
        # self-registration: that call is a no-op on some OpenCode builds (it returns
        # without connecting, see the diag() line in src/v2.mjs), and a sweep where the
        # tool is merely absent scores as if the model had ignored it. Both paths point
        # at the same store ($SMARTER_COMPACT_STATE_DIR/context/sessions), so registering twice
        # is harmless.
        cfg["mcp"] = {
            "servers": {
                # Server key = product name, matching the plugin's own ctx.mcp.add (src/v2.mjs):
                # the two registration paths must yield the SAME tool names, or a session with
                # both sees two differently-named copies of recall. OpenCode namespaces with an
                # underscore, so the agent is offered `smarter-compact_smart_compact_search`
                # (confirmed in captured requests, and what the pointer must therefore name).
                "smarter-compact": {
                    "type": "local",
                    "command": ["node", f"{REPO_MOUNT}/plugin/lib/mcp/server.mjs"],
                    "environment": {
                        "HOME": CONTAINER_HOME,
                        "SMARTER_COMPACT_STATE_DIR": CONTAINER_STATE,
                    },
                    "enabled": True,
                    # OpenCode hides MCP tools behind its code-mode `execute` tool unless a server
                    # opts out. Haiku does not enter code mode at a 60k budget (0 `execute` calls
                    # across every smart trial, 22 in a 12k probe), so a route the agent never
                    # takes is no route: expose the tools directly. RECALL_CODEMODE=1 restores
                    # code-mode routing for comparison.
                    "codemode": os.environ.get("RECALL_CODEMODE", "0") == "1",
                },
            },
        }
    (cfg_dir / "opencode.json").write_text(json.dumps(cfg, indent=2) + "\n")


def extract_tokens(stream_path: Path) -> dict:
    """Sum step token reports from the opencode JSON stream (tolerant).

    Also carries two facts the stream is the only place to get them: `session_id`, which a chained
    run has to hand to the next task's `--session`, and `max_prompt_tokens`, the peak context the
    session actually reached — the number that says whether a run entered the regime it is
    supposed to be measuring.
    """
    totals = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "steps": 0,
              "max_prompt_tokens": 0, "session_id": None}
    if not stream_path.is_file():
        return totals
    for line in stream_path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if totals["session_id"] is None:
            sid = row.get("sessionID") or (row.get("part") or {}).get("sessionID")
            if isinstance(sid, str) and sid.startswith("ses"):
                totals["session_id"] = sid
        tokens = row.get("tokens") or (row.get("part") or {}).get("tokens")
        if isinstance(tokens, dict):
            # An aborted step (a stream killed mid-generation) reports zero tokens, and counting it
            # as a step re-ran the max() below against 0: a timeout that lands there records a peak
            # of nothing, so the single most direct measure of whether an arm hit the threshold is
            # lost exactly on the trials that ran longest. Only a step that produced tokens counts.
            if not (tokens.get("output") or tokens.get("input")):
                continue
            totals["max_prompt_tokens"] = max(
                totals["max_prompt_tokens"],
                (tokens.get("input", 0) or 0) + (tokens.get("cache", {}) or {}).get("read", 0)
                + (tokens.get("output", 0) or 0))
            totals["steps"] += 1
            totals["input"] += tokens.get("input", 0) or 0
            totals["output"] += tokens.get("output", 0) or 0
            totals["reasoning"] += tokens.get("reasoning", 0) or 0
            cache = tokens.get("cache") or {}
            totals["cache_read"] += cache.get("read", 0) or 0
            totals["cache_write"] += cache.get("write", 0) or 0
    return totals


PRICING = [
    # (substring of the model id, USD per 1M tokens: input, output, cache read, cache write(5m))
    # List API prices — used to price tokens the endpoint reports. Anthropic cache
    # writes bill at 1.25x input and reads at 0.1x input; we cannot tell a 5m write
    # from a 1h write in the stream, so every write is priced as 5m.
    (("haiku",), (1.00, 5.00, 0.10, 1.25)),
    (("sonnet",), (3.00, 15.00, 0.30, 3.75)),
    (("opus",), (15.00, 75.00, 1.50, 18.75)),
]
FREE_PRICE = (0.0, 0.0, 0.0, 0.0)  # self-hosted endpoints (Qwen on cairn2) have no per-token bill


def price_for(model: str | None) -> tuple[float, float, float, float]:
    """List price for a model id, or FREE_PRICE when it is unrecognised (self-hosted)."""
    needle = (model or "").lower()
    for names, price in PRICING:
        if any(n in needle for n in names):
            return price
    return FREE_PRICE


def estimate_cost(model: str | None, tokens: dict) -> float:
    """USD for a token tally: generated text (output + reasoning) at the output rate,
    cache reads at the read rate, cache writes at the write rate, prompt at input."""
    pin, pout, pread, pwrite = price_for(model)
    return (
        (tokens.get("input", 0) * pin
         + (tokens.get("output", 0) + tokens.get("reasoning", 0)) * pout
         + tokens.get("cache_read", 0) * pread
         + tokens.get("cache_write", 0) * pwrite)
        / 1_000_000
    )


def cost_or_none(model: str | None, tokens: dict) -> float | None:
    """estimate_cost, but None for a model with no price row rather than a confident $0.00.

    A self-hosted model matches no PRICING entry, so estimate_cost returns 0.0 and a whole
    cairn2 run reads as free work. Unpriced and free are different facts; the log line and the
    mlflow metrics already skip None, and cost_report still sums the notional number.
    """
    return None if price_for(model) == FREE_PRICE else round(estimate_cost(model, tokens), 4)


def exec_stream(container, cmd, out_path: Path, workdir: str | None = None,
                timeout: int | None = None, durable: bool = False) -> int | None:
    """Run cmd in the container, landing stdout+stderr in out_path, and survive being killed.

    A buffered exec_run keeps the stream in the result object, so a trial that hits its timeout
    loses all of it. durable=True is the stronger form and is how the agent runs: `cmd` redirects
    its own output to a FILE at out_path (under the bind mount) instead of to the exec pipe. The
    reason is the writer, not the reader — Node writes a file descriptor synchronously but
    block-buffers a pipe, so a killed process discards whatever is still in that buffer. Measured
    on tb-95k: a 952s trial reached 139 messages and 85,590 tokens of context in the store and left
    a 0-byte stream on the host. With a file, everything the agent wrote survives the SIGKILL.
    The tradeoff is that the host sees the file grow rather than a live copy of the pipe.

    Returns the exit code, or None when the agent was killed at the timeout.
    """
    api = container.client.api
    if durable:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(b"")        # the command appends to a file we just emptied
        exec_id = api.exec_create(container.id, cmd, stdout=True, stderr=True,
                                  workdir=workdir, tty=False)

        def pump():
            api.exec_start(exec_id, tty=False, stream=False, demux=False)
            return api.exec_inspect(exec_id).get("ExitCode")
    else:
        exec_id = api.exec_create(container.id, cmd, stdout=True, stderr=True,
                                  workdir=workdir, tty=False)

        def pump():
            with open(out_path, "wb") as fh:
                for chunk in api.exec_start(exec_id, tty=False, stream=True, demux=True):
                    out, err = chunk if isinstance(chunk, tuple) else (chunk, None)
                    if out:
                        fh.write(out)
                    if err:  # interleaving is approximate; extract_tokens skips non-JSON lines
                        fh.write(err)
                    fh.flush()
            return api.exec_inspect(exec_id).get("ExitCode")

    ex = ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(pump)
        try:
            return fut.result(timeout=timeout)
        except FutureTimeout:
            # Open the artifacts while the container can still be exec'd into: the caller's
            # post-agent chmod and stats both need a live container. This has to be a *blocking*
            # exec with its own deadline — a detached one is still queued when the kill lands, and
            # the ledger dir then stays root-owned 700 where no host-side read can follow it.
            chmod_ex = ThreadPoolExecutor(max_workers=1)
            try:
                chmod_ex.submit(
                    container.exec_run,
                    ["sh", "-c", "chmod -R a+rX /opt/trial /opt/agent/state /opt/agent/home "
                                 "2>/dev/null; true"],
                    stdout=False, stderr=False).result(timeout=10)
            except Exception:  # noqa: BLE001 - best effort before the kill
                pass
            finally:
                chmod_ex.shutdown(wait=False)
            try:  # killing the container closes the stream, which ends the pump thread
                container.kill()
            except Exception:  # noqa: BLE001
                pass
            fut.cancel()
            return None
    finally:
        ex.shutdown(wait=False)


def context_stats_from_dir(trial_dir: Path) -> dict:
    """Trim/recall counts for a trial whose container is already gone (killed on timeout).

    The same program the live path runs, pointed at the bind mount instead of exec'd into a
    container, so the two paths cannot drift. Two things only this path has to handle:

    * the writer may have died with rows still in the WAL. A read-only connection on the live files
      counted 0 where the ledger held 1, so the program copies db+wal aside and lets sqlite replay
      them;
    * a trial killed before the chmod watcher's first pass leaves the ledger dirs root-owned 700.
      Globbing finds nothing there and sqlite would happily open a NEW empty db, which reads as
      "this trial never trimmed"; that case is reported as None, not 0.
    """
    for d in (trial_dir / "state" / "context" / "sessions", trial_dir / "context" / "sessions"):
        if d.is_dir():
            try:
                next(d.iterdir(), None)
            except PermissionError:
                return {"trims": None, "recall_calls": 0}
    stats = _run_stats_program(trial_dir)
    return stats if stats is not None else {"trims": None, "recall_calls": 0}


def trial_model_id(trial_dir: Path) -> str | None:
    """Model id a finished trial used, read back from its own generated config
    (meta.json predates the `model` field, so past runs stay priceable)."""
    cfg = trial_dir / "home" / ".config" / "opencode" / "opencode.json"
    if not cfg.is_file():
        return None
    try:
        models = json.loads(cfg.read_text())["provider"]["local"]["models"]
        return next(iter(models), None)
    except Exception:  # noqa: BLE001 - provenance is best-effort
        return None


def mcp_tools_offered(trial_dir: Path) -> int:
    """How many recall tools OpenCode reported for our MCP server (0 = never connected).

    OpenCode logs `message="mcp connected" server=<key> tools=4` once per connection; this is
    the only post-hoc proof that the model *could* have called them, which is what makes a zero
    recall count interpretable instead of merely suspicious.
    """
    log_file = trial_dir / "home" / ".local/share/opencode/log/opencode.log"
    if not log_file.is_file():
        return 0
    try:
        found = MCP_CONNECTED_RE.findall(log_file.read_bytes())
    except OSError:
        return 0
    return max((int(n) for n in found), default=0)


# `openmem` is the key runs before the rename logged; keep matching it so old result dirs still
# summarize. New runs log the product key.
MCP_CONNECTED_RE = re.compile(rb'message="mcp connected" server=(?:smarter-compact|openmem) tools=(\d+)')

CAPTURE_PORT_DEFAULT = int(os.environ.get("BENCH_CAPTURE_PORT", "0"))  # 0 = pick a free port per trial
CAPTURE_SCRIPT = f"{REPO_MOUNT}/bench/swebench/request_capture.py"


def capture_health_probe(port: int) -> str:
    """In-container readiness wait: conda's python can take seconds to appear under load."""
    return (
        "import sys,time,urllib.request\n"
        "for _ in range(120):\n"
        f"    try:\n        urllib.request.urlopen('http://127.0.0.1:{port}/health', timeout=1)\n        sys.exit(0)\n"
        "    except Exception:\n        time.sleep(0.5)\n"
        "sys.exit(1)\n"
    )


def free_port() -> int:
    """A port nothing is holding right now. The trials run with `network: host`, so a fixed
    capture port would make concurrent trials share one listener and record into each other's
    dirs — one port per trial, chosen at launch."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_capture(container, upstream: str, port: int) -> bool:
    """Run the logging forwarder in-container so the trial's own requests are recorded.

    `mcp connected tools=4` proves OpenCode loaded the recall tools; it does not prove they
    reached the model. Only the outbound request answers that, and without it a smart trial's
    `recall_calls: 0` cannot tell "the model ignored the tool" from "the tool was never offered"
    — the distinction the whole masking-vs-recall claim rests on. Fail-open: no forwarder, no
    capture, real endpoint.
    """
    try:
        container.exec_run(["sh", "-c", "mkdir -p /opt/trial/requests /opt/trial/logs"], detach=True)
        container.exec_run(
            ["sh", "-c", f"python3 {CAPTURE_SCRIPT} >> /opt/trial/logs/capture.log 2>&1"], detach=True,
            environment={
                "CAPTURE_UPSTREAM": upstream,
                "CAPTURE_DIR": "/opt/trial/requests",
                "CAPTURE_PORT": str(port),
            },
        )
        probe = container.exec_run(["python3", "-c", capture_health_probe(port)], stdout=True, stderr=True)
    except Exception as e:  # noqa: BLE001 - capture is evidence, never a blocker
        log(f"capture: could not start forwarder ({type(e).__name__}: {e}); running uncaptured")
        return False
    if probe.exit_code != 0:
        log(f"capture: forwarder on :{port} never answered /health; running uncaptured "
            f"(see requests/../logs/capture.log)")
        return False
    return True


def request_facts(trial_dir: Path) -> dict:
    """What the trial's outbound requests actually offered (written by request_capture.py)."""
    facts = {"requests": 0, "recall_tools_offered": 0, "pointer_in_request": False, "capture_failed": 0}
    index = trial_dir / "requests" / "index.jsonl"
    if not index.is_file():
        return facts
    for line in index.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        facts["requests"] += 1
        facts["recall_tools_offered"] = max(facts["recall_tools_offered"], len(row.get("recall_tools") or []))
        facts["pointer_in_request"] = facts["pointer_in_request"] or bool(row.get("pointer"))
        if int(row.get("status") or 0) >= 400:
            facts["capture_failed"] += 1
    return facts


def cost_report(run_dir: Path, model: str | None) -> None:
    """Price every trial of a run, stamping meta.json so mlflow/summaries see cost_usd.
    Notional for subscription keys (no per-token invoice), actual spend for API keys."""
    per_arm: dict[str, dict] = collections.defaultdict(
        lambda: {"trials": 0, "resolved": 0, "cost": 0.0, "output": 0, "cache_read": 0})
    by_instance: dict[str, dict[str, dict]] = collections.defaultdict(dict)
    unknown = set()
    for meta_file in sorted(run_dir.glob("*/*/meta.json")):
        meta = json.loads(meta_file.read_text())
        model_id = model or meta.get("model") or trial_model_id(meta_file.parent)
        if model_id and price_for(model_id) == FREE_PRICE:
            unknown.add(model_id)
        cost = estimate_cost(model_id, meta.get("tokens") or {})
        stamped = {"cost_usd": cost_or_none(model_id, meta.get("tokens") or {})}
        if model_id:
            stamped["model"] = model_id
        if any(meta.get(k) != v for k, v in stamped.items()):  # idempotent re-runs
            meta.update(stamped)
            meta_file.write_text(json.dumps(meta, indent=2))
        arm = per_arm[meta.get("arm") or meta_file.parent.parent.name]
        arm["trials"] += 1
        arm["resolved"] += 1 if meta.get("resolved") else 0
        arm["cost"] += cost
        arm["output"] += (meta.get("tokens") or {}).get("output", 0)
        arm["cache_read"] += (meta.get("tokens") or {}).get("cache_read", 0)
        instance_id = meta.get("instance_id") or meta_file.parent.name
        arm_name = meta.get("arm") or meta_file.parent.parent.name
        by_instance[instance_id][arm_name] = {
            "cost": cost, "steps": (meta.get("tokens") or {}).get("steps") or 0,
            "prompt": sum((meta.get("tokens") or {}).get(k, 0) or 0
                          for k in ("input", "cache_read", "cache_write"))}
    print(f"cost report {run_dir.name}  (list prices; subscription keys are notional)")
    for arm, s in sorted(per_arm.items()):
        print(f"  {arm:5s} trials={s['trials']:3d} resolved={s['resolved']:2d} "
              f"output={s['output']:,} cache_read={s['cache_read']/1e6:.1f}M "
              f"cost=${s['cost']:.2f} (${s['cost'] / s['trials']:.2f}/trial)")
    pairs = [(v["none"], v["smart"]) for v in by_instance.values() if "none" in v and "smart" in v]
    if pairs:
        # Cost tracks steps x context, and on the hard tail one rollout that runs twice
        # as long moves the arm mean more than compression does — so the paired delta and
        # the per-step rate are the signal, and the arm means are just the bill.
        priced = any(n["cost"] or s["cost"] for n, s in pairs)
        # A priced run reports dollars; a self-hosted one has no bill, so the compression
        # signal is the prompt it actually sent (input + cache reads + cache writes).
        value = (lambda t: t["cost"]) if priced else (lambda t: t["prompt"])
        show = (lambda v: f"${v:.4f}") if priced else (lambda v: f"{v:,.0f}")
        deltas = [value(s) - value(n) for n, s in pairs]
        rate_none = statistics.mean(value(n) / max(n["steps"], 1) for n, _ in pairs)
        rate_smart = statistics.mean(value(s) / max(s["steps"], 1) for _, s in pairs)
        pct = (rate_smart / rate_none - 1) * 100 if rate_none else 0.0
        delta_txt = (f"{statistics.median(deltas):+.4f} usd" if priced
                     else f"{statistics.median(deltas):+,.0f} prompt tokens")
        print(f"  paired {len(pairs)} instances: median delta={delta_txt}/trial, "
              f"smart cheaper on {sum(1 for d in deltas if d < 0)}/{len(pairs)}")
        print(f"  {'cost' if priced else 'prompt'}/step none={show(rate_none)} "
              f"smart={show(rate_smart)} ({pct:+.1f}%) at "
              f"{statistics.mean(n['steps'] for n, _ in pairs):.0f} vs "
              f"{statistics.mean(s['steps'] for _, s in pairs):.0f} steps/trial")
    if unknown:
        print(f"  priced at $0 (no list price for): {', '.join(sorted(unknown))}")
    print(f"  push to mlflow: run_bench.py --run-id {run_dir.name} --backfill-mlflow "
          f"--display-name <display-name>")


CHAIN_PREAMBLE = """You are continuing ONE long working session in this repository. Tasks 1-{prev} are finished and their changes are gone: the working tree has been reset to the base commit. Do not re-open, re-apply or reference earlier tasks; treat what is in your history as work already shipped.

New task:
"""


def _chunks(items: list, size: int):
    if not size or size >= len(items):
        yield items
        return
    for i in range(0, len(items), size):
        yield items[i:i + size]


def group_for_chaining(instances: list, arms: list, chain_max: int) -> list:
    """[(arm, chain_label, instances)] in an order both arms see identically.

    Grouping by repo keeps the tasks of a chain in one codebase, so earlier turns stay relevant to
    later ones; the order is fixed (sorted ids) because a chain whose tasks differ between arms
    would not be a comparison of anything.
    """
    by_repo: dict[str, list] = {}
    for inst in sorted(instances, key=lambda i: i["instance_id"]):
        by_repo.setdefault(inst.get("repo") or inst["instance_id"].rsplit("-", 1)[0], []).append(inst)
    groups = []
    for arm in arms:
        for repo in sorted(by_repo):
            for part, chunk in enumerate(_chunks(by_repo[repo], chain_max)):
                groups.append((arm, repo if not chain_max else f"{repo}-p{part}", chunk))
    return groups


def run_chain(client, insts: list, arm: str, args, run_dir: Path, repo: str) -> list:
    """One session, many tasks: each task is a fresh container on its own base commit, but every
    task continues the same opencode session against one shared trim ledger, so the conversation
    grows past what any single task could produce. Scores stay per-instance and comparable to the
    flat runs because each task's patch is taken against its own base and the tree is reset after.
    """
    chain_dir = run_dir / "_chains" / f"{arm}-{repo.replace('/', '_')}-{args.run_id}"
    chain_dir.mkdir(parents=True, exist_ok=True)
    chain = {"dir": str(chain_dir), "label": repo, "pos": 0, "session_id": None, "trims": None,
             "recall": {}}
    metas = []
    for pos, inst in enumerate(insts):
        chain["pos"] = pos
        meta = run_trial(client, inst, arm, args, run_dir, chain=chain)
        if meta.get("session_id"):
            chain["session_id"] = meta["session_id"]
        elif pos + 1 < len(insts):
            log(f"chain={repo} arm={arm} task={meta['instance_id']} gave no session id; "
                f"the remaining {len(insts) - pos - 1} task(s) start a fresh session")
        metas.append(meta)
    log(f"chain={repo} arm={arm} finished {len(metas)} tasks session={chain['session_id']} "
        f"trims={chain['trims']} recall={sum((chain['recall'] or {}).values())}")
    return metas


def run_trial(client, inst, arm, args, run_dir: Path, chain: dict | None = None) -> dict:
    iid = inst["instance_id"]
    trial_dir = run_dir / arm / iid
    (trial_dir / "logs").mkdir(parents=True, exist_ok=True)
    # Flat runs keep the agent home + trim ledger inside the trial dir, as they always have. A
    # chained run points them at its chain dir instead: that shared store IS the long horizon, and
    # the per-trial dirs keep prompt/patch/requests/score so scoring and reporting never change.
    if chain is None:
        agent_home, agent_state = trial_dir / "home", trial_dir / "state"
    else:
        agent_home, agent_state = Path(chain["dir"]) / "home", Path(chain["dir"]) / "state"
    agent_home.mkdir(parents=True, exist_ok=True)
    agent_state.mkdir(parents=True, exist_ok=True)
    if chain is not None:
        for name, target in (("home", agent_home), ("state", agent_state)):
            link = trial_dir / name
            if not link.is_symlink() and not link.exists():
                link.symlink_to(target)
    meta = {"instance_id": iid, "arm": arm, "image": inst["image"], "code_sha": CODE_SHA,
            # The thresholds are what make a peak interpretable (a 59k trial at trim_at=60k and one
            # at trim_at=40k are different results), and without them a results dir can only be read
            # through the run id that named them.
            "trim_at": args.trim_at, "native_at": args.native_at,
            "context_limit": args.context_limit}
    if chain is not None:
        meta.update({"chain": chain["label"], "chain_pos": chain["pos"],
                     "chain_session": chain["session_id"]})
    t0 = time.time()
    container = None
    log(f"{iid} arm={arm} start (pulls image if not local)")
    try:
        cname = f"scbench-{iid}-{arm}-{args.run_id}"
        try:  # a previous interrupted run may have left a container on this fixed name
            client.containers.get(cname).remove(force=True)
        except docker.errors.NotFound:
            pass
        container = client.containers.run(
            inst["image"],
            command=["sleep", str(args.timeout + 600)],
            detach=True,
            name=cname,
            network="host",
            working_dir="/testbed",
            volumes={
                str(args.opencode_bin_dir): {"bind": "/opt/opencodebin", "mode": "ro"},
                str(args.node_bin_dir): {"bind": "/opt/node", "mode": "ro"},
                str(REPO_ROOT): {"bind": REPO_MOUNT, "mode": "ro"},
                str(trial_dir): {"bind": "/opt/trial", "mode": "rw"},
                str(agent_home): {"bind": "/opt/agent/home", "mode": "rw"},
                str(agent_state): {"bind": "/opt/agent/state", "mode": "rw"},
            },
        )
        # The session store makes itself 0700 (private by design) and the pass below opens the
        # artifacts up only once the trial finishes, so while a trial runs a watcher on the host
        # gets EACCES on exactly the trial it is watching. Nudge the evidence dirs open while the
        # agent works; the loop dies with the container, and it skips /opt/trial/home (npm/opencode
        # caches) so it stays cheap.
        container.exec_run(
            ["sh", "-c", "while :; do chmod -R a+rX /opt/trial/requests /opt/trial/logs "
                          "/opt/agent/state 2>/dev/null; sleep 5; done"],
            detach=True,
        )
        models = [args.model]
        # Route the trial through the in-container logging forwarder so its own requests prove
        # what the model was offered (BENCH_CAPTURE=0 to skip).
        base_url = None
        upstream = args.provider_cfg["options"]["baseURL"]
        capture_port = CAPTURE_PORT_DEFAULT or free_port()
        if os.environ.get("BENCH_CAPTURE", "1") == "1" and start_capture(container, upstream, capture_port):
            route = re.match(r"https?://[^/]+(/.*)?$", upstream)
            base_url = f"http://127.0.0.1:{capture_port}{(route.group(1) if route and route.group(1) else '')}"
            meta["capture_port"] = capture_port
        write_agent_config(agent_home, args.provider_cfg, models, arm, REPO_MOUNT,
                           trim_at=args.trim_at, context_limit=args.context_limit, native_at=args.native_at,
                           base_url=base_url)
        image_env = dict(
            e.split("=", 1) for e in (container.image.attrs.get("Config", {}).get("Env") or []) if "=" in e
        )
        image_path = image_env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
        env = {
            "HOME": "/opt/agent/home",
            "XDG_CONFIG_HOME": "/opt/agent/home/.config",
            "XDG_DATA_HOME": "/opt/agent/home/.local/share",
            "XDG_CACHE_HOME": "/opt/agent/home/.cache",
            "PATH": f"/opt/node:/opt/opencodebin:{image_path}",
            "SMARTER_COMPACT_STATE_DIR": "/opt/agent/state",
            "SMARTER_COMPACT_AUTO_PROXY": "0",
            "CONTEXT_TRIM_AT_TOKENS": str(args.trim_at),
        }
        prompt = PROBLEM_LOCK.format(problem_statement=inst["problem_statement"])
        if chain is not None and chain["pos"] > 0:
            prompt = CHAIN_PREAMBLE.format(prev=chain["pos"]) + prompt
        (trial_dir / "prompt.txt").write_text(prompt)
        # Keep test-run byproducts out of `git add -A` (and the patch): uncommitted exclude file.
        container.exec_run(
            ["sh", "-c", "cd /testbed && printf '__pycache__/\\n*.pyc\\n.pytest_cache/\\nbuild/\\n*.egg-info/\\n.tox/\\n.coverage\\n' >> .git/info/exclude"],
            stdout=False, stderr=False,
        )
        oc_cmd = [
            "/opt/opencodebin/opencode", "run", "--standalone", "--print-logs", "--auto",
            "--format", "json", "--model", f"local/{args.model}", "-",
        ]
        if chain is not None and chain.get("session_id"):
            # Same session id, new container: the conversation and its trim ledger come from the
            # chain store, so context keeps accumulating across tasks.
            oc_cmd[-1:-1] = ["--session", chain["session_id"]]
            meta["chain_resumed"] = True
        # The agent writes its own stream to a file (see exec_stream): a pipe would be block-buffered
        # inside the agent process and lost whole when a timeout kills it.
        stream_in_container = "/opt/trial/logs/agent_stream.jsonl"
        inner = ("cd /testbed && " + " ".join(shlex.quote(c) for c in oc_cmd)
                 + " < /opt/trial/prompt.txt > " + stream_in_container + " 2>&1")
        exit_code = exec_stream(
            container,
            ["env"] + [f"{k}={v}" for k, v in env.items()] + ["sh", "-c", inner],
            trial_dir / "logs" / "agent_stream.jsonl",
            timeout=args.timeout, durable=True)
        if exit_code is None:
            meta["agent_exit"] = -1
            meta["error"] = "agent: timeout"
        else:
            meta["agent_exit"] = exit_code
        meta["agent_seconds"] = round(time.time() - t0, 1)
        meta["tokens"] = extract_tokens(trial_dir / "logs" / "agent_stream.jsonl")
        meta["session_id"] = meta["tokens"].pop("session_id", None)
        meta["max_prompt_tokens"] = meta["tokens"].pop("max_prompt_tokens", 0)
        meta["model"] = args.model
        meta["cost_usd"] = cost_or_none(args.model, meta["tokens"])

        if exit_code is None:
            # Nothing else can be read from a killed container: no patch, no chain reset, and
            # `patch_bytes` stays unset so score_trial is skipped and the trial is unscored
            # rather than scored as an empty patch. The trim ledger and the request capture are on
            # the bind mounts, so those still land in meta below.
            stats = context_stats_from_dir(trial_dir)
        else:
            patch = container.exec_run(
                ["sh", "-c", "cd /testbed && git add -A && git diff --cached"],
                stdout=True, stderr=True, tty=False,
            )
            patch_text = patch.output.decode(errors="replace")
            (trial_dir / "patch.diff").write_text(patch_text)
            meta["patch_bytes"] = len(patch_text)
            if chain is not None:
                # The conversation stays whole; the tree does not. Each task is scored against its own
                # base commit, so the next one starts from a pristine checkout.
                reset = container.exec_run(
                    ["sh", "-c", "cd /testbed && git reset --hard -q && git clean -fdq"],
                    stdout=True, stderr=True, tty=False)
                meta["chain_reset_exit"] = reset.exit_code

            # Artifacts land root-owned; make them readable by the bench user before removal.
            container.exec_run(["sh", "-c", "chmod -R a+rX /opt/trial /opt/agent/state "
                                            "/opt/agent/home 2>/dev/null; true"], stdout=False, stderr=False)

        # Store-trim + recall evidence (in-container: a WAL db read from the host can demand
        # recovery or miss uncheckpointed rows). `recall_available` matters as much as the
        # count: a smart trial where the tools never answered is a masking result, not a
        # mechanism result, and must not be scored as one.
        meta["trims_source"] = "host-dir" if exit_code is None else "in-container"
        stats = (context_stats_from_dir(trial_dir) if exit_code is None
                 else context_stats_in_container(container))
        # The store's own ledger is the source: one `events` row per trim (with the tokens around
        # it) and one `messages` row per tool call. recall_server_calls comes from the recall MCP
        # server's own log, which current builds do not write -- kept because a nonzero count there
        # is the only proof the server was reachable, and 0 means nothing either way.
        recall = stats.get("recall_ledger") or {}
        if chain is None:
            meta["trims"] = stats.get("trims")
            meta["recall_calls"] = stats.get("recall_calls") or 0
            meta["recall_tools"] = recall
            meta["recall_server_calls"] = stats.get("recall_server_calls") or {}
        else:
            # A chain has ONE ledger, so the raw counts are cumulative: keep them, and report the
            # task's own delta so a trim storm can be pinned to the task that caused it.
            total_trims = stats.get("trims")
            meta["trims_total"] = total_trims
            prev_recall = chain.get("recall") or {}
            meta["recall_tools"] = {k: v - prev_recall.get(k, 0) for k, v in recall.items()}
            meta["recall_calls"] = sum(meta["recall_tools"].values())
            meta["recall_server_calls"] = stats.get("recall_server_calls") or {}
            meta["trims"] = (total_trims - chain["trims"]
                             if total_trims is not None and chain.get("trims") is not None
                             else total_trims)
            chain["trims"], chain["recall"] = total_trims, recall
        if stats.get("trims") is not None:
            meta["trims_dropped"] = stats.get("trims_dropped")
            meta["trims_peak_before"] = stats.get("trims_peak_before")
        # The store's peak stands on its own: on a trial whose stream never made it out of the
        # agent's buffer it is the only surviving measure of how big the context got.
        if stats.get("usage_peak_ctx"):
            meta["peak_ctx_store"] = stats["usage_peak_ctx"]
        meta["ledger_unreadable"] = bool(stats.get("ledger_unreadable"))
        if arm == "smart":
            # Availability comes from the request itself when the forwarder ran: `mcp connected
            # tools=4` proves OpenCode loaded the tools, only the request proves the model was
            # offered them. The two separate "never offered" from "offered and ignored", which
            # is otherwise the same zero in the data — and one whole sweep read it wrong.
            req = request_facts(trial_dir)
            for key, value in req.items():
                meta[f"req_{key}"] = value
            offered = req["recall_tools_offered"] or mcp_tools_offered(trial_dir)
            meta["recall_tools_offered"] = offered
            meta["recall_available"] = offered > 0 or meta["recall_calls"] > 0
            if req["requests"] and not req["recall_tools_offered"]:
                meta["recall_warning"] = "recall tools connected but absent from the outbound request"
            elif meta["trims"] and req["requests"] and not req["pointer_in_request"]:
                meta["recall_warning"] = "trimmed but no pointer reached the model"
            elif meta["trims"] and not meta["recall_calls"]:
                meta["recall_warning"] = (
                    f"{offered} recall tools offered but unused after trimming" if offered
                    else "recall tools never connected: check the mcp config"
                )
            if meta.get("recall_warning"):
                log(f"{iid} arm={arm} WARNING {meta['recall_warning']}")
    except Exception as e:  # noqa: BLE001 - one trial's failure must not kill the run
        meta.setdefault("error", f"{type(e).__name__}: {e}")
    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:  # noqa: BLE001
                pass
    # Score-as-you-go: harness runs this instance's tests in a fresh container
    # right after the patch lands, so `resolved` is live per trial — in a chain too, which is the
    # only place the number gets tied to the task that earned it. Measured cost of this call in the
    # flat run was 26s median on django, 95s on astropy (trial wall minus agent wall), so keeping
    # it inside the loop is nearly free; a claim that django scoring cost ten minutes here was an
    # orphaned container from a killed parent, not a measurement. `--defer-score` moves it to
    # `--score-only` for when a repo's suite really is the long pole.
    if args.defer_score:
        meta["score_deferred"] = True
    if meta.get("patch_bytes") is not None and not args.defer_score:
        try:
            meta["resolved"] = score_trial(inst, trial_dir, arm, args)
        except Exception as e:  # noqa: BLE001 - scoring must not fail the trial
            meta["score_error"] = f"{type(e).__name__}: {e}"
    (trial_dir / "meta.json").write_text(json.dumps(meta, indent=2))
    mlflow_log_trial(meta, args)
    cost_text = f"cost=${meta['cost_usd']:.2f} " if meta.get("cost_usd") is not None else ""
    log(f"{iid} arm={arm} done exit={meta.get('agent_exit')} secs={meta.get('agent_seconds')} patch={meta.get('patch_bytes')}B {cost_text}trims={meta.get('trims')} recall={meta.get('recall_calls')} resolved={meta.get('resolved')} {meta.get('error', '')}{meta.get('score_error', '')}")
    return meta


def score_trial(inst: dict, trial_dir: Path, arm: str, args) -> bool | None:
    """Run the swebench harness for just this instance; return resolved (None on infra error)."""
    iid = inst["instance_id"]
    subset = trial_dir / "score-instances.json"
    subset.write_text(json.dumps([inst]))
    patch_file = trial_dir / "patch.diff"
    preds = trial_dir / "score-predictions.jsonl"
    preds.write_text(json.dumps({
        "instance_id": iid,
        "model_name": f"{args.display_name}-{arm}",
        "model_name_or_path": f"{args.display_name}-{arm}",
        "model_patch": patch_file.read_text() if patch_file.is_file() else "",
    }) + "\n")
    report_dir = trial_dir / "score"
    report_dir.mkdir(exist_ok=True)
    eval_id = f"{args.run_id}-{arm}-{iid}"
    subprocess.run(
        [sys.executable, "-m", "swebench.harness.run_evaluation",
         "-d", str(subset), "-p", str(preds),
         "-id", eval_id, "--max_workers", "1", "--report_dir", str(report_dir)],
        capture_output=True, text=True, check=False,
    )
    report_file = report_dir / f"{args.display_name}-{arm}.{eval_id}.json"
    if not report_file.is_file():
        return None
    report = json.loads(report_file.read_text())
    if isinstance(report.get("resolved_ids"), list):  # swebench v3 schema
        if iid in report["resolved_ids"]:
            return True
        return None if iid in report.get("error_ids", []) else False
    entry = report.get(iid)
    return None if entry is None else bool(entry.get("resolved"))


_MLFLOW_WARNED = False
_MLFLOW_EXP_CACHE = {}


def mlflow_enabled() -> bool:
    return os.environ.get("BENCH_MLFLOW", "") not in ("", "0")


def mlflow_uri() -> str:
    return os.environ.get("BENCH_MLFLOW_URI") or "http://127.0.0.1:5000"


def _mlflow_api(path: str, payload: dict, base: str = "/api/2.0/mlflow") -> dict | None:
    req = urllib.request.Request(
        mlflow_uri().rstrip("/") + base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read() or b"{}")


def _mlflow_experiment_id(name: str) -> str:
    if name in _MLFLOW_EXP_CACHE:
        return _MLFLOW_EXP_CACHE[name]
    try:  # create is idempotent-ish: already-exists raises, then we look it up
        _mlflow_api("/experiments/create", {"name": name})
    except urllib.error.HTTPError:
        pass
    # get-by-name is GET-only on mlflow 3; search needs view_type=ALL to see
    # non-default experiments. Match client-side to dodge filter dialect drift.
    body = _mlflow_api("/experiments/search", {"max_results": 200, "view_type": "ALL"})
    matches = [e for e in (body or {}).get("experiments", []) if e.get("name") == name]
    exp_id = matches[0]["experiment_id"] if matches else None
    if not exp_id:
        raise RuntimeError(f"mlflow experiment {name!r} not resolvable")
    _MLFLOW_EXP_CACHE[name] = exp_id
    return exp_id


def _mlflow_log(experiment: str, run_name: str, tags: dict, metrics: dict) -> None:
    """One-shot run: create, log metrics, finish. REST API -> no client dependency."""
    exp_id = _mlflow_experiment_id(experiment)
    run = _mlflow_api("/runs/create", {
        "experiment_id": exp_id,
        "run_name": run_name,
        "tags": [{"key": k, "value": str(v)} for k, v in tags.items()],
    })
    run_id = run["run"]["info"]["run_id"]
    now = int(time.time() * 1000)
    _mlflow_api("/runs/log-batch", {
        "run_id": run_id,
        "metrics": [{"key": k, "value": float(v), "timestamp": now} for k, v in metrics.items()],
    })
    _mlflow_api("/runs/update", {"run_id": run_id, "status": "FINISHED"})


def mlflow_prune(display_name: str, run_id: str) -> int:
    """Soft-delete existing runs for this run_id so backfills stay idempotent.
    (This mlflow build only answers runs/search over ajax-api.)"""
    exp_id = _mlflow_experiment_id(f"swebench-{display_name}")
    body = _mlflow_api(
        "/runs/search", {"experiment_ids": [exp_id], "max_results": 1000},
        base="/ajax-api/2.0/mlflow",
    )
    deleted = 0
    for r in (body or {}).get("runs", []):
        tags = r.get("data", {}).get("tags") or {}
        if isinstance(tags, list):
            tags = {t["key"]: t.get("value") for t in tags}
        if tags.get("run_id") != run_id:
            continue
        rid = r["info"].get("run_id") or r["info"].get("run_uuid")
        try:
            _mlflow_api("/runs/delete", {"run_id": rid})
            deleted += 1
        except Exception:  # noqa: BLE001 - already deleted / transient
            pass
    return deleted


def _mlflow_guard(fn) -> None:
    global _MLFLOW_WARNED
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - observability must never fail the bench
        if not _MLFLOW_WARNED:
            log(f"mlflow logging disabled ({mlflow_uri()} unreachable?): {type(e).__name__}: {e}")
            _MLFLOW_WARNED = True


def mlflow_log_trial(meta: dict, args) -> None:
    if not mlflow_enabled():
        return
    tags = {k: str(meta[k]) for k in ("instance_id", "arm", "image") if k in meta}
    tags["run_id"] = args.run_id
    if meta.get("model"):
        tags["model"] = str(meta["model"])
    if meta.get("error"):
        tags["error"] = str(meta["error"])[:200]
    metrics = {}
    for key in ("agent_seconds", "patch_bytes", "agent_exit", "trims", "recall_calls", "resolved", "cost_usd"):
        if meta.get(key) is not None:
            metrics[key] = meta[key]
    for key, val in (meta.get("tokens") or {}).items():
        if isinstance(val, (int, float)):
            metrics[f"tokens_{key}"] = val
    _mlflow_guard(lambda: _mlflow_log(
        f"swebench-{getattr(args, 'display_name', 'model')}",
        f"{meta['instance_id']}-{meta['arm']}", tags, metrics,
    ))


def mlflow_log_scores(run_id: str, display_name: str, arm: str, report: dict) -> None:
    if not mlflow_enabled() or not report:
        return
    # swebench v3 reports: resolved_ids/unresolved_ids lists.
    # swebench v2 reports: flat {instance_id: {...resolved...}} dict.
    # Some versions wrap the flat dict in per_instance.
    if isinstance(report.get("resolved_ids"), list):
        per = {iid: {"resolved": True} for iid in report["resolved_ids"]}
        per.update({iid: {"resolved": False} for iid in report.get("unresolved_ids", [])})
    else:
        per = report.get("per_instance") if isinstance(report.get("per_instance"), dict) else \
            {k: v for k, v in report.items() if isinstance(v, dict)}
    metrics = {"resolved": sum(1 for r in per.values() if r.get("resolved")), "total": len(per)}
    metrics.update({f"resolved__{iid}": 1 if r.get("resolved") else 0 for iid, r in per.items()})
    _mlflow_guard(lambda: _mlflow_log(
        f"swebench-{display_name}", f"score-{arm}",
        {"run_id": run_id, "arm": arm, "kind": "score"}, metrics,
    ))


CONTEXT_STATS_PY = r"""
import glob, json, os, shutil, sqlite3, sys, tempfile
base = sys.argv[1]
# The store's own db (not opencode's): `events` has one row per trim with the token counts around
# it, and `messages` has one row per tool call, so both numbers come from what the plugin recorded
# rather than from anything the model echoed back.
TRIMS_QUERY = ("select count(*), coalesce(sum(dropped), 0), coalesce(max(tokens_before), 0) "
               "from events where kind = 'trim'")
RECALL_NAME = ("(name like '%smart_compact_search%' or name like '%smart_compact_sql%' "
               "or name like '%smart_compact_context%')")
CALLS_QUERY = "select count(*) from messages where kind = 'tool_use' and " + RECALL_NAME
DONE_QUERY = "select count(*) from messages where kind = 'tool_result' and " + RECALL_NAME
LEDGER_QUERY = ("select name, count(*) from messages where kind = 'tool_use' and " + RECALL_NAME
                + " group by name")
# The store's own measure of how big the context got, from the usage rows it logged. It stands on
# its own when the stream is missing or truncated, which is what a killed trial looks like.
USAGE_QUERY = ("select max(coalesce(input_tokens,0) + coalesce(cache_read_tokens,0)) from events "
               "where kind = 'usage'")
# `<trial>/state` is bind-mounted at /opt/agent/state and also sits under the /opt/trial mount, so
# globbing both roots read every db twice and reported double every count -- the store said 3 and
# the runner said 6 in every run before this dedupe. Dedupe by (dev, inode): a bind mount is not a
# symlink, so realpath gives two different strings for one file.
seen = set()
def once(patterns):
    for root in (os.path.join(base, 'state'), base):
        for pat in patterns:
            for p in glob.glob(os.path.join(root, pat)):
                try: key = (os.stat(p).st_dev, os.stat(p).st_ino)
                except OSError: key = ('path', p)
                if key not in seen:
                    seen.add(key); yield p
trims = dropped = peak_before = calls = completed = 0
usage_peak = None
# A ledger dir that exists but cannot be listed is not an empty one: the artifacts are created
# root-owned inside the container, and a trial killed before the chmod watcher reached this dir
# leaves it 700. Reporting that as 0 trims would read as "this trial never trimmed".
unreadable = any(os.path.isdir(d) and not os.access(d, os.R_OK)
                 for d in (os.path.join(base, 'state', 'context', 'sessions'),
                           os.path.join(base, 'context', 'sessions')))
by_name: dict = {}
found_db = events_found = ledger_error = False
for db in once(['context/sessions/*.db']):
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # db and -wal only: a copied -shm carries the writer process's own recovery state, which
            # makes sqlite skip the WAL it should replay (measured: a killed trial read 0 with -shm
            # copied, 1 without it). Copying at all is what lets a read follow a dead writer -- a
            # read-only connection to a db with an open WAL fails outright.
            for suffix in ('', '-wal'):
                src = db + suffix
                if os.path.isfile(src):
                    dst = os.path.join(tmp, os.path.basename(src))
                    shutil.copyfile(src, dst); os.chmod(dst, 0o600)
            con = sqlite3.connect(os.path.join(tmp, os.path.basename(db)))
            con.text_factory = str
            found_db = True
            try:
                n, drop, before = con.execute(TRIMS_QUERY).fetchone()
                trims += n or 0; dropped += drop or 0; peak_before = max(peak_before, before or 0)
                events_found = True
            except Exception:
                pass          # no events table: trims stays unknown rather than zero
            try:
                u = con.execute(USAGE_QUERY).fetchone()[0]
                if u and (usage_peak is None or u > usage_peak):
                    usage_peak = u
            except Exception:
                pass
            try:
                calls += con.execute(CALLS_QUERY).fetchone()[0] or 0
                completed += con.execute(DONE_QUERY).fetchone()[0] or 0
                for name, n in con.execute(LEDGER_QUERY).fetchall():
                    by_name[name] = by_name.get(name, 0) + (n or 0)
            except Exception:
                pass
            con.close()
    except Exception:
        ledger_error = True   # a db that would not open: counts so far are a lower bound
# tool-calls.jsonl is appended by the recall MCP server itself, so a nonzero count proves the server
# was reachable from the agent. Current builds do not write it; it stays read so a build that does
# is not silently missed.
server = {}
for path in once(['context/tool-calls.jsonl']):
    try:
        with open(path) as fh:
            for raw in fh:
                try: name = json.loads(raw).get('tool')
                except Exception: continue
                if name: server[name] = server.get(name, 0) + 1
    except Exception:
        pass
print(json.dumps({'trims': trims if events_found else None, 'trims_dropped': dropped,
                  'trims_peak_before': peak_before, 'usage_peak_ctx': usage_peak,
                  'recall_calls': calls,
                  'recall_ledger': by_name, 'recall_completed': completed,
                  'recall_server_calls': server, 'ledger_found': found_db,
                  'events_found': events_found, 'ledger_error': ledger_error,
                  'ledger_unreadable': unreadable}))
"""


def _run_stats_program(trial_dir: Path) -> dict | None:
    """Run the one stats program against a trial dir on this machine, or None if it did not run."""
    try:
        out = subprocess.run([sys.executable, "-c", CONTEXT_STATS_PY, str(trial_dir)],
                             capture_output=True, text=True, timeout=120)
    except Exception:  # noqa: BLE001
        return None
    for line in reversed(out.stdout.strip().splitlines()):
        try:
            return json.loads(line)
        except Exception:  # noqa: BLE001
            continue
    return None


def context_stats_in_container(container) -> dict:
    """Trim/recall counts for a live trial, read where the ledger lives.

    Same program the host fallback runs, so the two paths cannot drift.
    """
    r = container.exec_run(["python3", "-c", CONTEXT_STATS_PY, "/opt/trial"],
                           stdout=True, stderr=True, tty=False)
    try:
        out = r.output.decode()
    except Exception:  # noqa: BLE001
        return {"trims": None, "recall_calls": 0}
    for line in reversed(out.strip().splitlines()):
        try:
            return json.loads(line)
        except Exception:  # noqa: BLE001
            continue
    return {"trims": None, "recall_calls": 0}


def score_arms(run_dir: Path, instances: list, instance_ids: list, arms: list, display_name: str, run_id: str, parallel: int) -> None:
    # Predictions + scoring (swebench harness, same images, tests in-container).
    subset_path = run_dir / "instances.json"
    subset_path.write_text(json.dumps(instances))
    for arm in arms:
        preds = run_dir / f"predictions-{arm}.jsonl"
        with open(preds, "w") as f:
            for iid in instance_ids:
                p = run_dir / arm / iid / "patch.diff"
                patch = p.read_text() if p.is_file() else ""
                f.write(json.dumps({
                    "instance_id": iid,
                    "model_name": f"{display_name}-{arm}",
                    "model_name_or_path": f"{display_name}-{arm}",
                    "model_patch": patch,
                }) + "\n")
        report_dir = run_dir / "reports" / arm
        report_dir.mkdir(parents=True, exist_ok=True)
        log(f"scoring arm={arm} -> {report_dir}")
        cmd = [
            sys.executable, "-m", "swebench.harness.run_evaluation",
            "-d", str(subset_path), "-p", str(preds),
            "-id", f"{run_id}-{arm}", "--max_workers", str(parallel),
            "--report_dir", str(report_dir),
        ]
        subprocess.run(cmd, check=False)
        report_file = report_dir / f"{display_name}-{arm}.{run_id}-{arm}.json"
        if report_file.is_file():
            try:
                mlflow_log_scores(run_id, display_name, arm, json.loads(report_file.read_text()))
            except Exception as e:  # noqa: BLE001
                log(f"could not read report {report_file}: {e}")

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instances", default=",".join(DEFAULT_INSTANCES))
    ap.add_argument("--arms", default="none,smart")
    ap.add_argument("--model", help="model id at the endpoint, e.g. Qwen/Qwen3.8-Flash-Next-FP8 (not needed for --score-only/--backfill-mlflow)")
    ap.add_argument("--display-name", default="qwen-3.8-flash-next")
    ap.add_argument("--provider", default="cairn2", help="provider id in the HOST opencode.json to copy endpoint+key from")
    ap.add_argument("--trim-at", type=int, default=40000)
    ap.add_argument("--context-limit", type=int, default=102400, help="model context window; native compaction is pinned to (limit - reserved)")
    ap.add_argument("--native-at", type=int, default=None, help="native opencode compaction trigger in tokens for BOTH arms (default: none=trim_at, smart=1.5x trim capped)")
    ap.add_argument("--timeout", type=int, default=2400, help="per-trial agent timeout (s)")
    ap.add_argument("--chain", action="store_true", help="one session per repo: that repo's "
        "instances run back to back in the SAME session (tree reset between tasks), so context "
        "accumulates across tasks the way a real work session does")
    ap.add_argument("--chain-max", type=int, default=0, help="split a repo's chain into batches of "
        "at most N tasks (0 = one chain per repo)")
    ap.add_argument("--defer-score", action="store_true", help="skip the per-trial test run and "
        "score afterwards with --score-only (per-trial scoring costs ~26s on django, ~95s on "
        "astropy; only worth deferring for a suite that really is the long pole)")
    ap.add_argument("--parallel", type=int, default=2)
    ap.add_argument("--evict-images", action="store_true", help="remove each instance's image after its last trial (layer-shared base layers survive); avoids mid-run docker prunes")
    ap.add_argument("--run-id", default=time.strftime("%Y%m%d-%H%M%S"))
    ap.add_argument("--opencode-bin-dir", default=str(Path.home() / ".opencode" / "bin"))
    ap.add_argument("--node-bin-dir", default=None, help="dir containing a node>=24 binary (default: resolve `node`)")
    ap.add_argument("--score-only", action="store_true", help="skip trials; build predictions and score existing patches in --run-id")
    ap.add_argument("--cost-report", action="store_true", help="price every trial of --run-id (stamps meta.json + prints per-arm totals) and exit")
    ap.add_argument("--backfill-mlflow", action="store_true", help="log existing trials+scores of --run-id to mlflow without running anything")
    ap.add_argument("--skip-batch-score", action="store_true", help="trials already score individually; skip the end-of-run aggregate scoring pass")
    args = ap.parse_args()

    instance_ids = [i for i in args.instances.split(",") if i.strip()]
    arms = [a for a in args.arms.split(",") if a.strip()]
    assert set(arms) <= {"none", "smart"}, "arms must be none and/or smart"
    run_dir = BENCH_DIR / "results" / args.run_id

    if args.cost_report:
        cost_report(run_dir, args.model)
        return

    if args.backfill_mlflow:
        os.environ["BENCH_MLFLOW"] = "1"
        try:
            pruned = mlflow_prune(args.display_name, args.run_id)
            log(f"pruned {pruned} existing mlflow runs for {args.run_id}")
        except Exception as e:  # noqa: BLE001
            log(f"prune skipped: {type(e).__name__}: {e}")
        n = 0
        for meta_file in sorted(run_dir.glob("*/*/meta.json")):
            mlflow_log_trial(json.loads(meta_file.read_text()), args)
            n += 1
        for arm in arms:
            rf = run_dir / "reports" / arm / f"{args.display_name}-{arm}.{args.run_id}-{arm}.json"
            if rf.is_file():
                mlflow_log_scores(args.run_id, args.display_name, arm, json.loads(rf.read_text()))
        log(f"backfilled {n} trials + scores to {mlflow_uri()}")
        return

    if args.score_only:
        instances = load_instances(instance_ids, BENCH_DIR / "data")
        score_arms(run_dir, instances, instance_ids, arms, args.display_name, args.run_id, args.parallel)
        log(f"done. results: {run_dir}")
        return

    if not args.model:
        ap.error("--model is required when running trials")
    args.provider_cfg = host_provider_config(args.provider)
    if args.node_bin_dir is None:
        from shutil import which

        args.node_bin_dir = str(Path(which("node")).resolve().parent)
    args.opencode_bin_dir = str(Path(args.opencode_bin_dir).resolve())

    run_dir.mkdir(parents=True, exist_ok=True)

    instances = load_instances(instance_ids, BENCH_DIR / "data")
    log(f"run {args.run_id}: {len(instances)} instances x arms {arms} model={args.display_name} "
        f"trim_at={args.trim_at} native_at={args.native_at} (both arms; 'none' is native-compaction-only)")

    client = docker.from_env()
    # Group jobs by repo (and keep an instance's arms adjacent) so parallel workers
    # hit images sharing base layers back-to-back, and --evict-images reclaims the
    # repo-unique layers as each repo drains instead of needing mid-run prunes.
    groups = group_for_chaining(instances, arms, args.chain_max) if args.chain else None
    jobs = None if args.chain else sorted(
        ((inst, arm) for arm in arms for inst in instances),
        key=lambda j: (j[0]["repo"], j[0]["instance_id"], j[1]))
    if args.chain:
        log("chains: " + "  ".join(f"{a}:{r}x{len(i)}" for a, r, i in groups))

    remaining = collections.Counter()
    for inst in instances:
        remaining[inst["image"]] = len(arms)
    lock = threading.Lock()

    def run_and_evict(inst, arm):
        try:
            return run_trial(client, inst, arm, args, run_dir)
        finally:
            ref = inst["image"]
            if args.evict_images:
                with lock:
                    remaining[ref] -= 1
                    last = remaining[ref] == 0
                if last:
                    # removes only layers not shared with other kept images
                    try:
                        client.images.remove(ref, force=True)
                        print(f"[evict] {ref}", flush=True)
                    except Exception:
                        pass

    if args.chain:
        # Image eviction is per-image-count and a chain needs its repo's images until the chain
        # ends, so chained runs leave images alone and rely on `docker image prune -a -f` after.
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = [pool.submit(run_chain, client, insts, arm, args, run_dir, repo)
                       for arm, repo, insts in groups]
            metas = [m for fut in as_completed(futures) for m in fut.result()]
    else:
        with ThreadPoolExecutor(max_workers=args.parallel) as pool:
            metas = list(pool.map(lambda j: run_and_evict(j[0], j[1]), jobs))

    if args.skip_batch_score:
        log("batch scoring skipped (per-trial scores are in each trial dir + mlflow)")
    else:
        score_arms(run_dir, instances, instance_ids, arms, args.display_name, args.run_id, args.parallel)

    cost_report(run_dir, args.model)
    log(f"done. results: {run_dir}")


if __name__ == "__main__":
    main()
