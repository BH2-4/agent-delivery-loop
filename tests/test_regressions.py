from __future__ import annotations

import json
import os
import signal
import sys
import time
import fcntl
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import ExitStack, contextmanager, redirect_stderr
from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from agent_delivery_loop.claude_worker import (
    AUTH_ENV,
    ClaudeConfig,
    WorkerCancelled,
    WorkerLifecycle,
    WorkerOutcome,
    WorkerResultError,
    _check_effort_support,
    _process_group_exists,
    _stop_process_group,
    _validated_outcome,
    run_claude,
)
from agent_delivery_loop.cli import _report_error, agent_run_main, agent_watch_main
from agent_delivery_loop.errors import AgentDeliveryError
from agent_delivery_loop.github import GitHubClient, Repo
from agent_delivery_loop.git_ops import DeliveryCommit, StagedSnapshot, commit_changes, git, stage_changes
from agent_delivery_loop.runner import _record_cancelled_run, _safe_changed_paths, execute_plan
from agent_delivery_loop.review_cli import ReviewGateError, assert_review_processes_settled, run_review_process
from agent_delivery_loop.orchestrator import _wait_for_ci, verify_installation
from agent_delivery_loop.store import RunStateError, RunStore, task_key
from agent_delivery_loop.work_order import parse_work_order


FAKE_CODEX_SCRIPT = """#!{python}
import json, sys, time
args = sys.argv[1:]
if "--version" in args:
    print("FakeCodex 1.0")
    sys.exit(0)
if "--sleep-forever" in args:
    time.sleep(120)
    sys.exit(0)
out = args[args.index("--output-last-message") + 1]
with open(out, "w") as stream:
    json.dump({{"verdict": "pass"}}, stream)
sys.exit(int(args[args.index("--exit-with") + 1]) if "--exit-with" in args else 0)
"""


class ReviewCliProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        base = Path(self._temporary.name)
        self.state = base / "state"
        self.bundle = base / "bundle"
        self.bundle.mkdir()
        for name in ("context.md", "review.schema.json", "prompt.txt"):
            (self.bundle / name).write_text(f"{name} content\n", encoding="utf-8")
        (self.bundle / "request.json").write_text(
            json.dumps({"base_sha": "0" * 40, "head_sha": "1" * 40, "context_sha256": "2" * 64}), encoding="utf-8"
        )
        self.codex = base / "fake-codex"
        self.codex.write_text(FAKE_CODEX_SCRIPT.format(python=sys.executable), encoding="utf-8")
        self.codex.chmod(0o700)
        self.patcher = patch.dict(os.environ, {"AGENT_STATE_DIR": str(self.state)})
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_exit_code_is_captured_and_stop_confirmed(self) -> None:
        result = run_review_process(
            bundle_dir=self.bundle, timeout_seconds=60, review_model="fake-model",
            review_effort="high", codex_binary=str(self.codex),
        )
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["stop_status"], "confirmed_stopped")
        self.assertEqual(result["result"], {"verdict": "pass"})
        assert_review_processes_settled()  # gate stays open after a confirmed stop

    def test_nonzero_exit_is_recorded_and_never_accepted(self) -> None:
        failing = self.codex.with_name("failing-codex")
        failing.write_text(
            FAKE_CODEX_SCRIPT.format(python=sys.executable).replace(
                'sys.exit(int(args[args.index("--exit-with") + 1]) if "--exit-with" in args else 0)',
                'sys.exit(3)',
            ),
            encoding="utf-8",
        )
        failing.chmod(0o700)
        with self.assertRaises(AgentDeliveryError) as raised:
            run_review_process(
                bundle_dir=self.bundle, timeout_seconds=60, review_model="m",
                review_effort="high", codex_binary=str(failing),
            )
        self.assertIn("exited with code 3", str(raised.exception))
        assert_review_processes_settled()

    def test_timeout_kills_process_group_and_confirms_stop(self) -> None:
        sleeping = self.codex.with_name("sleeping-codex")
        sleeping.write_text(
            "#!" + sys.executable + "\nimport sys, time\n"
            "if '--version' in sys.argv[1:]:\n    print('FakeCodex 1.0')\n    sys.exit(0)\n"
            "time.sleep(600)\n",
            encoding="utf-8",
        )
        sleeping.chmod(0o700)
        with patch("agent_delivery_loop.review_cli.MIN_REVIEW_TIMEOUT_SECONDS", 1):
            with self.assertRaises(AgentDeliveryError) as raised:
                run_review_process(
                    bundle_dir=self.bundle, timeout_seconds=1, review_model="m",
                    review_effort="high", codex_binary=str(sleeping),
                )
        self.assertIn("time limit", str(raised.exception))
        assert_review_processes_settled()

    def test_exit_zero_without_result_blocks_review_but_not_the_gate(self) -> None:
        silent = self.codex.with_name("silent-codex")
        silent.write_text(
            "#!" + sys.executable + "\nimport sys\n"
            "if '--version' in sys.argv[1:]:\n    print('FakeCodex 1.0')\n    sys.exit(0)\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        silent.chmod(0o700)
        with self.assertRaises(AgentDeliveryError) as raised:
            run_review_process(
                bundle_dir=self.bundle, timeout_seconds=60, review_model="m",
                review_effort="high", codex_binary=str(silent),
            )
        self.assertIn("review input", str(raised.exception).lower())
        # The stop was confirmed before the result was read, so later reviews stay allowed.
        assert_review_processes_settled()

    def test_unconfirmed_stop_blocks_later_reviews(self) -> None:
        reviews = self.state / "reviews"
        reviews.mkdir(parents=True)
        (reviews / "00000000-0000-0000-0000-000000000000.json").write_text(
            json.dumps({
                "review_id": "00000000-0000-0000-0000-000000000000",
                "stop_status": "stop_unconfirmed",
            }),
            encoding="utf-8",
        )
        with self.assertRaises(ReviewGateError):
            assert_review_processes_settled()


class InstallVerificationTests(unittest.TestCase):
    def _environment(self, base: Path) -> tuple[Path, Path, str, str]:
        import hashlib
        venv_bin = base / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        for name in ("agent-run", "agent-delivery"):
            entry = venv_bin / name
            entry.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            entry.chmod(0o700)
        fake_python = venv_bin / "python"
        fake_python.write_text(
            "#!/bin/sh\necho '" + str(base / "venv" / "lib" / "python3" / "site-packages" / "agent_delivery_loop" / "__init__.py") + "'\n",
            encoding="utf-8",
        )
        fake_python.chmod(0o700)
        wheel = base / "approved.whl"
        wheel.write_bytes(b"wheel-bytes")
        receipt = base / "receipt.json"
        receipt.write_text(json.dumps({
            "schema_version": 1,
            "source_sha": "a" * 40,
            "wheel_sha256": hashlib.sha256(b"wheel-bytes").hexdigest(),
            "venv_bin": str(venv_bin),
            "wheel_path": str(wheel),
        }), encoding="utf-8")
        return receipt, venv_bin, "a" * 40, hashlib.sha256(b"wheel-bytes").hexdigest()

    def test_matching_receipt_with_site_packages_origin_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            receipt, _, source_sha, wheel_sha = self._environment(Path(temporary))
            inside = Path(temporary) / "venv" / "lib" / "python3" / "site-packages" / "agent_delivery_loop" / "orchestrator.py"
            with patch("agent_delivery_loop.orchestrator.__file__", str(inside)):
                verified = verify_installation(
                    receipt_path=receipt, expected_source_sha=source_sha, expected_wheel_sha256=wheel_sha
                )
            self.assertTrue(verified["agent_run_entry"].endswith("agent-run"))

    def test_mismatched_provenance_and_editable_origin_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            receipt, _, source_sha, wheel_sha = self._environment(Path(temporary))
            with self.assertRaises(AgentDeliveryError):
                verify_installation(
                    receipt_path=receipt, expected_source_sha="b" * 40, expected_wheel_sha256=wheel_sha
                )
            base = Path(temporary)
            editable = base / "venv" / "bin" / "python"
            editable.write_text("#!/bin/sh\necho '" + str(base / "src" / "agent_delivery_loop" / "__init__.py") + "'\n", encoding="utf-8")
            with self.assertRaises(AgentDeliveryError):
                verify_installation(
                    receipt_path=receipt, expected_source_sha=source_sha, expected_wheel_sha256=wheel_sha
                )
            outside = base / "checkout" / "agent_delivery_loop" / "orchestrator.py"
            with patch("agent_delivery_loop.orchestrator.__file__", str(outside)):
                with self.assertRaises(AgentDeliveryError):
                    verify_installation(
                        receipt_path=receipt, expected_source_sha=source_sha, expected_wheel_sha256=wheel_sha
                    )


class CiWaitTests(unittest.TestCase):
    def test_only_full_uppercase_success_with_required_check_passes(self) -> None:
        head = "c" * 40
        view = {"headRefOid": head}
        with patch("agent_delivery_loop.orchestrator._gh_json", side_effect=[
            [{"name": "validate", "state": "SUCCESS", "link": "https://example.invalid/run/1"}], view,
        ]):
            outcome = _wait_for_ci(
                repo_slug="o/r", pr_number=1, head_sha=head, timeout_seconds=5, proxy=None
            )
        self.assertEqual(outcome["checks"], [("validate", "SUCCESS")])

    def test_missing_required_check_or_failure_state_stops(self) -> None:
        with patch("agent_delivery_loop.orchestrator._gh_json", return_value=[
            {"name": "other", "state": "SUCCESS"},
        ]):
            with self.assertRaises(AgentDeliveryError):
                _wait_for_ci(repo_slug="o/r", pr_number=1, head_sha="c" * 40, timeout_seconds=5, proxy=None)
        with patch("agent_delivery_loop.orchestrator._gh_json", return_value=[
            {"name": "validate", "state": "FAILURE"},
        ]):
            with self.assertRaises(AgentDeliveryError):
                _wait_for_ci(repo_slug="o/r", pr_number=1, head_sha="c" * 40, timeout_seconds=5, proxy=None)

    def test_empty_check_list_never_passes_as_success(self) -> None:
        with patch("agent_delivery_loop.orchestrator.CI_POLL_SECONDS", 0):
            with patch("agent_delivery_loop.orchestrator._gh_json", return_value=[]):
                with self.assertRaises(AgentDeliveryError):
                    _wait_for_ci(repo_slug="o/r", pr_number=1, head_sha="c" * 40, timeout_seconds=1, proxy=None)


class ExplicitClaudeConfigurationTests(unittest.TestCase):
    def test_run_and_watch_require_the_same_explicit_worker_options(self) -> None:
        calls = (
            (agent_run_main, ["--plan-pr", "owner/repo#1", "--work-order-path", "example.json"]),
            (agent_watch_main, ["--once"]),
        )
        for command, command_args in calls:
            with self.subTest(command=command.__name__):
                stderr = StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    command(
                        command_args
                        + [
                            "--model", "glm-5.3",
                            "--base-url", "https://open.bigmodel.cn/api/anthropic",
                            "--effort", "max",
                        ]
                    )
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("--auth-config", stderr.getvalue())
                self.assertNotIn("/private/", stderr.getvalue())

    def test_explicit_values_and_child_environment_use_one_frozen_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repository"
            repo.mkdir()
            settings_path = base / "claude-settings.json"
            selected_secret = "fake-selected-credential"
            settings_path.write_text(
                json.dumps(
                    {
                        "env": {
                            "ANTHROPIC_API_KEY": selected_secret,
                            "ANTHROPIC_BASE_URL": "HTTPS://OPEN.BIGMODEL.CN:443/api/anthropic/",
                            "UNRELATED": "not-forwarded",
                        }
                    }
                ),
                encoding="utf-8",
            )
            parent_environment = {
                "PATH": "/snapshot/bin",
                "HTTPS_PROXY": "https://proxy.invalid/initial",
                "SSL_CERT_FILE": "/snapshot/cert.pem",
                "ANTHROPIC_AUTH_TOKEN": "ambient-old-token",
                "ANTHROPIC_API_KEY": "ambient-other-token",
                "CLAUDE_CODE_OAUTH_TOKEN": "ambient-oauth-token",
                "CLAUDE_CODE_EFFORT_LEVEL": "low",
                "ANTHROPIC_MODEL": "ambient-model",
                "ANTHROPIC_BASE_URL": "https://ambient.invalid",
                "REASONING_MODEL": "ambient-reasoning-model",
            }
            with patch.dict(os.environ, parent_environment, clear=True), patch(
                "agent_delivery_loop.claude_worker.shutil.which", return_value="/fake/claude"
            ):
                config = ClaudeConfig.from_explicit(
                    model="glm-5.3",
                    base_url="https://open.bigmodel.cn/api/anthropic",
                    effort="max",
                    auth_config=settings_path,
                    repo_root=repo,
                )
                os.environ["HTTPS_PROXY"] = "https://proxy.invalid/changed"
                os.environ["ANTHROPIC_AUTH_TOKEN"] = "ambient-new-token"
                os.environ["ANTHROPIC_BASE_URL"] = "https://another.invalid/api"
                child_environment = config.child_environment(base / "worker-home")
                self.assertNotEqual(child_environment["ANTHROPIC_BASE_URL"], os.environ["ANTHROPIC_BASE_URL"])

            self.assertEqual(config.model, "glm-5.3")
            self.assertEqual(config.base_url, "https://open.bigmodel.cn/api/anthropic")
            self.assertEqual(config.effort, "max")
            self.assertEqual(config.auth_name, "ANTHROPIC_API_KEY")
            self.assertEqual(child_environment["ANTHROPIC_API_KEY"], selected_secret)
            self.assertEqual(child_environment["ANTHROPIC_MODEL"], "glm-5.3")
            self.assertEqual(child_environment["ANTHROPIC_BASE_URL"], "https://open.bigmodel.cn/api/anthropic")
            self.assertEqual(child_environment["HTTPS_PROXY"], "https://proxy.invalid/initial")
            self.assertEqual(child_environment["SSL_CERT_FILE"], "/snapshot/cert.pem")
            self.assertEqual(child_environment["PATH"], "/snapshot/bin")
            self.assertEqual(
                [name for name in AUTH_ENV if name in child_environment],
                ["ANTHROPIC_API_KEY"],
            )
            self.assertNotIn("UNRELATED", child_environment)
            self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", child_environment)
            self.assertNotIn("REASONING_MODEL", child_environment)
            self.assertNotIn(str(settings_path), repr(config))
            self.assertNotIn(selected_secret, repr(config))
            self.assertNotIn("snapshot/bin", repr(config))

    def test_settings_fail_closed_for_bad_json_duplicates_missing_or_multiple_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repository"
            repo.mkdir()
            settings_path = base / "settings.json"
            invalid_settings = (
                "not-json",
                '{"env":{"ANTHROPIC_API_KEY":"first","ANTHROPIC_API_KEY":"second"}}',
                json.dumps({"env": {"ANTHROPIC_API_KEY": "fake-one", "ANTHROPIC_AUTH_TOKEN": "fake-two"}}),
                json.dumps({"env": {"UNRELATED": "fake-token"}}),
                json.dumps({"env": {"ANTHROPIC_API_KEY": 22}}),
                json.dumps({"env": {"ANTHROPIC_API_KEY": "  "}}),
            )
            with patch("agent_delivery_loop.claude_worker.shutil.which", return_value="/fake/claude"):
                for raw_settings in invalid_settings:
                    with self.subTest(settings=raw_settings[:20]):
                        settings_path.write_text(raw_settings, encoding="utf-8")
                        with self.assertRaises(AgentDeliveryError) as raised:
                            ClaudeConfig.from_explicit(
                                model="glm-5.3",
                                base_url="https://open.bigmodel.cn/api/anthropic",
                                effort="max",
                                auth_config=settings_path,
                                repo_root=repo,
                            )
                        self.assertNotIn(str(settings_path), str(raised.exception))
                        self.assertNotIn("fake-", str(raised.exception))

    def test_settings_inside_target_repository_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary) / "repository"
            repo.mkdir()
            settings_path = repo / "claude-settings.json"
            settings_path.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": "fake-token"}}), encoding="utf-8")
            with patch("agent_delivery_loop.claude_worker.shutil.which", return_value="/fake/claude"):
                with self.assertRaisesRegex(AgentDeliveryError, "outside the target repository") as raised:
                    ClaudeConfig.from_explicit(
                        model="glm-5.3",
                        base_url="https://open.bigmodel.cn/api/anthropic",
                        effort="max",
                        auth_config=settings_path,
                        repo_root=repo,
                    )
            self.assertNotIn(str(settings_path), str(raised.exception))

    def test_settings_endpoint_must_exist_and_match_host_port_and_api_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repository"
            repo.mkdir()
            settings_path = base / "settings.json"
            mismatched_endpoints = (
                None,
                "not a URL",
                "https://other.example/api/anthropic",
                "https://open.bigmodel.cn:8443/api/anthropic",
                "https://open.bigmodel.cn/api/other",
                "https://open.bigmodel.cn/api/anthropic////",
                "https://faß.de/api/anthropic",
            )
            with patch("agent_delivery_loop.claude_worker.shutil.which", return_value="/fake/claude"):
                for source_url in mismatched_endpoints:
                    with self.subTest(source_url=source_url):
                        auth_env = {"ANTHROPIC_API_KEY": "fake-endpoint-bound-credential"}
                        if source_url is not None:
                            auth_env["ANTHROPIC_BASE_URL"] = source_url
                        settings_path.write_text(json.dumps({"env": auth_env}), encoding="utf-8")
                        with self.assertRaisesRegex(AgentDeliveryError, "do not match the explicit endpoint") as raised:
                            ClaudeConfig.from_explicit(
                                model="glm-5.3",
                                base_url="https://open.bigmodel.cn/api/anthropic",
                                effort="max",
                                auth_config=settings_path,
                                repo_root=repo,
                            )
                        self.assertNotIn(str(settings_path), str(raised.exception))
                        self.assertNotIn("fake-endpoint-bound-credential", str(raised.exception))

                settings_path.write_text(
                    json.dumps(
                        {
                            "env": {
                                "ANTHROPIC_API_KEY": "fake-endpoint-bound-credential",
                                "ANTHROPIC_BASE_URL": "https://open.bigmodel.cn/api/anthropic/",
                            }
                        }
                    ),
                    encoding="utf-8",
                )
                for explicit_url in (
                    "https://faß.de/api/anthropic",
                    "https://open.bigmodel.cn/api/anthropic////",
                ):
                    with self.subTest(explicit_url=explicit_url):
                        with self.assertRaisesRegex(AgentDeliveryError, "requested Claude base URL is invalid") as raised:
                            ClaudeConfig.from_explicit(
                                model="glm-5.3",
                                base_url=explicit_url,
                                effort="max",
                                auth_config=settings_path,
                                repo_root=repo,
                            )
                        self.assertNotIn(explicit_url, str(raised.exception))
                        self.assertNotIn("fake-endpoint-bound-credential", str(raised.exception))

    def test_effort_capability_check_requires_plain_help_to_advertise_level_without_auth(self) -> None:
        config = ClaudeConfig(
            executable="/fake/claude",
            model="glm-5.3",
            base_url="https://open.bigmodel.cn/api/anthropic",
            provider_host="open.bigmodel.cn",
            auth_name="ANTHROPIC_API_KEY",
            auth_value="fake-token",
            effort="max",
            target_repo_root=Path("/private/repository"),
        )
        help_result = SimpleNamespace(
            returncode=0,
            stdout="  --effort <level> Effort for the current session (low, medium, high, max)\n",
            stderr="",
        )
        with patch("agent_delivery_loop.claude_worker.subprocess.run", return_value=help_result) as run:
            _check_effort_support(config, Path("/private/home"))
        self.assertNotIn(config.auth_name, run.call_args.kwargs["env"])

        unsupported_help = SimpleNamespace(
            returncode=0,
            stdout="  --effort <level> Effort for the current session (low, medium, high)\n",
            stderr="",
        )
        with patch("agent_delivery_loop.claude_worker.subprocess.run", return_value=unsupported_help):
            with self.assertRaisesRegex(AgentDeliveryError, "does not advertise"):
                _check_effort_support(config, Path("/private/home"))

    def test_worker_argv_contains_effort_and_selected_auth_stays_only_in_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            worktree.mkdir()
            skill = worktree / "skill.md"
            skill.write_text("test skill\n", encoding="utf-8")
            order = parse_work_order((Path(__file__).parents[1] / "examples/work-orders/WO-2026-001.json").read_bytes())
            config = ClaudeConfig(
                executable="/fake/claude",
                model="glm-5.3",
                base_url="https://open.bigmodel.cn/api/anthropic",
                provider_host="open.bigmodel.cn",
                auth_name="ANTHROPIC_API_KEY",
                auth_value="fake-worker-credential",
                effort="max",
                target_repo_root=root,
                passthrough_environment=(("PATH", "/frozen/path"),),
            )
            process = SimpleNamespace(pid=12345, returncode=0, communicate=Mock(return_value=("{}", "")))
            outcome = WorkerOutcome("requested-session", "complete", [])
            isolated_home = root / "isolated-home"
            with (
                patch("agent_delivery_loop.claude_worker.subprocess.Popen", return_value=process) as popen,
                patch("agent_delivery_loop.claude_worker._process_group_exists", return_value=False),
                patch("agent_delivery_loop.claude_worker._validated_outcome", return_value=outcome),
            ):
                run_claude(worktree, skill, order, isolated_home, config)

            argv, options = popen.call_args.args[0], popen.call_args.kwargs
            self.assertEqual(argv[argv.index("--effort") + 1], "max")
            self.assertNotIn("fake-worker-credential", argv)
            self.assertEqual(options["env"]["ANTHROPIC_API_KEY"], "fake-worker-credential")
            self.assertEqual(options["env"]["PATH"], "/frozen/path")
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", options["env"])
            self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", options["env"])


class WorkerCompletionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ClaudeConfig(
            executable="claude",
            model="fixed-model",
            base_url="https://api.example.invalid",
            provider_host="api.example.invalid",
            auth_name="ANTHROPIC_AUTH_TOKEN",
            auth_value="test-secret-token-value",
            effort="max",
            target_repo_root=Path("/private/worker-repo"),
        )
        self.criteria = ["Create the requested file"]

    def _envelope(self, report: dict[str, object]) -> str:
        return json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": "requested-session",
                "structured_output": report,
            }
        )

    def test_zero_exit_with_blocked_report_fails_and_records_only_redacted_unfinished_items(self) -> None:
        report = {
            "status": "blocked",
            "criteria": [{"criterion": self.criteria[0], "status": "unresolved"}],
            "incomplete_items": ["Could not finish; token=test-secret-token-value"],
        }

        with self.assertRaises(WorkerResultError) as raised:
            _validated_outcome(
                self._envelope(report),
                returncode=0,
                requested_session_id="requested-session",
                acceptance_criteria=self.criteria,
                config=self.config,
                private_paths=("/private/worker-home",),
            )

        self.assertEqual(raised.exception.completion_status, "blocked")
        self.assertEqual(raised.exception.session_id, "requested-session")
        self.assertNotIn("test-secret-token-value", " ".join(raised.exception.incomplete_items))
        self.assertIn("[redacted]", " ".join(raised.exception.incomplete_items))

    def test_complete_status_with_unresolved_criterion_is_rejected_as_contradictory(self) -> None:
        report = {
            "status": "complete",
            "criteria": [{"criterion": self.criteria[0], "status": "unresolved"}],
            "incomplete_items": [],
        }

        with self.assertRaises(WorkerResultError) as raised:
            _validated_outcome(
                self._envelope(report),
                returncode=0,
                requested_session_id="requested-session",
                acceptance_criteria=self.criteria,
                config=self.config,
                private_paths=(),
            )

        self.assertEqual(raised.exception.completion_status, "invalid")
        self.assertEqual(raised.exception.incomplete_items, [self.criteria[0]])

    def test_missing_structured_output_fails_closed(self) -> None:
        envelope = json.loads(
            self._envelope(
                {
                    "status": "complete",
                    "criteria": [{"criterion": self.criteria[0], "status": "met"}],
                    "incomplete_items": [],
                }
            )
        )
        envelope.pop("structured_output")

        with self.assertRaises(WorkerResultError) as raised:
            _validated_outcome(
                json.dumps(envelope),
                returncode=0,
                requested_session_id="requested-session",
                acceptance_criteria=self.criteria,
                config=self.config,
                private_paths=(),
            )

        self.assertEqual(raised.exception.completion_status, "invalid")

    def test_short_selected_credential_is_redacted_from_incomplete_items(self) -> None:
        config = ClaudeConfig(
            executable="claude",
            model="fixed-model",
            base_url="https://api.example.invalid",
            provider_host="api.example.invalid",
            auth_name="ANTHROPIC_API_KEY",
            auth_value="tiny",
            effort="max",
            target_repo_root=Path("/private/worker-repo"),
        )
        report = {
            "status": "blocked",
            "criteria": [{"criterion": self.criteria[0], "status": "met"}],
            "incomplete_items": ["credential=tiny"],
        }
        with self.assertRaises(WorkerResultError) as raised:
            _validated_outcome(
                self._envelope(report),
                returncode=0,
                requested_session_id="requested-session",
                acceptance_criteria=self.criteria,
                config=config,
                private_paths=(),
            )
        self.assertEqual(raised.exception.incomplete_items, ["credential=[redacted]"])


class MergedPullRequestDiscoveryTests(unittest.TestCase):
    def test_scans_past_first_closed_pr_page_to_find_recent_merged_plan_prs(self) -> None:
        class FakeGitHub(GitHubClient):
            def __init__(self) -> None:
                super().__init__(Repo("owner", "repo"))
                self.pages: list[int] = []

            def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> object:
                page = int(parse_qs(urlsplit(path).query)["page"][0])
                self.pages.append(page)
                if page == 1:
                    return [
                        {"number": number, "merged_at": None, "base": {"ref": "main"}}
                        for number in range(1, 101)
                    ]
                return [
                    {"number": number, "merged_at": "2026-09-30T00:00:00Z", "base": {"ref": "main"}}
                    for number in range(101, 131)
                ]

            def pull_files(self, number: int) -> list[dict[str, object]]:
                if number == 101:
                    return [{"filename": ".agents/work-orders/WO-2026-001-r1.json", "status": "added"}]
                return []

        client = FakeGitHub()

        candidates = client.merged_plan_candidates()

        self.assertEqual(client.pages, [1, 2])
        self.assertEqual(
            candidates,
            [(101, ".agents/work-orders/WO-2026-001-r1.json", "https://github.com/owner/repo/pull/101")],
        )

    def test_safety_cap_fails_instead_of_claiming_no_task_when_scope_is_incomplete(self) -> None:
        class FakeGitHub(GitHubClient):
            def __init__(self) -> None:
                super().__init__(Repo("owner", "repo"))

            def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> object:
                return [
                    {"number": number, "merged_at": None, "base": {"ref": "main"}}
                    for number in range(1, 101)
                ]

            def pull_files(self, number: int) -> list[dict[str, object]]:
                return []

        client = FakeGitHub()
        with patch("agent_delivery_loop.github.MAX_CLOSED_PR_PAGES", 1):
            with self.assertRaises(AgentDeliveryError):
                client.merged_plan_candidates()


class GitReplaceRefTests(unittest.TestCase):
    def test_executor_reads_ignore_replace_refs_and_keep_them_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)

            def raw(*args: str) -> str:
                return subprocess.run(
                    ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
                ).stdout

            raw("init", "-q")
            raw("config", "user.name", "Regression Test")
            raw("config", "user.email", "regression@example.invalid")
            (repo / "base.md").write_text("base\n", encoding="utf-8")
            raw("add", ".")
            raw("commit", "-m", "base")
            base = raw("rev-parse", "HEAD").strip()
            (repo / "delivered.md").write_text("original candidate\n", encoding="utf-8")
            raw("add", ".")
            raw("commit", "-m", "candidate")
            head = raw("rev-parse", "HEAD").strip()

            raw("checkout", "-q", "--detach", base)
            (repo / "delivered.md").write_text("replaced content\n", encoding="utf-8")
            raw("add", ".")
            raw("commit", "-m", "impostor")
            impostor = raw("rev-parse", "HEAD").strip()
            raw("replace", head, impostor)

            # The failure signal: plain Git honors refs/replace/* for the labeled SHA.
            self.assertIn("replaced content", raw("show", f"{head}:delivered.md"))
            self.assertNotIn("replaced content", git(repo, "show", f"{head}:delivered.md").stdout)
            self.assertIn("original candidate", git(repo, "show", f"{head}:delivered.md").stdout)
            diff = git(
                repo, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", base, head
            ).stdout
            self.assertIn("original candidate", diff)
            self.assertNotIn("replaced content", diff)
            self.assertEqual(
                git(repo, "show", "-s", "--format=%P", head).stdout.strip(), base
            )
            self.assertTrue(
                subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "--verify", "-q", f"refs/replace/{head}"],
                    capture_output=True,
                ).returncode
                == 0,
                "existing replace refs must not be deleted by the executor",
            )


class DeliveredPathTests(unittest.TestCase):
    def test_ignored_files_are_scope_checked_but_only_committed_paths_are_deliverable(self) -> None:
        class DocsOnlyOrder:
            def allows_path(self, path: str) -> bool:
                return path.startswith("docs/")

        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)

            def git(*args: str) -> None:
                subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

            git("init", "-q")
            git("config", "user.name", "Regression Test")
            git("config", "user.email", "regression@example.invalid")
            (repo / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
            (repo / "docs").mkdir()
            (repo / "docs/base.md").write_text("base\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "base")

            (repo / "outside.tmp").write_text("out of scope\n", encoding="utf-8")
            with self.assertRaises(AgentDeliveryError):
                _safe_changed_paths(repo, DocsOnlyOrder())  # type: ignore[arg-type]
            (repo / "outside.tmp").unlink()

            (repo / "docs/only.tmp").write_text("ignored only\n", encoding="utf-8")
            _safe_changed_paths(repo, DocsOnlyOrder())  # type: ignore[arg-type]
            with self.assertRaises(AgentDeliveryError):
                stage_changes(repo)
            (repo / "docs/only.tmp").unlink()

            (repo / "docs/delivered.md").write_text("delivered\n", encoding="utf-8")
            (repo / "docs/ignored.tmp").write_text("ignored\n", encoding="utf-8")
            _safe_changed_paths(repo, DocsOnlyOrder())  # type: ignore[arg-type]
            staged = stage_changes(repo)
            self.assertEqual(staged.paths, ("docs/delivered.md",))

            hooks = repo / ".git" / "hooks"
            hooks.mkdir(exist_ok=True)
            hook_ran = repo / "hook-ran"
            pre_commit = hooks / "pre-commit"
            pre_commit.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                "import subprocess, sys\n"
                "repo = Path.cwd()\n"
                "(repo / 'docs/delivered.md').write_text('changed by hook\\n')\n"
                "subprocess.run(['git', 'add', 'docs/delivered.md'], check=True)\n"
                f"Path({str(hook_ran)!r}).write_text('ran')\n",
                encoding="utf-8",
            )
            pre_commit.chmod(0o700)

            committed = commit_changes(repo, "WO-2026-001-r1", staged)
            self.assertFalse(hook_ran.exists())
            self.assertTrue(pre_commit.exists(), "the user's hook must not be deleted")
            self.assertEqual(committed.paths, staged.paths)
            self.assertEqual(committed.commit_sha, subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip())
            committed = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(committed, staged.tree_sha)
            self.assertEqual((repo / "docs/delivered.md").read_text(encoding="utf-8"), "delivered\n")


class RunRecordRoundTripTests(unittest.TestCase):
    def test_completed_delivery_paths_roundtrip_through_run_store(self) -> None:
        order_path = ".agents/work-orders/WO-TEST-001-r1.json"
        skill_path = ".agents/policies/delivery/SKILL.md"
        skill_bytes = b"Use the local file tools only.\n"
        order_bytes = json.dumps(
            {
                "schema_version": 1,
                "task_id": "WO-TEST-001",
                "revision": 1,
                "objective": "Create one documentation file.",
                "out_of_scope": [],
                "acceptance_criteria": ["Create docs/delivered.md"],
                "allowed_paths": ["docs/**"],
                "worker_profile": "claude-code-v1",
                "skill_ref": skill_path,
                "stop_conditions": ["Stop when the file is created."],
                "limits": {"max_turns": 1, "timeout_seconds": 60, "max_budget_usd": 1},
            }
        ).encode("utf-8")

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repository"
            state_dir = base / "state"
            repo.mkdir()

            def git(*args: str) -> str:
                result = subprocess.run(
                    ["git", "-C", str(repo), *args],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return result.stdout.strip()

            git("init", "-q")
            git("config", "user.name", "Regression Test")
            git("config", "user.email", "regression@example.invalid")
            git("remote", "add", "origin", "https://github.com/BH2-4/agent-delivery-loop.git")
            skill_file = repo / skill_path
            skill_file.parent.mkdir(parents=True)
            skill_file.write_bytes(skill_bytes)
            git("add", ".")
            git("commit", "-m", "test base")
            merge_sha = git("rev-parse", "HEAD")

            github = Mock()
            github.authorized_plan.return_value = SimpleNamespace(
                merge_sha=merge_sha,
                order_path=order_path,
                order_bytes=order_bytes,
            )
            github.content.return_value = skill_bytes
            worker_config = ClaudeConfig(
                executable="/not-started/claude",
                model="test-model",
                base_url="https://api.example.invalid",
                provider_host="api.example.invalid",
                auth_name="ANTHROPIC_API_KEY",
                auth_value="test-only-credential",
                effort="high",
                target_repo_root=repo,
            )
            commits: list[DeliveryCommit] = []

            def record_commit(worktree: Path, task_identity: str, staged: StagedSnapshot) -> DeliveryCommit:
                committed = commit_changes(worktree, task_identity, staged)
                commits.append(committed)
                return committed

            def fake_worker(
                worktree: Path,
                _skill: Path,
                _order: object,
                _runtime_home: Path,
                _config: ClaudeConfig,
                *,
                lifecycle: WorkerLifecycle,
            ) -> WorkerOutcome:
                (worktree / "docs").mkdir()
                (worktree / "docs/delivered.md").write_text("candidate\n", encoding="utf-8")
                lifecycle.status = "stopped"
                lifecycle.session_id = "test-session"
                return WorkerOutcome("test-session", "complete", [])

            with (
                patch("agent_delivery_loop.runner.default_state_dir", return_value=state_dir),
                patch("agent_delivery_loop.runner.GitHubClient", return_value=github),
                patch("agent_delivery_loop.runner.ensure_plan_is_on_main"),
                patch("agent_delivery_loop.runner.preflight", return_value=(2, 1, 288)),
                patch("agent_delivery_loop.runner.run_claude", side_effect=fake_worker),
                patch("agent_delivery_loop.runner.commit_changes", side_effect=record_commit),
            ):
                result = execute_plan(
                    repo_path=repo,
                    plan_pr="https://github.com/BH2-4/agent-delivery-loop/pull/2",
                    work_order_path=order_path,
                    worker_config=worker_config,
                )

            self.assertEqual(len(commits), 1)
            self.assertIsInstance(commits[0], DeliveryCommit)
            self.assertEqual(commits[0].paths, ("docs/delivered.md",))
            self.assertIs(type(result["changed_paths"]), list)
            self.assertEqual(result["changed_paths"], ["docs/delivered.md"])

            store = RunStore(state_dir)
            key = task_key("BH2-4/agent-delivery-loop", "WO-TEST-001", 1)
            persisted = store._read_record(store.record_path(key, result["run_id"]))
            self.assertEqual(persisted["changed_paths"], ["docs/delivered.md"])
            self.assertEqual(persisted["status"], "local_ready")
            self.assertEqual(persisted["worker_status"], "stopped")
            store.assert_worker_available()


class WorkerCancellationTests(unittest.TestCase):
    def test_cancel_during_spawn_registration_stops_worker_before_home_record_and_lock_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            worktree.mkdir()
            skill = worktree / "skill.md"
            skill.write_text("test skill\n", encoding="utf-8")
            ready = worktree / "worker-pids"
            executable = root / "temporary-worker"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import os, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "skill = Path(sys.argv[sys.argv.index('--append-system-prompt-file') + 1])\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "skill.with_name('worker-pids').write_text(f'{os.getpid()} {child.pid} {os.getpgrp()}')\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            isolated_home = root / "private-home"
            store = RunStore(root / "state")
            run_id = "run-start-cancel-test"
            identity = task_key("owner/repo", "WO-2026-001", 1)
            record: dict[str, object] = {
                "schema_version": 1,
                "run_id": run_id,
                "status": "starting",
                "worker_status": "start_unconfirmed",
                "finished_at": None,
                "session_id": None,
            }
            order_path = Path(__file__).parents[1] / "examples/work-orders/WO-2026-001.json"
            order = parse_work_order(order_path.read_bytes())
            config = ClaudeConfig(
                executable=str(executable),
                model="fixed-model",
                base_url="https://api.example.invalid",
                provider_host="api.example.invalid",
                auth_name="ANTHROPIC_AUTH_TOKEN",
                auth_value="test-secret-token-value",
                effort="max",
                target_repo_root=root,
            )
            lifecycle = WorkerLifecycle()
            original_popen = subprocess.Popen
            original_write = store.write
            verified_record_order: list[str] = []

            def spawn_and_queue_interrupt(*args: object, **kwargs: object) -> subprocess.Popen[str]:
                process = original_popen(*args, **kwargs)  # type: ignore[arg-type]
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not ready.exists():
                    _stop_process_group(process)
                    self.fail("temporary Worker did not reach the process-registration boundary")
                # run_claude must defer SIGINT here until Popen returns and the
                # process handle/lifecycle have been registered.
                os.kill(os.getpid(), signal.SIGINT)
                return process

            def check_record_write(key: str, current_run_id: str, value: dict[str, object]) -> None:
                if value.get("status") == "cancelled":
                    self.assertFalse(isolated_home.exists(), "HOME must be removed only after Worker shutdown")
                    descriptor = os.open(store.locks / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
                    try:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        os.close(descriptor)
                    verified_record_order.append("record_while_locked")
                original_write(key, current_run_id, value)

            error: AgentDeliveryError | None = None
            with (
                patch.object(subprocess, "Popen", new=spawn_and_queue_interrupt),
                patch.object(store, "write", new=check_record_write),
            ):
                try:
                    with store.claim(identity):
                        store.write(identity, run_id, record)
                        try:
                            run_claude(
                                worktree,
                                skill,
                                order,
                                isolated_home,
                                config,
                                lifecycle=lifecycle,
                            )
                        except WorkerCancelled as cancelled:
                            _record_cancelled_run(
                                store,
                                identity,
                                run_id,
                                record,
                                session_id=cancelled.session_id,
                                reason="Cancellation completed after the Worker process group stopped.",
                            )
                            raise AgentDeliveryError(f"Work Order was cancelled. Run ID: {run_id}.") from None
                except AgentDeliveryError as exc:
                    error = exc

            self.assertIsNotNone(error)
            self.assertIn(run_id, str(error))
            self.assertEqual(lifecycle.status, "stopped")
            self.assertTrue(lifecycle.session_id)
            self.assertEqual(verified_record_order, ["record_while_locked"])
            self.assertFalse(isolated_home.exists())
            _, _, process_group = map(int, ready.read_text(encoding="utf-8").split())
            self.assertFalse(_process_group_exists(process_group))
            stored = json.loads(store.record_path(identity, run_id).read_text(encoding="utf-8"))
            self.assertEqual(stored["status"], "cancelled")
            self.assertIsNotNone(stored["finished_at"])
            self.assertTrue(stored["session_id"])
            with store.claim(identity):
                pass

    def test_cancel_stops_group_before_home_record_and_lock_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            worktree.mkdir()
            skill = worktree / "skill.md"
            skill.write_text("test skill\n", encoding="utf-8")
            ready = worktree / "worker-pids"
            executable = root / "temporary-worker"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import os, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "skill = Path(sys.argv[sys.argv.index('--append-system-prompt-file') + 1])\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                "skill.with_name('worker-pids').write_text(f'{os.getpid()} {child.pid} {os.getpgrp()}')\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            isolated_home = root / "private-home"
            store = RunStore(root / "state")
            run_id = "run-cancel-test"
            identity = task_key("owner/repo", "WO-2026-001", 1)
            record: dict[str, object] = {
                "schema_version": 1,
                "run_id": run_id,
                "status": "starting",
                "worker_status": "start_unconfirmed",
                "finished_at": None,
                "session_id": None,
            }
            order_path = Path(__file__).parents[1] / "examples/work-orders/WO-2026-001.json"
            order = parse_work_order(order_path.read_bytes())
            config = ClaudeConfig(
                executable=str(executable),
                model="fixed-model",
                base_url="https://api.example.invalid",
                provider_host="api.example.invalid",
                auth_name="ANTHROPIC_AUTH_TOKEN",
                auth_value="test-secret-token-value",
                effort="max",
                target_repo_root=root,
            )
            original_write = store.write
            verified_record_order: list[str] = []

            def check_record_write(key: str, current_run_id: str, value: dict[str, object]) -> None:
                if value.get("status") == "cancelled":
                    self.assertFalse(isolated_home.exists(), "HOME must be removed only after Worker shutdown")
                    descriptor = os.open(store.locks / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
                    try:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    finally:
                        os.close(descriptor)
                    verified_record_order.append("record_while_locked")
                original_write(key, current_run_id, value)

            def interrupt_after_start(process: subprocess.Popen[str], *args: object, **kwargs: object) -> tuple[str, str]:
                deadline = time.monotonic() + 10
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                if not ready.exists():
                    raise AssertionError("temporary Worker did not start")
                raise KeyboardInterrupt

            error: AgentDeliveryError | None = None
            with (
                patch.object(subprocess.Popen, "communicate", new=interrupt_after_start),
                patch.object(store, "write", new=check_record_write),
            ):
                try:
                    with store.claim(identity):
                        store.write(identity, run_id, record)
                        try:
                            run_claude(worktree, skill, order, isolated_home, config)
                        except WorkerCancelled as cancelled:
                            _record_cancelled_run(
                                store,
                                identity,
                                run_id,
                                record,
                                session_id=cancelled.session_id,
                                reason="Cancellation completed after the Worker process group stopped.",
                            )
                            raise AgentDeliveryError(f"Work Order was cancelled. Run ID: {run_id}.") from None
                except AgentDeliveryError as exc:
                    error = exc

            self.assertIsNotNone(error)
            self.assertIn(run_id, str(error))
            self.assertEqual(verified_record_order, ["record_while_locked"])
            self.assertFalse(isolated_home.exists())
            parent_pid, child_pid, process_group = map(int, ready.read_text(encoding="utf-8").split())
            self.assertFalse(_process_group_exists(process_group))
            for pid in (parent_pid, child_pid):
                with self.assertRaises(ProcessLookupError):
                    os.kill(pid, 0)
            stored = json.loads(store.record_path(identity, run_id).read_text(encoding="utf-8"))
            self.assertEqual(stored["status"], "cancelled")
            self.assertIsNotNone(stored["finished_at"])
            self.assertTrue(stored["session_id"])
            stderr = StringIO()
            with redirect_stderr(stderr):
                exit_code = _report_error(error)  # type: ignore[arg-type]
            self.assertNotEqual(exit_code, 0)
            self.assertIn(run_id, stderr.getvalue())
            with store.claim(identity):
                pass


class WorkerSafetyGateTests(unittest.TestCase):
    @contextmanager
    def _runner_fixture(self, root: Path):
        """Exercise the runner with authorization/Git stubs and no Claude executable."""
        repo = Repo("owner", "repo")
        order_path = ".agents/work-orders/WO-2026-001-r1.json"
        raw = (Path(__file__).parents[1] / "examples/work-orders/WO-2026-001.json").read_bytes()
        skill_bytes = b"temporary test skill\n"
        order = parse_work_order(raw)
        authorization = SimpleNamespace(merge_sha="a" * 40, order_bytes=raw, order_path=order_path)
        client = Mock()
        client.authorized_plan.return_value = authorization
        client.content.return_value = skill_bytes
        config = ClaudeConfig(
            executable=str(root / "never-executed-claude"),
            model="fixed-model",
            base_url="https://api.example.invalid",
            provider_host="api.example.invalid",
            auth_name="ANTHROPIC_AUTH_TOKEN",
            auth_value="test-secret-token-value",
            effort="max",
            target_repo_root=root,
        )

        def make_worktree(_repo, destination, _branch, _sha):
            skill = destination / order.skill_ref
            skill.parent.mkdir(parents=True)
            skill.write_bytes(skill_bytes)

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"AGENT_STATE_DIR": str(root / "state")}))
            for name, replacement in (
                ("repository_root", Mock(return_value=root)),
                ("repository_remote", Mock(return_value=repo)),
                ("GitHubClient", Mock(return_value=client)),
                ("preflight", Mock(return_value=(2, 1, 284))),
                ("ensure_plan_is_on_main", Mock()),
                ("create_worktree", make_worktree),
            ):
                stack.enter_context(patch(f"agent_delivery_loop.runner.{name}", replacement))
            commit = stack.enter_context(patch("agent_delivery_loop.runner.commit_changes"))
            yield RunStore(root / "state"), commit, {
                "repo_path": root,
                "plan_pr": "owner/repo#123",
                "work_order_path": order_path,
                "worker_config": config,
            }

    def _stored_run(self, store):
        paths = list(store.runs.glob("*/*.json"))
        self.assertEqual(len(paths), 1)
        return json.loads(paths[0].read_text(encoding="utf-8"))

    def _assert_next_run_blocked(self, store):
        # A fresh store and different task simulate executor exit and a new attempt.
        with self.assertRaises(RunStateError):
            with RunStore(store.root).claim(task_key("owner/repo", "WO-DIFFERENT", 1)):
                self.fail("Unconfirmed prior Worker must block all subsequent tasks")

    def test_creation_exception_before_handle_retains_gate_and_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_popen = subprocess.Popen
            children = []
            with self._runner_fixture(root) as (store, commit, args):
                def create_then_raise(*_args, **_kwargs):
                    with self.assertRaises(RunStateError):
                        RunStore(store.root).assert_worker_available()
                    children.append(original_popen(
                        [sys.executable, "-c", "import time; time.sleep(60)"], **_kwargs,
                    ))
                    raise OSError("Injected failure after child creation, before handle return")

                try:
                    with patch.object(subprocess, "Popen", new=create_then_raise):
                        with self.assertRaisesRegex(AgentDeliveryError, "cleanup failed"):
                            execute_plan(**args)
                    record = self._stored_run(store)
                    self.assertEqual(record["worker_status"], "stop_unconfirmed")
                    self.assertEqual(record["requested_effort"], "max")
                    self.assertEqual(record["auth_source"], "claude_settings")
                    self.assertEqual(record["auth_env_name"], "ANTHROPIC_AUTH_TOKEN")
                    self.assertNotIn("test-secret-token-value", json.dumps(record))
                    self.assertEqual(record["status"], "cleanup_failed")
                    self.assertIsNone(record["finished_at"])
                    self.assertIsNone(children[0].poll())
                    self.assertTrue((store.root / "runtime" / record["run_id"]).is_dir())
                    self._assert_next_run_blocked(store)
                    commit.assert_not_called()
                finally:
                    for process in children:
                        self.assertTrue(_stop_process_group(process))
                # Even a known test child stopping must not automatically clear the record.
                self._assert_next_run_blocked(store)

    def test_unconfirmed_stop_update_failure_or_interrupt_preserves_start_gate(self):
        for failure in (OSError("Injected write failure"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                original_popen, original_replace = subprocess.Popen, os.replace
                children = []
                with self._runner_fixture(root) as (store, commit, args):
                    def launch(*_args, **_kwargs):
                        child = original_popen(
                            [sys.executable, "-c", "import time; time.sleep(60)"], **_kwargs,
                        )
                        children.append(child)
                        return child

                    def replace(source, destination):
                        if Path(destination).exists():
                            raise failure
                        return original_replace(source, destination)

                    try:
                        with (
                            patch.object(subprocess, "Popen", new=launch),
                            patch.object(original_popen, "communicate", side_effect=KeyboardInterrupt),
                            patch("agent_delivery_loop.claude_worker._stop_process_group", return_value=False),
                            patch("agent_delivery_loop.store.os.replace", new=replace),
                        ):
                            with self.assertRaises((RunStateError, KeyboardInterrupt)):
                                execute_plan(**args)
                        record = self._stored_run(store)
                        self.assertEqual(record["worker_status"], "start_unconfirmed")
                        self.assertEqual(record["status"], "starting")
                        self.assertTrue((store.root / "runtime" / record["run_id"]).is_dir())
                        self.assertFalse(store.cleanup_failure.exists())
                        self._assert_next_run_blocked(store)
                        commit.assert_not_called()
                    finally:
                        for process in children:
                            self.assertTrue(_stop_process_group(process))

    def test_unreadable_invalid_or_unpersisted_state_never_launches(self):
        for failure in ("invalid", "unreadable", "write", "readback"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                with self._runner_fixture(Path(temporary)) as (store, commit, args):
                    with ExitStack() as stack:
                        launch = stack.enter_context(patch.object(subprocess, "Popen"))
                        if failure == "invalid":
                            directory = store.runs / "prior-task"
                            directory.mkdir()
                            (directory / "prior.json").write_text('{"worker_status":', encoding="utf-8")
                        elif failure == "unreadable":
                            original_iterdir = Path.iterdir

                            def iterdir(path):
                                if path == store.runs:
                                    raise PermissionError("Injected state read failure")
                                return original_iterdir(path)

                            stack.enter_context(patch.object(Path, "iterdir", new=iterdir))
                        elif failure == "write":
                            stack.enter_context(patch("agent_delivery_loop.store.os.replace", side_effect=OSError))
                        else:
                            stack.enter_context(patch.object(RunStore, "_read_record", side_effect=OSError))
                        with self.assertRaises(RunStateError):
                            execute_plan(**args)
                        launch.assert_not_called()
                        commit.assert_not_called()

    def test_sigint_during_home_cleanup_aborts_successful_delivery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_popen = subprocess.Popen
            with self._runner_fixture(root) as (store, commit, args):
                def launch(*_args, **kwargs):
                    return original_popen([sys.executable, "-c", "pass"], **kwargs)

                def complete(_stdout, **kwargs):
                    return WorkerOutcome(kwargs["requested_session_id"], "complete", [])

                from agent_delivery_loop import claude_worker
                original_rmtree = claude_worker.shutil.rmtree

                def interrupt_cleanup(path, *cleanup_args, **cleanup_kwargs):
                    os.kill(os.getpid(), signal.SIGINT)
                    return original_rmtree(path, *cleanup_args, **cleanup_kwargs)

                with (
                    patch.object(subprocess, "Popen", new=launch),
                    patch.object(claude_worker, "_validated_outcome", new=complete),
                    patch.object(claude_worker.shutil, "rmtree", new=interrupt_cleanup),
                ):
                    with self.assertRaisesRegex(AgentDeliveryError, "cancelled"):
                        execute_plan(**args)
                record = self._stored_run(store)
                self.assertEqual(record["worker_status"], "stopped")
                self.assertEqual(record["status"], "cancelled")
                self.assertFalse((store.root / "runtime" / record["run_id"]).exists())
                commit.assert_not_called()
                with RunStore(store.root).claim("next-task"):
                    pass


if __name__ == "__main__":
    unittest.main()
