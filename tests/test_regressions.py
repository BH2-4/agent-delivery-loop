from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from agent_delivery_loop.claude_worker import ClaudeConfig, WorkerResultError, _validated_outcome
from agent_delivery_loop.errors import AgentDeliveryError
from agent_delivery_loop.github import GitHubClient, Repo
from agent_delivery_loop.git_ops import commit_changes, stage_changes
from agent_delivery_loop.runner import _safe_changed_paths


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
                "result": json.dumps(report),
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
            paths = stage_changes(repo)
            self.assertEqual(paths, ["docs/delivered.md"])
            commit_changes(repo, "WO-2026-001-r1")
            committed = subprocess.run(
                ["git", "-C", str(repo), "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertEqual(paths, committed)


if __name__ == "__main__":
    unittest.main()
