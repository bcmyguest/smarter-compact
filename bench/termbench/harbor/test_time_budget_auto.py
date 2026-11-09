"""Offline tests of CONTEXT_TIME_BUDGET_SECONDS=auto in sc_agent_claude: fake trial and task config, no container.

Run with Harbor's interpreter: ~/.local/share/uv/tools/harbor/bin/python -m unittest test_time_budget_auto -v
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sc_agent_claude as sc  # noqa: E402
from harbor.models.task.config import TaskConfig  # noqa: E402
from harbor.models.trial.config import AgentConfig, TrialConfig  # noqa: E402
from harbor.trial.trial import Trial  # noqa: E402

TASK_TOML = "[agent]\ntimeout_sec = 900.0\n"


class FakeTrial:
    """Harbor's own timeout resolution on a fake trial: a TrialConfig and a task.toml string."""

    _compute_agent_timeout_sec = Trial._compute_agent_timeout_sec
    _resolve_timeout_sec = Trial._resolve_timeout_sec

    def __init__(self, task_toml=TASK_TOML, agent_env=None, **config):
        agent = AgentConfig(import_path="sc_agent_claude:ScClaudeSmarterCompact", env=agent_env or {},
                            **{k: config.pop(k) for k in ("override_timeout_sec", "max_timeout_sec") if k in config})
        self.config = TrialConfig(task={"path": "/tasks/fake"}, agent=agent, **config)
        self.task = SimpleNamespace(config=TaskConfig.model_validate_toml(task_toml))
        self._agent_timeout_sec = self._compute_agent_timeout_sec()


def init(trial, env):
    """sc._init_agent on trial, Harbor's own init replaced by one that makes an ScClaudeSmarterCompact with env."""
    agent = object.__new__(sc.ScClaudeSmarterCompact)
    agent._extra_env = dict(env)

    def stock(self):
        self.agent = agent

    with mock.patch.object(sc, "_STOCK_INIT_AGENT", stock):
        sc._init_agent(trial)
    return agent._extra_env


class TimeBudgetAutoTests(unittest.TestCase):
    def test_patch_installed(self):
        self.assertIs(Trial._init_agent, sc._init_agent)
        self.assertIn(sc.TIME_BUDGET_KEY, sc.ARM_ENV_KEYS)

    def test_auto_is_the_task_timeout(self):
        env = init(FakeTrial(), {sc.TIME_BUDGET_KEY: "auto", "CONTEXT_LEDGER": "1"})
        self.assertEqual(env, {sc.TIME_BUDGET_KEY: "900", "CONTEXT_LEDGER": "1"})

    def test_override_and_multipliers(self):
        self.assertEqual(init(FakeTrial(override_timeout_sec=1200.0), {sc.TIME_BUDGET_KEY: "auto"})[sc.TIME_BUDGET_KEY],
                         "1200")
        self.assertEqual(init(FakeTrial(timeout_multiplier=2.0), {sc.TIME_BUDGET_KEY: "auto"})[sc.TIME_BUDGET_KEY],
                         "1800")
        # The agent multiplier wins over the global one.
        trial = FakeTrial(timeout_multiplier=2.0, agent_timeout_multiplier=1.5)
        self.assertEqual(init(trial, {sc.TIME_BUDGET_KEY: "AUTO"})[sc.TIME_BUDGET_KEY], "1350")
        trial = FakeTrial(override_timeout_sec=3000.0, max_timeout_sec=2400.0, agent_timeout_multiplier=0.5)
        self.assertEqual(init(trial, {sc.TIME_BUDGET_KEY: "auto"})[sc.TIME_BUDGET_KEY], "1200")

    def test_no_timeout_is_off(self):
        trial = FakeTrial(task_toml="[agent]\n")
        trial._agent_timeout_sec = None
        self.assertEqual(init(trial, {sc.TIME_BUDGET_KEY: "auto"})[sc.TIME_BUDGET_KEY], "0")

    def test_other_values_pass_through(self):
        env = {sc.TIME_BUDGET_KEY: "600"}
        self.assertEqual(init(FakeTrial(), env), env)
        self.assertEqual(init(FakeTrial(), {}), {})

    def test_other_agents_untouched(self):
        trial = FakeTrial()
        agent = SimpleNamespace(_extra_env={sc.TIME_BUDGET_KEY: "auto"})
        with mock.patch.object(sc, "_STOCK_INIT_AGENT", lambda self: setattr(self, "agent", agent)):
            sc._init_agent(trial)
        self.assertEqual(agent._extra_env, {sc.TIME_BUDGET_KEY: "auto"})


if __name__ == "__main__":
    unittest.main()
