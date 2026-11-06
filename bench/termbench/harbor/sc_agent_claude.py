"""Harbor arms for Terminal-Bench with **Claude Code**: native compaction vs smarter-compact.

Why this file is much smaller than the OpenCode one: Claude Code needs no custom config, model
provider or MCP wiring from us. The only difference between the arms is whether the session
runs through smarter-compact's context proxy.

Per the repo README's support tiers, Claude Code gets *full trim* only through the loopback
LiteLLM proxy (OpenCode 2 and Pi trim in-process). So the smart arm, inside the agent
container, installs this repo from the `/opt/smarter-compact` bind mount and runs
`smarter-compact enable claude`, which repoints `ANTHROPIC_BASE_URL` at `http://127.0.0.1:4101`,
starts the proxy, and registers the search tools / compact command via the Claude plugin
marketplace. The baseline leaves `ANTHROPIC_BASE_URL` alone (api.anthropic.com) and gets Claude
Code's own autocompaction.

Two things this file deliberately does **not** fake:

* It does not set a native compaction threshold in code. Jobs set Claude Code's own
  `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (100000 to 1000000) and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE`
  (lowers the trigger to that percentage of the window) per agent; see
  https://code.claude.com/docs/en/env-vars and https://code.claude.com/docs/en/model-config.
* It does not carry an API key. Harbor reads `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` from
  the launching environment; nothing here should hardcode one.

Credentials note (measured): `ANTHROPIC_API_KEY` is **not** set anywhere on this host; the repo
`.env` has `CLAUDE_API_KEY`, which `bench/swebench/run_bench.py` describes as the *Claude
subscription* token. That is the explanation for "no Haiku usage on my API token" -- runs
authenticated as the subscription. A run here bills to whichever token is exported at launch.
"""

import os
import shlex
from pathlib import Path

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.agents.installed.node_install import nvm_node_install_snippet

REPO = "/opt/smarter-compact"
STATE_DIR = "/logs/agent/smarter-compact"
# SC_TRIM_AT on the harbor host sets the default cut; a job agent's env CONTEXT_TRIM_AT_TOKENS overrides it.
TRIM_AT = os.environ.get("SC_TRIM_AT", "95000")
# Proxy experiment knobs a job's per-agent "env" may set; Harbor applies that env only while the agent runs,
# after install has already started the proxy, so install forwards these itself.
ARM_ENV_KEYS = ("CONTEXT_TRIM_AT_TOKENS", "CONTEXT_TRIM_TO_FRACTION", "CONTEXT_STRIP_PROSE", "CONTEXT_PROGRESS_NOTE",
                "CONTEXT_MASK_OBSERVATIONS", "CONTEXT_STANDING_INSTRUCTION", "CONTEXT_POINTER_TOOLS",
                "CONTEXT_SERVER_CLEAR", "CONTEXT_SERVER_CLEAR_TRIGGER_TOKENS", "CONTEXT_SERVER_CLEAR_KEEP_TOOL_USES",
                "CONTEXT_SERVER_CLEAR_AT_LEAST_TOKENS", "CONTEXT_CLEAR_KEEP_RESULTS")
# A trim threshold no Terminal-Bench session reaches: the proxy runs, our trim never fires.
TRIM_OFF = "10000000"
# Job-level bind mount of the host ~/.claude/.credentials.json (read-only in practice; the
# trial container never refreshes it).
CREDS_MOUNT = "/opt/claude-creds/.credentials.json"
# The recall server needs node:sqlite (package.json engines: node >= 24); task images ship older Node.
NODE_MAJOR = 24
# Prints a Node >= NODE_MAJOR binary path, installing one via nvm when the image's node is older.
_NODE_RESOLVE = (
    f'if node -e "process.exit(+process.versions.node.split(\'.\')[0]>={NODE_MAJOR}?0:1)" 2>/dev/null; '
    'then NODE_BIN=$(command -v node); else '
    f"{nvm_node_install_snippet(NODE_MAJOR)} >>/logs/agent/sc-install.log 2>&1 && "
    f'NODE_BIN=$(nvm which {NODE_MAJOR}); fi; '
    '[ -x "$NODE_BIN" ] || { echo "FATAL: no node >= ' + str(NODE_MAJOR) + '"; '
    "tail -20 /logs/agent/sc-install.log; exit 1; }; "
    'export PATH="$(dirname "$NODE_BIN"):$PATH"; echo "node at: $NODE_BIN ($(node --version))"; '
)
# Merges the recall MCP server into <CLAUDE_CONFIG_DIR>/.claude.json, pinned to the trial's store
# and to the Node binary passed as argv[2].
_MCP_REGISTER = f"""
import json, os, sys
p = sys.argv[1]
d = json.load(open(p)) if os.path.exists(p) else {{}}
d.setdefault("mcpServers", {{}})["smarter-compact"] = {{
    "type": "stdio",
    "command": sys.argv[2],
    "args": ["{REPO}/plugin/lib/mcp/server.mjs"],
    "env": {{"SMARTER_COMPACT_STATE_DIR": "{STATE_DIR}", "CONTEXT_COMPACTION_RECALL": "0"}},
}}
json.dump(d, open(p, "w"), indent=2)
print("mcp-registered=ok")
"""
# Restarts the proxy when it stops listening: a task's own build can OOM-kill it (compile-compcert
# lost 30 of 40 minutes to ECONNREFUSED), and containers cannot lower its oom_score_adj.
_WATCHDOG = (
    "up() { bash -c 'exec 3<>/dev/tcp/127.0.0.1/4101' 2>/dev/null; }; "
    "while sleep 5; do up && continue; "
    'echo "$(date -u +%FT%TZ) proxy down, restarting"; '
    f"{REPO}/plugin/scripts/smarter-compact enable claude; "
    "for i in $(seq 60); do up && break; sleep 2; done; "
    'up && echo "$(date -u +%FT%TZ) proxy back" || echo "$(date -u +%FT%TZ) proxy still down"; done'
)


class ScClaudeBase(ClaudeCode):
    """Shared: seed Claude Code's OAuth credentials into the config dir Harbor will use.

    Harbor sets `CLAUDE_CONFIG_DIR=<logs>/sessions` unconditionally at run time, so credentials
    have to be sitting in that exact directory before the agent runs -- an env override will not
    win. This is only needed for the **subscription** credential path; with a real
    `ANTHROPIC_API_KEY` this is a no-op (nothing is mounted, so it silently skips).
    """

    def __init__(self, *args, extra_env=None, **kwargs):
        # The subscription login fetches the account's claude.ai connectors: hundreds of KB of deferred tools per request.
        extra_env = {"ENABLE_CLAUDEAI_MCP_SERVERS": "false", **(extra_env or {})}
        super().__init__(*args, extra_env=extra_env, **kwargs)

    async def install(self, environment):
        await super().install(environment)
        dest = (self.environment_logs_dir / "sessions").as_posix()
        await self.exec_as_agent(
            environment,
            command=(
                f"mkdir -p {dest} && "
                f"if [ -f {CREDS_MOUNT} ]; then cp {CREDS_MOUNT} {dest}/.credentials.json && "
                f"chmod 600 {dest}/.credentials.json; fi; true"
            ),
        )


class ScClaudeBaseline(ScClaudeBase):
    """Claude Code with its own autocompaction. Distinct class name so Harbor's eval key
    (agent name + model) keeps the two arms in separate buckets."""


class ScClaudeSmarterCompact(ScClaudeBase):
    """Same agent, plus smarter-compact's proxy trim at $TRIM_AT and its MCP search tools."""

    # False leaves the recall MCP server unregistered, so Claude Code offers no smart_compact_* tool.
    RECALL_TOOLS = True
    # Env names the mounted proxy's settings.py must read; install fails rather than run an arm whose knob is a no-op.
    REQUIRES_PROXY_SETTINGS = ()
    # Proxy env that defines the arm; applied after the job's env so a job cannot undo it.
    FORCED_ENV = {}
    # Proxy env the arm defaults to; the job's env overrides it. Turn drops only: masking is the proxy default.
    DEFAULT_ENV = {"CONTEXT_MASK_OBSERVATIONS": "0"}

    async def install(self, environment):
        await super().install(environment)
        # The dir Harbor will point CLAUDE_CONFIG_DIR at.
        dest = (self.environment_logs_dir / "sessions").as_posix()
        requires = "".join(
            f"grep -q {name} {REPO}/proxy/smarter_compact/settings.py || "
            f"{{ echo 'FATAL: mounted proxy lacks {name}'; exit 1; }}; "
            for name in self.REQUIRES_PROXY_SETTINGS)
        recall = (
            # The plugin's MCP server loads from $HOME/.claude's marketplace, invisible under
            # CLAUDE_CONFIG_DIR; register it user-scoped there (Harbor writes this file only
            # when its own mcp_servers option is set).
            f'python3 -c {shlex.quote(_MCP_REGISTER)} {dest}/.claude.json "$NODE_BIN" || '
            "{ echo 'FATAL: recall MCP not registered'; exit 1; }; "
        ) if self.RECALL_TOOLS else (
            f"! grep -qs smarter-compact {dest}/.claude.json || "
            "{ echo 'FATAL: recall MCP registered in a no-recall arm'; exit 1; }; echo recall-tools=off; "
        )

        # Harbor's ClaudeCode.install() puts the CLI in the *default user's* ~/.local/bin (curl
        # bootstrap) or npm's global prefix, and only appends that dir to ~/.bashrc -- which a
        # non-interactive exec never sources. Overriding HOME here pointed it at the wrong home
        # and the arm silently ran unarmed, so: never touch HOME, and locate the binary by
        # searching the real candidates. Still fails loudly rather than arming nothing.
        await self.exec_as_agent(
            environment,
            command=(
                requires +
                'found=""; for d in "$HOME/.local/bin" /root/.local/bin /home/*/.local/bin '
                '"$(npm prefix -g 2>/dev/null)/bin" /usr/local/bin /usr/bin; do '
                '[ -n "$d" ] && [ -x "$d/claude" ] && found="$d" && break; done; '
                '[ -n "$found" ] || { echo "FATAL: claude binary not found"; '
                'ls -la /root/.local/bin /home/*/.local/bin /usr/local/bin 2>/dev/null | head -30; exit 1; }; '
                'export PATH="$found:$HOME/.local/bin:$PATH"; echo "claude at: $found"; claude --version; '
                # The trim proxy *is* litellm (plugin/scripts/smarter-compact.mjs spawns `litellm
                # --config proxy/config.yaml`), so the arm needs it importable and on PATH.
                "command -v litellm >/dev/null 2>&1 || python3 -m pip install --quiet 'litellm[proxy]' "
                ">>/logs/agent/sc-install.log 2>&1 || python3 -m pip install --quiet --user "
                "--break-system-packages 'litellm[proxy]' >>/logs/agent/sc-install.log 2>&1; "
                # Some task images ship python3 without pip; uv brings its own Python.
                "command -v litellm >/dev/null 2>&1 || { curl -LsSf https://astral.sh/uv/install.sh "
                "| sh && \"$HOME/.local/bin/uv\" tool install --quiet 'litellm[proxy]'; } "
                ">>/logs/agent/sc-install.log 2>&1; "
                'command -v litellm >/dev/null 2>&1 || { echo "FATAL: litellm not on PATH after install"; '
                "tail -20 /logs/agent/sc-install.log; exit 1; }; "
                + _NODE_RESOLVE +
                # Keep the proxy's log with the trial; it otherwise dies with the container.
                "mkdir -p ~/.config/smarter-compact && ln -sf /logs/agent/proxy.log "
                "~/.config/smarter-compact/proxy.log; "
                f"cd {REPO} && (npm ci --omit=dev || npm install --omit=dev) "
                ">>/logs/agent/sc-install.log 2>&1 && "
                "./plugin/scripts/smarter-compact enable claude "
                ">>/logs/agent/sc-install.log 2>&1; "
                # `enable` writes $HOME/.claude/settings.json, but Harbor runs Claude Code with
                # CLAUDE_CONFIG_DIR=<logs>/sessions, which wins. Harbor writes no settings.json of
                # its own there, so copying the file keeps the base URL and trim key intact.
                "cp ~/.claude/settings.json " + dest + "/settings.json && grep -q 4101 " + dest +
                "/settings.json || { echo 'FATAL: proxy not wired into CLAUDE_CONFIG_DIR'; "
                "tail -20 /logs/agent/sc-install.log; exit 1; }; "
                "echo proxy-enabled-check=ok; "
                + recall +
                # `enable` gives up after 12s while litellm often needs ~20s, so wait here instead.
                "for i in $(seq 60); do bash -c 'exec 3<>/dev/tcp/127.0.0.1/4101' 2>/dev/null && break; "
                "sleep 2; done; bash -c 'exec 3<>/dev/tcp/127.0.0.1/4101' 2>/dev/null || "
                "{ echo 'FATAL: proxy not listening after 120s'; tail -20 /logs/agent/proxy.log; exit 1; }; "
                "echo proxy-live=yes; "
                f"setsid nohup bash -c {shlex.quote(_WATCHDOG)} >>/logs/agent/proxy-watchdog.log 2>&1 "
                "</dev/null & echo watchdog-started=ok"
            ),
            env={
                "CONTEXT_TRIM_AT_TOKENS": TRIM_AT,
                "SMARTER_COMPACT_STATE_DIR": STATE_DIR,
                "SMARTER_COMPACT_AUTO_PROXY": "1",
                **self.DEFAULT_ENV,
                **{k: v for k, v in self.extra_env.items() if k in ARM_ENV_KEYS},
                **self.FORCED_ENV,
            },
        )


# Experiment arms: distinct class names keep Harbor's eval buckets apart; the job's per-agent env sets the knobs.
class ScClaudeTrim50(ScClaudeSmarterCompact):
    """Trim to about 50K (CONTEXT_TRIM_TO_FRACTION)."""


class ScClaudeProse(ScClaudeSmarterCompact):
    """Strip tool calls and results from older turns (CONTEXT_STRIP_PROSE)."""


class ScClaudeProgress(ScClaudeSmarterCompact):
    """Mechanical progress note at each trim (CONTEXT_PROGRESS_NOTE)."""


class ScClaudeCombined(ScClaudeSmarterCompact):
    """Trim to about 50K, prose strip and progress note together."""


class ScClaudeMask(ScClaudeSmarterCompact):
    """Keep older tool calls, mask their results (CONTEXT_MASK_OBSERVATIONS=1, the proxy default; wins over prose strip)."""

    DEFAULT_ENV = {"CONTEXT_MASK_OBSERVATIONS": "1"}


class ScClaudeClear(ScClaudeSmarterCompact):
    """Tool-result clearing: each trim masks every result but the newest N (CONTEXT_CLEAR_KEEP_RESULTS, default 10),
    keeping every turn and call; turns drop only when that is not enough."""

    REQUIRES_PROXY_SETTINGS = ("CONTEXT_CLEAR_KEEP_RESULTS",)
    DEFAULT_ENV = {"CONTEXT_CLEAR_KEEP_RESULTS": "10", "CONTEXT_MASK_OBSERVATIONS": "0"}


class ScClaudeNoStrip(ScClaudeSmarterCompact):
    """Prose strip off (CONTEXT_STRIP_PROSE=0, the default since 26d2040): the control for prose and mask."""


class ScClaudeNoRecall(ScClaudeSmarterCompact):
    """Trim without the search tools: separates the archive's effect from a shorter prompt.

    The trim pointer names smart_compact_search/_sql even when no request offers them, so this arm needs the proxy
    to drop those directions (proposed CONTEXT_POINTER_TOOLS=0, not implemented yet); install fails until it exists.
    """

    RECALL_TOOLS = False
    REQUIRES_PROXY_SETTINGS = ("CONTEXT_POINTER_TOOLS",)
    FORCED_ENV = {"CONTEXT_POINTER_TOOLS": "0", "CONTEXT_STANDING_INSTRUCTION": "0"}


class ScClaudeServerClear(ScClaudeSmarterCompact):
    """Anthropic context editing (clear_tool_uses_20250919) added by the proxy, our trim off (TRIM_OFF), no recall tools.

    Needs CONTEXT_SERVER_CLEAR from branch sc-core in the mounted repo; install fails until it is there.
    """

    RECALL_TOOLS = False
    REQUIRES_PROXY_SETTINGS = ("CONTEXT_SERVER_CLEAR", "CONTEXT_SERVER_CLEAR_TRIGGER_TOKENS")
    FORCED_ENV = {"CONTEXT_SERVER_CLEAR": "1", "CONTEXT_TRIM_AT_TOKENS": TRIM_OFF, "CONTEXT_STANDING_INSTRUCTION": "0"}


class ScClaudeNativeLow(ScClaudeBaseline):
    """Claude Code's summarizing compaction at a lowered trigger (CLAUDE_CODE_AUTO_COMPACT_WINDOW x
    CLAUDE_AUTOCOMPACT_PCT_OVERRIDE in the job env), no proxy."""
