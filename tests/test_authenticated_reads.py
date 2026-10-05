"""Focused regressions: explicit-identity propagation to agent-run reads and the
bounded transient retry for identified read-only gh invocations. No real secrets,
network calls, or model subprocesses are used."""

from __future__ import annotations

import contextlib
import json
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

    def assert_worker_available(self, *, exempt_record: Path | None = None) -> None:
        return None

    def claim(self, _key: str, *, exempt_record: Path | None = None):
        return contextlib.nullcontext()

    def write(self, *_args, **_kwargs) -> None:
        return None


class OrchestratorPatChannelTests(unittest.TestCase):
    def test_channel_env_carries_snapshot_value_only_to_the_child(self) -> None:
        identity = _identity()
        with tempfile.TemporaryDirectory() as temporary:
            stdout = Path(temporary) / "out.json"
            stdout.write_text("{}", encoding="utf-8")
            gate_path = Path(temporary) / "runs" / "k" / "spawn-abcd1234.json"
            gate_path.parent.mkdir(parents=True)
            captured: dict[str, object] = {}
            gate_writes: list[dict] = []

            class FakeStore:
                def write(self, _key, run_id, record):  # gate flip durability
                    gate_writes.append({"run_id": run_id, "phase": record.get("phase")})

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
                    store=FakeStore(), task_key_value="k", gate_path=gate_path,
                )
            self.assertEqual(result, {"agent_run_exit_code": 0})
            env = captured["env"]
            self.assertEqual(env["AGENT_DELIVERY_PAT"], FAKE_PAT)
            self.assertEqual(env["AGENT_DELIVERY_PAT_LOGIN"], "o")
            self.assertEqual(env["AGENT_DELIVERY_SPAWN_GATE"], f"k/{gate_path.name}")
            self.assertNotIn("GH_TOKEN", env)
            self.assertNotIn(FAKE_PAT, " ".join(captured["argv"]))
            # The orchestrator's own environment never carries the channel.
            self.assertNotIn("AGENT_DELIVERY_PAT", os.environ)
            # The gate was flipped to the blocking phase once the handle existed.
            self.assertEqual(gate_writes, [{"run_id": "spawn-abcd1234", "phase": "started"}])

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
            ["api", "-XPOST", "repos/o/r/pulls"],
            ["api", "--method=POST", "repos/o/r/pulls"],
            ["api", "repos/o/r/pulls", "-f", "title=x"],
            ["api", "repos/o/r/pulls", "-F", "title=x"],
            ["api", "repos/o/r/pulls", "--raw-field", "title=x"],
            ["api", "repos/o/r/pulls", "--input", "body.json"],
            ["api", "repos/o/r/pulls", "--field=title=x"],
            # gh api treats extra positionals as key=value fields and switches to POST.
            ["api", "repos/o/r/pulls", "title=x"],
            # --slurp is a boolean flag: it must not consume (and hide) a method override.
            ["api", "--slurp", "--method=POST", "repos/o/r/pulls"],
            # Everything after "--" is positional; a second positional is a POST field.
            ["api", "--", "repos/o/r/pulls", "title=x"],
            ["pr", "merge", "26", "--repo", "o/r", "--merge"],
        ):
            with patch(
                "agent_delivery_loop.publish.run_gh",
                return_value=_completed(1, stderr="net/http: TLS handshake timeout"),
            ) as raw:
                result = publish.run_gh_read(args, identity=identity)
            self.assertEqual(raw.call_count, 1, msg=str(args))
            self.assertEqual(result.returncode, 1)

    def test_allowlisted_get_flags_still_retry(self) -> None:
        identity = _identity()
        outcomes = [
            _completed(1, stderr="net/http: TLS handshake timeout"),
            _completed(0, stdout='"login"'),
        ]
        for args in (
            ["api", "user", "--jq", ".login"],
            ["api", "user", "--slurp", "--jq=.login", "--verbose"],
        ):
            with patch("agent_delivery_loop.publish.run_gh", side_effect=list(outcomes)) as raw:
                result = publish.gh_json(list(args), identity=identity)
            self.assertEqual(result, "login", msg=str(args))
            self.assertEqual(raw.call_count, 2, msg=str(args))

    def test_empty_gh_args_report_sanitized_error(self) -> None:
        identity = _identity()
        with patch("agent_delivery_loop.publish.subprocess.run", side_effect=OSError("boom")):
            with self.assertRaises(AgentDeliveryError) as caught:
                publish.run_gh_read([], identity=identity)
        self.assertIn("no arguments", str(caught.exception))

    def test_remote_branch_404_still_maps_to_none_without_retry(self) -> None:
        identity = _identity()
        with patch(
            "agent_delivery_loop.publish.run_gh",
            return_value=_completed(1, stderr="gh: Not Found (HTTP 404)"),
        ) as raw:
            self.assertIsNone(publish.remote_branch_sha("o/r", "agent/x", identity=identity))
        self.assertEqual(raw.call_count, 1)


class SpawnInterruptionTests(unittest.TestCase):
    def _spawn(self, temporary: Path, stop_result: bool, expected: type[BaseException]):
        stdout = temporary / "out.json"
        gate_path = temporary / "spawn-abcd1234.json"

        class InterruptedProcess:
            def wait(self, timeout=None):  # noqa: ANN001 - subprocess API shape
                raise KeyboardInterrupt()

        class FakeStore:
            def write(self, *_args, **_kwargs) -> None:
                return None

        with patch(
            "agent_delivery_loop.orchestrator.subprocess.Popen", return_value=InterruptedProcess(),
        ), patch(
            "agent_delivery_loop.orchestrator._stop_agent_run_bounded", return_value=stop_result,
        ) as stop:
            with self.assertRaises(expected) as caught:
                orchestrator._spawn_agent_run(
                    entry="agent-run", repo_root=temporary,
                    plan_pr="https://github.com/o/r/pull/26",
                    work_order_path=".agents/work-orders/WO-X-001-r1.json",
                    model="m", base_url="https://api.example", effort="max",
                    auth_config=Path("/nowhere"), timeout_seconds=30, proxy=None,
                    stdout_path=stdout, identity=_identity(),
                    store=FakeStore(), task_key_value="k", gate_path=gate_path,
                )
        stop.assert_called_once()
        return caught.exception

    def test_interrupt_with_confirmed_stop_propagates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self._spawn(Path(temporary), stop_result=True, expected=KeyboardInterrupt)

    def test_interrupt_with_unconfirmed_stop_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            raised = self._spawn(Path(temporary), stop_result=False, expected=AgentDeliveryError)
            self.assertIn("could not be confirmed stopped", str(raised))


class ReviewIdentityWiringTests(unittest.TestCase):
    def _repo(self, base: Path) -> Path:
        repo = base / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/r.git"], check=True)
        (repo / "f.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "c"], check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        return repo

    def _record(self, base: Path, run_id: str) -> Path:
        record_dir = base / "runs"
        record_dir.mkdir(exist_ok=True)
        record = {
            "schema_version": 1, "run_id": run_id, "session_id": "11111111-1111-1111-1111-111111111111",
            "worker_status": "stopped", "status": "local_ready", "completion_status": "complete",
            "incomplete_items": [], "failure": None, "finished_at": "2026-10-06T00:00:00+00:00",
        }
        path = record_dir / f"{run_id}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def test_snapshot_builds_the_authenticated_client_from_the_identity(self) -> None:
        import agent_delivery_loop.review_handoff as handoff

        class Sentinel(AgentDeliveryError):
            pass

        seen: dict[str, object] = {}

        class FakeClient:
            def __init__(self, repo, token=None):  # noqa: ANN001
                seen["repo"] = repo.slug
                seen["token"] = token
                raise Sentinel("stop-before-network")

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = self._repo(base)
            record = self._record(base, "22222222-2222-2222-2222-222222222222")
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
            with patch.object(handoff, "GitHubClient", FakeClient):
                with self.assertRaises(Sentinel):
                    handoff._snapshot(
                        repo_path=repo, plan_pr="o/r#26",
                        work_order_path=".agents/work-orders/WO-X-001-r1.json",
                        run_record=record, head_sha=head, pat_identity=_identity(),
                    )
                self.assertEqual(seen, {"repo": "o/r", "token": FAKE_PAT})
                seen.clear()
                with self.assertRaises(Sentinel):
                    handoff._snapshot(
                        repo_path=repo, plan_pr="o/r#26",
                        work_order_path=".agents/work-orders/WO-X-001-r1.json",
                        run_record=record, head_sha=head,
                    )
                # The legacy entry keeps the anonymous client.
                self.assertEqual(seen, {"repo": "o/r", "token": None})

    def test_review_loop_forwards_identity_to_prepare_and_check(self) -> None:
        from unittest.mock import Mock

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = self._repo(base)
            branch = "agent/x-r1-abcd1234"
            subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", branch], check=True)
            head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                                  capture_output=True, text=True).stdout.strip()
            receipt = base / "res.json"
            receipt.write_text(json.dumps({"verdict": "pass", "findings": []}), encoding="utf-8")
            instance = Mock()
            instance.record = {"candidate": {"sha": head, "origin": "worker", "review_round": 0}}
            fake_review = {"review_id": "rv", "codex_version": "v", "exit_code": 0,
                           "stop_status": "confirmed_stopped", "result_path": str(receipt)}
            identity = _identity()
            params = orchestrator._Params(
                plan_pr="o/r#26", work_order_path="wo", review_timeout_seconds=60,
                review_model="m", review_effort="high", review_bundle_dir=base / "bundle", proxy=None,
                model="m", base_url="https://x", effort="max", auth_config=base / "a.json",
            )

            class _Repo:
                slug = "o/r"

            with patch.object(orchestrator, "prepare_review") as prep, \
                 patch.object(orchestrator, "run_review_process", return_value=fake_review), \
                 patch.object(orchestrator, "check_review") as check:
                state = orchestrator._review_loop(
                    instance, params, repo, _Repo(), Mock(), None,
                    base / "rr.json", repo, branch, skill_sha256=None, identity=identity,
                )
            self.assertEqual(state, {"candidate": head})
            self.assertIs(prep.call_args.kwargs["pat_identity"], identity)
            self.assertIs(check.call_args.kwargs["pat_identity"], identity)


class TwoLayerStopTests(unittest.TestCase):
    """Real local subprocesses, no credentials and no models: prove that stopping the
    outer Python process group is possible while an inner, separately-sessioned child
    stays alive — exactly why outer-stop can never prove the inner Worker stopped."""

    def test_flip_failure_stops_the_child_and_keeps_the_gate(self) -> None:
        identity = _identity()
        with tempfile.TemporaryDirectory() as temporary:
            stdout = Path(temporary) / "out.json"
            gate_path = Path(temporary) / "spawn-abcd1234.json"
            gate_path.write_text("{}", encoding="utf-8")  # the pending gate on disk

            class FlipFailureStore:
                def write(self, *_args, **_kwargs):
                    raise RuntimeError("disk full")

            with patch("agent_delivery_loop.orchestrator.subprocess.Popen") as popen, \
                 patch("agent_delivery_loop.orchestrator._stop_agent_run_bounded", return_value=True) as stop:
                popen.return_value.wait.return_value = 0
                with self.assertRaises(RuntimeError):
                    orchestrator._spawn_agent_run(
                        entry="agent-run", repo_root=Path(temporary),
                        plan_pr="https://github.com/o/r/pull/26",
                        work_order_path=".agents/work-orders/WO-X-001-r1.json",
                        model="m", base_url="https://api.example", effort="max",
                        auth_config=Path("/nowhere"), timeout_seconds=30, proxy=None,
                        stdout_path=stdout, identity=identity,
                        store=FlipFailureStore(), task_key_value="k", gate_path=gate_path,
                    )
            stop.assert_called_once()
            self.assertTrue(gate_path.exists())  # durably retained, not removed

    def test_stdout_open_failure_is_not_started_and_removes_no_child_assumptions(self) -> None:
        identity = _identity()
        with tempfile.TemporaryDirectory() as temporary:
            stdout = Path(temporary) / "missing-dir" / "out.json"  # parent does not exist
            gate_path = Path(temporary) / "spawn-abcd1234.json"

            class FakeStore:
                def write(self, *_args, **_kwargs) -> None:
                    return None

            with patch("agent_delivery_loop.orchestrator.subprocess.Popen") as popen:
                with self.assertRaises(orchestrator.AgentRunNotStartedError):
                    orchestrator._spawn_agent_run(
                        entry="agent-run", repo_root=Path(temporary),
                        plan_pr="https://github.com/o/r/pull/26",
                        work_order_path=".agents/work-orders/WO-X-001-r1.json",
                        model="m", base_url="https://api.example", effort="max",
                        auth_config=Path("/nowhere"), timeout_seconds=30, proxy=None,
                        stdout_path=stdout, identity=identity,
                        store=FakeStore(), task_key_value="k", gate_path=gate_path,
                    )
            popen.assert_not_called()

    def test_execute_plan_passes_its_gate_exemption_to_the_first_availability_check(self) -> None:
        from agent_delivery_loop.store import RunStore

        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/o/r.git"], check=True)
            config = ClaudeConfig(
                executable="claude", model="m", base_url="https://api.example",
                provider_host="api.example", auth_name="ANTHROPIC_AUTH_TOKEN",
                auth_value="x", effort="max", target_repo_root=repo.resolve(),
            )
            with patch("agent_delivery_loop.runner.default_state_dir", return_value=base / "state"):
                store = RunStore(base / "state")
                gate = store.record_path("k", "spawn-abcd1234")
                store.write("k", "spawn-abcd1234", orchestrator._spawn_gate_record("abcd1234-full", "started"))

                class Sentinel(AgentDeliveryError):
                    pass

                def fake_client(repo_arg, token=None):  # noqa: ANN001
                    raise Sentinel("stop")

                with patch("agent_delivery_loop.runner.GitHubClient", side_effect=fake_client), \
                     patch("agent_delivery_loop.runner.preflight", return_value=(0, 1, 0)):
                    # With the gate present, a runner WITHOUT its exemption is blocked;
                    # passing the gate name must carry the exemption past the FIRST check.
                    with self.assertRaises(Sentinel):
                        runner.execute_plan(
                            repo_path=repo, plan_pr="o/r#26",
                            work_order_path=".agents/work-orders/WO-X-001-r1.json",
                            worker_config=config, pat_identity=_identity(),
                            spawn_gate_name=f"k/{gate.name}",
                        )
                    # An invalid or missing gate name fails closed instead.
                    with self.assertRaises(AgentDeliveryError) as caught:
                        runner.execute_plan(
                            repo_path=repo, plan_pr="o/r#26",
                            work_order_path=".agents/work-orders/WO-X-001-r1.json",
                            worker_config=config, pat_identity=_identity(),
                            spawn_gate_name="k/spawn-not-there.json",
                        )
                    self.assertIn("spawn gate is invalid or missing", str(caught.exception))

    def test_sigint_ignoring_same_group_descendant_is_hard_stopped(self) -> None:
        import signal as signal_module

        outer = subprocess.Popen(
            [
                "python3", "-c",
                "import subprocess,time\n"
                "subprocess.Popen(['python3','-c',"
                "'import signal,time\\nsignal.signal(signal.SIGINT, signal.SIG_IGN)\\ntime.sleep(120)'])\n"
                "print('ready',flush=True)\n"
                "time.sleep(120)\n",
            ],
            start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        try:
            self.assertEqual(outer.stdout.readline().strip(), "ready")
            confirmed = orchestrator._stop_agent_run_bounded(outer)
            self.assertTrue(confirmed)
            self.assertIsNotNone(outer.poll())
            # The whole outer group (including the SIGINT-ignoring descendant) is gone.
            with self.assertRaises(OSError):
                os.killpg(outer.pid, 0)
        finally:
            outer.kill()
            try:
                os.killpg(outer.pid, signal_module.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            outer.wait(timeout=10)


class CiRemainingBudgetTests(unittest.TestCase):
    def test_wait_for_ci_passes_exact_remaining_floats_to_reads(self) -> None:
        identity = _identity()
        captured: list[float] = []

        def fake_gh_json(args, *, identity=None, proxy=None, timeout=0):  # noqa: ANN001
            captured.append(float(timeout))
            return {"headRefOid": "a" * 40, "baseRefName": "main"}

        def fake_snapshot(identity_arg, *, pr_number, head_sha, proxy, request_timeout):  # noqa: ANN001
            captured.append(float(request_timeout()))
            return {
                "status": "completed", "conclusion": "success",
                "workflow_run_id": 1, "run_attempt": 1,
                "checks": [("validate", "SUCCESS")],
            }

        with patch("agent_delivery_loop.publish.gh_json", side_effect=fake_gh_json), \
             patch("agent_delivery_loop.publish._ci_snapshot", side_effect=fake_snapshot):
            result = publish.wait_for_ci(
                repo_slug="o/r", pr_number=1, head_sha="a" * 40,
                timeout_seconds=2, identity=identity,
            )
        self.assertEqual(result["status"], "completed")
        # Exact floats, never floored to 1: with ~2s left, reads get ~2s budgets.
        self.assertTrue(all(value > 1.5 for value in captured), msg=str(captured))
        self.assertTrue(all(value <= 2.0 for value in captured), msg=str(captured))


class TwoLayerStopSeparationTests(unittest.TestCase):
    """Real local subprocesses, no credentials and no models: prove that stopping the
    outer Python process group is possible while an inner, separately-sessioned child
    stays alive — exactly why outer-stop can never prove the inner Worker stopped."""

    OUTER = (
        "import subprocess,sys,time\n"
        "inner=subprocess.Popen(['sleep','120'],start_new_session=True)\n"
        "print(inner.pid,flush=True)\n"
        "time.sleep(120)\n"
    )

    def test_sigint_first_stop_confirms_outer_but_not_the_inner_group(self) -> None:
        import signal as signal_module

        outer = subprocess.Popen(
            ["python3", "-c", self.OUTER], start_new_session=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        try:
            inner_pid = int(outer.stdout.readline().strip())
            confirmed = orchestrator._stop_agent_run_bounded(outer)
            self.assertTrue(confirmed)  # outer group confirmed stopped
            self.assertIsNotNone(outer.poll())
            # The inner group is a separate session: it must still be alive, proving
            # that an outer-stop confirmation says nothing about the inner program.
            os.kill(inner_pid, 0)
        finally:
            outer.kill()
            try:
                os.killpg(inner_pid, signal_module.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            outer.wait(timeout=10)


class SpawnGateTests(unittest.TestCase):
    def _store(self, base: Path):
        from agent_delivery_loop.store import RunStore

        return RunStore(base / "state")

    def test_stale_safe_evidence_cannot_release_a_newer_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            key = "k"
            # A safe record from an EARLIER launch — even with a FUTURE mtime (clock
            # skew), its runner-written started_at predates this gate's registration.
            store.write(key, "11111111-1111-1111-1111-111111111111", {
                "schema_version": 1, "run_id": "11111111-1111-1111-1111-111111111111",
                "worker_status": "stopped", "status": "local_ready",
                "started_at": "2020-01-01T00:00:00+00:00",
            })
            old = store.record_path(key, "11111111-1111-1111-1111-111111111111")
            # ...then THIS spawn's gate flips, plus an unconfirmed current record.
            gate = store.record_path(key, "spawn-abcd1234")
            store.write(key, "spawn-abcd1234", orchestrator._spawn_gate_record("abcd1234-full", "started"))
            store.write(key, "22222222-2222-2222-2222-222222222222", {
                "schema_version": 1, "run_id": "22222222-2222-2222-2222-222222222222",
                "worker_status": "stop_unconfirmed", "status": "cleanup_failed",
            })
            import os as _os

            future = _os.stat(old).st_mtime + 3600.0
            _os.utime(old, (future, future))
            self.assertFalse(orchestrator._resolve_spawn_gate(
                store, key, gate, gate.stat().st_mtime - 5.0,
            ))
            self.assertTrue(gate.exists())

    def test_started_gate_actually_blocks_until_trusted_evidence_resolves_it(self) -> None:
        from agent_delivery_loop.store import RunStateError

        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            key = "k"
            gate = store.record_path(key, "spawn-abcd1234")
            store.write(key, "spawn-abcd1234", orchestrator._spawn_gate_record("abcd1234-full", "started"))
            registered = gate.stat().st_mtime
            # Real blocking: every launch check refuses while the gate is unresolved.
            with self.assertRaises(RunStateError):
                store.assert_worker_available()
            with self.assertRaises(AgentDeliveryError):
                orchestrator._preflight_spawn_gates(store)
            self.assertFalse(orchestrator._resolve_spawn_gate(store, key, gate, registered))
            self.assertTrue(gate.exists())
            with self.assertRaises(RunStateError):
                store.assert_worker_available()
            # Trusted evidence from THIS window: a stopped-safe run record whose
            # runner-written started_at is at or after the gate's registration.
            gate_registered_at = json.loads(gate.read_text(encoding="utf-8"))["registered_at"]
            store.write(key, "11111111-1111-1111-1111-111111111111", {
                "schema_version": 1, "run_id": "11111111-1111-1111-1111-111111111111",
                "worker_status": "stopped", "status": "local_ready",
                "started_at": gate_registered_at,
            })
            self.assertTrue(orchestrator._resolve_spawn_gate(store, key, gate, registered))
            self.assertFalse(gate.exists())
            store.assert_worker_available()  # unblocked only now

    def test_tracked_runner_may_exempt_exactly_its_own_gate(self) -> None:
        from agent_delivery_loop.store import RunStateError

        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            gate = store.record_path("k", "spawn-abcd1234")
            store.write("k", "spawn-abcd1234", orchestrator._spawn_gate_record("abcd1234-full", "started"))
            # The tracked child may start (its own gate is exempted) ...
            store.assert_worker_available(exempt_record=gate)
            # ... while every other check keeps blocking.
            with self.assertRaises(RunStateError):
                store.assert_worker_available()

    def test_pending_gate_is_nonblocking_and_preflight_still_refuses_new_spawns(self) -> None:
        from agent_delivery_loop.store import RunStateError

        with tempfile.TemporaryDirectory() as temporary:
            store = self._store(Path(temporary))
            store.write("k", "spawn-abcd1234", orchestrator._spawn_gate_record("abcd1234-full", "pending"))
            # Pre-start phase is truthful and non-blocking for ordinary safety scans ...
            store.assert_worker_available()
            # ... but the orchestrator's spawn preflight still refuses a new spawn.
            with self.assertRaises(AgentDeliveryError):
                orchestrator._preflight_spawn_gates(store)


class StrictBudgetTests(unittest.TestCase):
    def setUp(self) -> None:
        sleeper = patch("agent_delivery_loop.publish.time.sleep", lambda _s: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_exhausted_or_subsecond_budget_starts_no_request(self) -> None:
        identity = _identity()
        for timeout in (0.0, 0.5, 0.999):
            with patch("agent_delivery_loop.publish.run_gh") as raw:
                with self.assertRaises(AgentDeliveryError) as caught:
                    publish.run_gh_read(["api", "user"], identity=identity, timeout=timeout)
            self.assertEqual(raw.call_count, 0)
            self.assertIn("was not started", str(caught.exception))

    def test_per_attempt_timeout_never_exceeds_the_remaining_budget(self) -> None:
        identity = _identity()
        captured: list[float] = []

        def fake_run_gh(args, *, identity=None, proxy=None, timeout=0):  # noqa: ANN001
            captured.append(float(timeout))
            return _completed(1, stderr="net/http: TLS handshake timeout")

        with patch("agent_delivery_loop.publish.run_gh", side_effect=fake_run_gh):
            with self.assertRaises(AgentDeliveryError):
                publish.run_gh_read(["api", "user"], identity=identity, timeout=3.5)
        self.assertTrue(1 <= len(captured) <= 3)
        self.assertLessEqual(max(captured), 3.5)
        # Attempts never get a longer timeout than the budget that is left,
        # and never more than the single-call ceiling.
        self.assertLessEqual(max(captured), 3.5 + 1e-9)
        for value in captured:
            self.assertGreater(value, 0.0)


if __name__ == "__main__":
    unittest.main()
