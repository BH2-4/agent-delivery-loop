"""Focused regressions: explicit-identity propagation to agent-run reads and the
bounded transient retry for identified read-only gh invocations. No real secrets,
network calls, or model subprocesses are used."""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_delivery_loop import orchestrator, publish, review_cli, runner
from agent_delivery_loop.claude_worker import ClaudeConfig
from agent_delivery_loop.cli import _take_orchestrator_pat
from agent_delivery_loop.errors import AgentDeliveryError
from agent_delivery_loop.github_identity import GitHubPAT

# Intentionally not a usable credential.
FAKE_PAT = "github_pat_test_not_a_real_credential"


def _identity() -> GitHubPAT:
    return GitHubPAT(repository="o/r", expected_login="o", _token=FAKE_PAT)


def _completed(code: int, stderr: str = "", stdout: str = "{}") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], code, stdout, stderr)


class _FakeStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def assert_worker_available(self) -> None:
        return None

    def claim(self, _key: str):
        return contextlib.nullcontext()

    def write(self, *_args, **_kwargs) -> None:
        return None


class OrchestratorPatChannelTests(unittest.TestCase):
    def test_channel_env_carries_snapshot_value_only_to_the_child(self) -> None:
        identity = _identity()
        with tempfile.TemporaryDirectory() as temporary:
            stdout = Path(temporary) / "out.json"
            stdout.write_text("{}", encoding="utf-8")
            captured: dict[str, object] = {}

            def fake_popen(argv, **kwargs):  # noqa: ANN001
                captured["argv"] = argv
                captured["env"] = kwargs["env"]

                class FakeProcess:
                    def wait(self, timeout=None):  # noqa: ANN001 - subprocess API shape
                        kwargs["stdout"].write(b"{}")
                        return 0

                return FakeProcess()

            with patch("agent_delivery_loop.orchestrator.subprocess.Popen", side_effect=fake_popen):
                result = orchestrator._spawn_agent_run(
                    entry="agent-run", repo_root=Path(temporary),
                    plan_pr="https://github.com/o/r/pull/26",
                    work_order_path=".agents/work-orders/WO-X-001-r1.json",
                    model="m", base_url="https://api.example", effort="max",
                    auth_config=Path("/nowhere"), timeout_seconds=30, proxy=None,
                    stdout_path=stdout, identity=identity,
                )
            self.assertEqual(result, {"agent_run_exit_code": 0})
            env = captured["env"]
            self.assertEqual(env["AGENT_DELIVERY_PAT"], FAKE_PAT)
            self.assertEqual(env["AGENT_DELIVERY_PAT_LOGIN"], "o")
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn(FAKE_PAT, " ".join(captured["argv"]))
            # The orchestrator's own environment never carries the channel.
            self.assertNotIn("AGENT_DELIVERY_PAT", os.environ)

    def test_cli_consumes_channel_and_closes_it_fail_closed(self) -> None:
        with patch.dict(os.environ, {"AGENT_DELIVERY_PAT": FAKE_PAT, "AGENT_DELIVERY_PAT_LOGIN": "o"}):
            identity = _take_orchestrator_pat("o/r#26")
            self.assertEqual(identity.token_for("o/r"), FAKE_PAT)
            # The channel is closed inside the process before any model subprocess exists.
            self.assertNotIn("AGENT_DELIVERY_PAT", os.environ)
            self.assertNotIn("AGENT_DELIVERY_PAT_LOGIN", os.environ)
        # Without the channel the legacy entry is preserved.
        self.assertIsNone(_take_orchestrator_pat("o/r#26"))
        # A half-present or non-fine-grained value fails closed, never falls back.
        with patch.dict(os.environ, {"AGENT_DELIVERY_PAT": FAKE_PAT}):
            with self.assertRaises(AgentDeliveryError):
                _take_orchestrator_pat("o/r#26")
        with patch.dict(os.environ, {"AGENT_DELIVERY_PAT": "classic_secret", "AGENT_DELIVERY_PAT_LOGIN": "o"}):
            with self.assertRaises(AgentDeliveryError):
                _take_orchestrator_pat("o/r#26")

    def test_runner_passes_identity_token_into_the_real_read_client(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/r.git"], check=True,
            )
            config = ClaudeConfig(
                executable="claude", model="m", base_url="https://api.example",
                provider_host="api.example", auth_name="ANTHROPIC_AUTH_TOKEN",
                auth_value="x", effort="max", target_repo_root=repo.resolve(),
            )
            seen: dict[str, object] = {}

            class Sentinel(AgentDeliveryError):
                pass

            def fake_client(repo_arg, token=None):  # noqa: ANN001
                seen["repo"] = repo_arg.slug
                seen["token"] = token
                raise Sentinel("stop-before-worker")

            with patch("agent_delivery_loop.runner.GitHubClient", side_effect=fake_client), \
                 patch("agent_delivery_loop.runner.preflight", return_value=(0, 1, 0)), \
                 patch("agent_delivery_loop.runner.RunStore", return_value=_FakeStore(base / "state")):
                with self.assertRaises(Sentinel):
                    runner.execute_plan(
                        repo_path=repo,
                        plan_pr="o/r#26",
                        work_order_path=".agents/work-orders/WO-X-001-r1.json",
                        worker_config=config,
                        pat_identity=_identity(),
                    )
            self.assertEqual(seen["repo"], "o/r")
            self.assertEqual(seen["token"], FAKE_PAT)


class ModelSubprocessExclusionTests(unittest.TestCase):
    def test_claude_allowlist_never_carries_the_channel(self) -> None:
        with patch.dict(
            os.environ,
            {"AGENT_DELIVERY_PAT": FAKE_PAT, "AGENT_DELIVERY_PAT_LOGIN": "o", "GH_TOKEN": "ambient"},
        ):
            config = ClaudeConfig(
                executable="claude", model="m", base_url="https://api.example",
                provider_host="api.example", auth_name="ANTHROPIC_AUTH_TOKEN",
                auth_value="x", effort="max", target_repo_root=Path("/tmp"),
            )
            child = config.child_environment(Path("/tmp/isolated"), include_auth=True)
        self.assertNotIn("AGENT_DELIVERY_PAT", child)
        self.assertNotIn("AGENT_DELIVERY_PAT_LOGIN", child)
        self.assertNotIn("GH_TOKEN", child)
        self.assertEqual(child["ANTHROPIC_AUTH_TOKEN"], "x")

    def test_review_and_publish_scrub_lists_cover_the_channel(self) -> None:
        for names in (publish.CREDENTIAL_ENV_NAMES, review_cli.CREDENTIAL_ENV_NAMES):
            self.assertIn("AGENT_DELIVERY_PAT", names)
            self.assertIn("AGENT_DELIVERY_PAT_LOGIN", names)


class GhReadOnlyRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        sleeper = patch("agent_delivery_loop.publish.time.sleep", lambda _s: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_transient_failures_recover_within_three_attempts(self) -> None:
        identity = _identity()
        outcomes = [
            _completed(1, stderr="Get https://api.github.com/x: net/http: TLS handshake timeout"),
            _completed(1, stderr="read tcp: connection reset by peer"),
            _completed(0, stdout='{"ok": true}'),
        ]
        with patch("agent_delivery_loop.publish.run_gh", side_effect=outcomes) as raw:
            result = publish.gh_json(["api", "user"], identity=identity)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(raw.call_count, 3)

    def test_persistent_transient_failure_stops_bounded(self) -> None:
        identity = _identity()
        with patch(
            "agent_delivery_loop.publish.run_gh",
            return_value=_completed(1, stderr="net/http: TLS handshake timeout"),
        ) as raw:
            with self.assertRaises(AgentDeliveryError) as caught:
                publish.gh_json(["api", "user"], identity=identity)
        self.assertEqual(raw.call_count, 3)
        message = str(caught.exception)
        self.assertIn("exit code 1", message)
        self.assertIn("3 bounded attempt", message)
        # Sanitized errors never carry the raw stderr text.
        self.assertNotIn("TLS handshake timeout", message)

    def test_permission_and_rate_limit_failures_are_never_retried(self) -> None:
        identity = _identity()
        for stderr in (
            "gh: Resource not accessible by personal access token (HTTP 403)",
            "gh: API rate limit exceeded for installation (HTTP 403)",
        ):
            with patch(
                "agent_delivery_loop.publish.run_gh", return_value=_completed(1, stderr=stderr),
            ) as raw:
                with self.assertRaises(AgentDeliveryError):
                    publish.gh_json(["api", "user"], identity=identity)
            self.assertEqual(raw.call_count, 1)

    def test_write_shaped_invocations_stay_single_attempt(self) -> None:
        identity = _identity()
        for args in (
            ["api", "-X", "POST", "repos/o/r/pulls"],
            ["api", "repos/o/r/pulls", "-f", "title=x"],
            ["pr", "merge", "26", "--repo", "o/r", "--merge"],
        ):
            with patch(
                "agent_delivery_loop.publish.run_gh",
                return_value=_completed(1, stderr="net/http: TLS handshake timeout"),
            ) as raw:
                result = publish.run_gh_read(args, identity=identity)
            self.assertEqual(raw.call_count, 1)
            self.assertEqual(result.returncode, 1)

    def test_remote_branch_404_still_maps_to_none_without_retry(self) -> None:
        identity = _identity()
        with patch(
            "agent_delivery_loop.publish.run_gh",
            return_value=_completed(1, stderr="gh: Not Found (HTTP 404)"),
        ) as raw:
            self.assertIsNone(publish.remote_branch_sha("o/r", "agent/x", identity=identity))
        self.assertEqual(raw.call_count, 1)


if __name__ == "__main__":
    unittest.main()
