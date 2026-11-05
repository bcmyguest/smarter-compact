"""Harbor agent: OpenCode v2 standalone build, so the smarter-compact arm is real.

Harbor's built-in `opencode` agent installs `opencode-ai` from npm, whose latest
publish is the 1.x line (1.19.5 -- 2.x ships only as the standalone installer
build). On 1.x the `plugins` and `mcp.servers` config keys do not exist, so
opencode logs

    message="configuration compatibility diagnostic" path='["plugins"]' kind=unsupported

and the smart arm silently degrades to a plain baseline: no trimming, no recall
tools, and a trial that measures nothing (the 95k/195k smoke ran to 164k context
without a single trim, and left no state dir).

This subclass installs the same v2 build the SWE-bench runner uses
(`/opt/opencodebin/opencode` in that image), bind-mounted here at
/opt/opencode-dist/opencode, instead of pulling v1 from npm.
"""

import re

from harbor.agents.installed.node_install import nvm_node_install_snippet
from harbor.agents.installed.opencode import OpenCode
from harbor.environments.base import BaseEnvironment

BINARY = "/opt/opencode-dist/opencode"


class _ScOpenCodeBase(OpenCode):
    """Shared install; the two arms get distinct names so Harbor keeps their
    metrics in separate eval buckets (the eval key is agent name + model, so two
    arms sharing a name are silently pooled into one result)."""

    async def exec_as_agent(self, environment, command, *args, **kwargs):
        # Harbor targets the npm v1 CLI: `opencode --model=X run ... --thinking
        # --dangerously-skip-permissions -- <prompt>`. The v2 build parses flags
        # only after the subcommand and has no --dangerously-skip-permissions, so
        # it printed its usage and exited (0 steps, no agent log). Rewrite to the
        # v2 shape our SWE-bench runner uses
        # (`opencode run --standalone --auto --format json --model ... -`), minus
        # --print-logs: that writes server logs to stderr, and Harbor parses this
        # stream as JSON lines to build trajectory.json.
        if "opencode --model=" in command:
            command = command.replace("--dangerously-skip-permissions", "--auto")
            command = re.sub(
                r"opencode --model=(\S+) run", r"opencode run --model=\1 --standalone",
                command,
            )
        return await super().exec_as_agent(environment, command, *args, **kwargs)

    async def install(self, environment: BaseEnvironment) -> None:
        # Same system deps as the built-in agent: node runs the plugin's MCP
        # server, coreutils gives run() the stdbuf it pipes through.
        await self.ensure_system_dependencies(
            environment, ("curl", "bash", "coreutils", "nodejs", "npm")
        )
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "if ldd --version 2>&1 | grep -qi musl || "
                "[ -f /etc/alpine-release ]; then "
                "node --version && npm --version; "
                f"else {nvm_node_install_snippet()}; fi && "
                f"test -x {BINARY} && install -m 755 {BINARY} /usr/local/bin/opencode && "
                "command -v opencode && opencode --version"
            ),
        )


class ScOpencodeBaseline(_ScOpenCodeBase):
    """Control: opencode v2, native compaction only."""

    @staticmethod
    def name() -> str:
        return "sc-opencode-baseline"


class ScOpencodeSmarterCompact(_ScOpenCodeBase):
    """Treatment: same build, smarter-compact plugin + recall MCP from the mount."""

    @staticmethod
    def name() -> str:
        return "sc-opencode-smarter-compact"
