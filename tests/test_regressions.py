from __future__ import annotations

import json
import os
import sys
import time
import fcntl
import subprocess
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stderr
from io import StringIO
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from agent_delivery_loop.claude_worker import (
    ClaudeConfig,
    WorkerCancelled,
    WorkerResultError,
    _process_group_exists,
    _validated_outcome,
    run_claude,
)
from agent_delivery_loop.cli import _report_error
from agent_delivery_loop.errors import AgentDeliveryError
from agent_delivery_loop.github import GitHubClient, Repo
from agent_delivery_loop.git_ops import commit_changes, stage_changes
from agent_delivery_loop.runner import _record_cancelled_run, _safe_changed_paths
from agent_delivery_loop.store import RunStore, task_key
from agent_delivery_loop.work_order import parse_work_order


class WorkerCompletionContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ClaudeConfig(
            executable="claude",
            model="fixed-model",
            base_url="https://api.example.invalid",
            provider_host="api.example.invalid",
            auth_name="ANTHROPIC_AUTH_TOKEN",
            auth_value="test-secret-token-value",
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


class WorkerCancellationTests(unittest.TestCase):
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
                "run_id": run_id,
                "status": "starting",
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


if __name__ == "__main__":
    unittest.main()
