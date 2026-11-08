"""Offline tests of lhtb_continue: fake trial and environment, no container, no API call.

Run with Harbor's interpreter: ~/.local/share/uv/tools/harbor/bin/python -m unittest test_lhtb_continue -v
"""

import asyncio
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lhtb_continue as lc  # noqa: E402
from harbor.agents.installed.base import NonZeroAgentExitCodeError  # noqa: E402
from harbor.models.agent.context import AgentContext  # noqa: E402
from harbor.models.trial.paths import EnvironmentPaths  # noqa: E402
from harbor.models.verifier.result import VerifierResult  # noqa: E402
from harbor.trial.errors import AgentTimeoutError  # noqa: E402

BASE = "Play 2048."
TOML_ON = '[agent]\ncontinue_until_timeout = true\ntimeout_sec = 60.0\n'
TOML_OFF = '[agent]\ntimeout_sec = 60.0\n'


class FakeAgent:
    SC_CONTINUE_UNTIL_TIMEOUT = True
    extra_env: dict = {}

    def __init__(self, phase_sec=0.0):
        self.calls = []
        self.phase_sec = phase_sec

    async def run(self, instruction, environment, context):
        self.calls.append(("run", instruction))
        await asyncio.sleep(self.phase_sec)

    async def resume(self, instruction, environment, context):
        self.calls.append(("resume", instruction))
        await asyncio.sleep(self.phase_sec)

    def populate_context_post_run(self, context):
        context.n_input_tokens, context.n_output_tokens, context.cost_usd = 100, 7, 0.01


class FakeEnv:
    capabilities = SimpleNamespace(mounted=True)

    def __init__(self, events):
        self.events = events

    @contextlib.contextmanager
    def with_default_user(self, user):
        yield

    @contextlib.contextmanager
    def scoped_exec_env(self, env):
        yield

    async def exec(self, command, timeout_sec=None):
        if "tmux*" in command:
            self.events.append("STOP" if "kill -STOP" in command else "CONT")
        elif command == lc.KILL_CLAUDE_CMD:
            self.events.append("KILL")
        elif command.startswith("cp -f"):
            self.events.append("COPYLOG")
        return SimpleNamespace(return_code=0)

    async def reset_dirs(self, remove_dirs, create_dirs):
        self.events.append(("RESET", [str(d) for d in remove_dirs]))


class FakeTrial:
    def __init__(self, tmp, *, toml=TOML_ON, timeout=1.0, agent=None, rewards=None):
        self.events = []
        tmp = Path(tmp)
        (tmp / "task.toml").write_text(toml)
        self.task = SimpleNamespace(
            instruction=BASE,
            config=SimpleNamespace(agent=SimpleNamespace(user=None),
                                   verifier=SimpleNamespace(user=None, environment_mode=None, environment=None),
                                   environment=SimpleNamespace(os="linux")),
            paths=SimpleNamespace(config_path=tmp / "task.toml"))
        self.agent = agent or FakeAgent()
        self.user_agent = None
        self._load_trajectory = None
        self._agent_timeout_sec = timeout
        self._verifier_timeout_sec = 10
        self.agent_environment = FakeEnv(self.events)
        self.agent_env_paths = EnvironmentPaths.for_os("linux")
        self.paths = SimpleNamespace(agent_dir=tmp / "agent", verifier_dir=tmp / "verifier", trial_dir=tmp,
                                     artifacts_dir=tmp / "artifacts")
        self.paths.verifier_dir.mkdir(exist_ok=True)
        self.config = SimpleNamespace(environment=SimpleNamespace(type="docker"))
        self.result = SimpleNamespace(agent_result=None, agent_execution=None)
        self.logger = SimpleNamespace(warning=lambda *a, **k: None, info=lambda *a, **k: None)
        self.rewards = list(rewards or [])
        self.exceptions = []
        self.synced = 0

    async def _emit(self, event):
        self.events.append(str(event))

    @staticmethod
    def _now():
        return lc.datetime.now(lc.timezone.utc)

    def _network_plan(self, step_cfg=None, **kw):
        return SimpleNamespace(agent_env_baseline=None, agent_phase=None)

    @contextlib.asynccontextmanager
    async def _phase_network_policy(self, env, *, baseline_policy, phase_policy):
        yield

    async def _configure_web_tools(self, policy):
        pass

    @contextlib.contextmanager
    def _log_context(self, phase, env, step_name=None):
        yield

    async def _run_shared_verifier(self, *, timeout_sec, user):
        self.events.append("VERIFY")
        (self.paths.verifier_dir / "reward.txt").write_text("0.3")
        reward = self.rewards.pop(0) if self.rewards else 0.3
        return VerifierResult(rewards={"reward": reward})

    def _record_exception(self, exc):
        self.exceptions.append(exc)

    async def _sync_agent_output(self, target):
        self.synced += 1


@contextlib.asynccontextmanager
async def _no_sync(*a, **k):
    yield


lc.sync_trajectory = _no_sync


def run(trial):
    asyncio.run(lc._run_agent(trial))
    return trial


def rounds(trial):
    return [json.loads(line) for line in (trial.paths.agent_dir / lc.ROUND_LOG).read_text().splitlines()]


class ContinueUntilTimeout(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_agent_that_stops_at_once_is_reprompted_until_the_deadline(self):
        t = run(FakeTrial(self.tmp.name, timeout=0.5, agent=FakeAgent(phase_sec=0.05)))
        kinds = [k for k, _ in t.agent.calls]
        self.assertGreater(len(kinds), 2)
        self.assertEqual(kinds[0], "run")
        self.assertEqual(set(kinds[1:]), {"resume"})
        self.assertEqual(t.agent.calls[0][1], BASE)
        second = t.agent.calls[1][1]
        self.assertTrue(second.startswith(BASE + "\n\n---\nYour submitted solution did not pass verification."))
        self.assertIn("This is continuation 1; approximately ", second)
        self.assertNotIn("0.3", second)
        self.assertIn("This is continuation 2;", t.agent.calls[2][1])
        # The deadline cuts the last phase or falls between phases; upstream records AgentTimeoutError for the former.
        self.assertTrue(all(isinstance(e, AgentTimeoutError) for e in t.exceptions))
        meta = t.result.agent_result.metadata
        self.assertEqual(meta["verifier_feedback_mode"], "binary")
        self.assertEqual(meta["continue_until_timeout_phases"], len(kinds) - 1)
        self.assertEqual((t.result.agent_result.n_input_tokens, t.result.agent_result.cost_usd), (100, 0.01))

    def test_verifier_is_bracketed_by_freeze_scrub_thaw(self):
        t = run(FakeTrial(self.tmp.name, timeout=0.3, agent=FakeAgent(phase_sec=0.05)))
        i = t.events.index("VERIFY")
        self.assertEqual(t.events[i - 1], "STOP")
        self.assertEqual(t.events[i + 1][0], "RESET")
        self.assertIn("/tests", t.events[i + 1][1])
        self.assertEqual(t.events[i + 2], "CONT")
        self.assertEqual(list(t.paths.verifier_dir.iterdir()), [])
        self.assertTrue((t.paths.trial_dir / "interim_verifier_artifacts" / "phase-0001" / "reward.txt").exists())

    def test_full_pass_ends_the_loop(self):
        t = run(FakeTrial(self.tmp.name, timeout=30, rewards=[0.5, 1.0]))
        self.assertEqual([k for k, _ in t.agent.calls], ["run", "resume"])
        self.assertEqual([r["passed"] for r in rounds(t)], [False, True])
        self.assertEqual(t.result.agent_result.metadata["continue_until_timeout_phases"], 1)

    def test_checkpoint_cut_kills_claude_then_verifies_and_resumes(self):
        lc.CHECKPOINT_INTERVAL_SEC, saved = 0.1, lc.CHECKPOINT_INTERVAL_SEC
        self.addCleanup(setattr, lc, "CHECKPOINT_INTERVAL_SEC", saved)
        t = run(FakeTrial(self.tmp.name, timeout=30, agent=FakeAgent(phase_sec=5), rewards=[0.0, 1.0]))
        self.assertEqual([k for k, _ in t.agent.calls], ["run", "resume"])
        self.assertEqual(t.events.count("KILL"), 2)
        self.assertLess(t.events.index("KILL"), t.events.index("VERIFY"))
        self.assertEqual([r["end"] for r in rounds(t)], ["checkpoint", "checkpoint"])

    def test_deadline_mid_phase_records_agent_timeout(self):
        t = run(FakeTrial(self.tmp.name, timeout=0.2, agent=FakeAgent(phase_sec=5)))
        self.assertEqual(len(t.agent.calls), 1)
        self.assertIsInstance(t.exceptions[0], AgentTimeoutError)
        self.assertIn("KILL", t.events)
        self.assertEqual(rounds(t)[-1]["end"], "deadline")
        self.assertEqual(t.synced, 1)

    def test_agent_error_is_recorded_and_ends_the_loop(self):
        class Failing(FakeAgent):
            async def resume(self, instruction, environment, context):
                raise NonZeroAgentExitCodeError("boom")
        t = run(FakeTrial(self.tmp.name, timeout=30, agent=Failing()))
        self.assertIsInstance(t.exceptions[0], NonZeroAgentExitCodeError)
        self.assertEqual(t.synced, 1)

    def test_flag_off_or_foreign_agent_runs_stock_harbor(self):
        stock = []

        async def fake_stock(trial):
            stock.append(trial)
        lc._STOCK_RUN_AGENT, saved = fake_stock, lc._STOCK_RUN_AGENT
        self.addCleanup(setattr, lc, "_STOCK_RUN_AGENT", saved)

        class Foreign(FakeAgent):
            SC_CONTINUE_UNTIL_TIMEOUT = False
        off = run(FakeTrial(self.tmp.name, toml=TOML_OFF))
        other = FakeTrial(self.tmp.name, agent=Foreign())
        run(other)
        self.assertEqual(stock, [off, other])
        self.assertEqual(off.agent.calls, [])

    def test_diagnostic_mode_appends_feedback(self):
        text = lc.format_verifier_continuation(BASE, phase=2, remaining_sec=99, feedback_mode="diagnostic",
                                               feedback="Verifier reward: 0.3")
        self.assertIn("(phase 2)", text)
        self.assertTrue(text.endswith("Verifier feedback:\nVerifier reward: 0.3\n"))


class ClaudeCodeResumeCommand(unittest.TestCase):
    """Dry run: render the commands ScClaudeBaseline sends for phase 0 and a continuation."""

    def test_resume_adds_continue_and_keeps_the_flags(self):
        import sc_agent_claude

        sent = []

        class Env:
            default_user = None

            async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
                sent.append(command)
                return SimpleNamespace(return_code=0, stdout="", stderr="")

            async def upload_file(self, *a, **k):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            agent = sc_agent_claude.ScClaudeBaseline(logs_dir=Path(tmp), model_name="anthropic/claude-haiku-5-5")
            asyncio.run(agent.run(BASE, Env(), AgentContext()))
            first = [c for c in sent if "claude --verbose" in c][-1]
            asyncio.run(agent.resume(BASE + " again", Env(), AgentContext()))
            second = [c for c in sent if "claude --verbose" in c][-1]
        self.assertNotIn("--continue", first)
        self.assertEqual(second.split("claude --verbose", 1)[1], first.split("claude --verbose", 1)[1].replace(
            "--print", "--continue --print"))


if __name__ == "__main__":
    unittest.main()
