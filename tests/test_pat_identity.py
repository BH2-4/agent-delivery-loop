"""Focused PAT-boundary regressions; no real secrets or network/model calls."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_delivery_loop.errors import AgentDeliveryError
from agent_delivery_loop.github_identity import GitHubPAT
from agent_delivery_loop.publish import run_gh, verify_github_identity

# This is intentionally not a usable credential.
FAKE_PAT = "github_pat_test_not_a_real_credential"


class PatIdentityTests(unittest.TestCase):
    def test_private_snapshot_rejects_missing_inrepo_symlink_and_broad_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            repo.mkdir()
            private = base / "private"
            private.mkdir(mode=0o700)
            token_file = private / "pat"

            def load(path: Path) -> GitHubPAT:
                return GitHubPAT.from_file(path=path, repo_root=repo, repository="o/r", expected_login="o")

            with self.assertRaises(AgentDeliveryError):
                load(token_file)
            token_file.write_text(FAKE_PAT + "\n", encoding="ascii")
            token_file.chmod(0o600)
            identity = load(token_file)
            self.assertEqual(identity.token_for("o/r"), FAKE_PAT)
            self.assertNotIn(FAKE_PAT, repr(identity))
            self.assertNotIn(FAKE_PAT, str(identity.metadata()))
            with self.assertRaises(AgentDeliveryError):
                identity.token_for("o/another")
            with self.assertRaises(AgentDeliveryError):
                load(repo / "not-created")
            link = private / "link"
            link.symlink_to(token_file)
            with self.assertRaises(AgentDeliveryError):
                load(link)
            token_file.chmod(0o644)
            with self.assertRaises(AgentDeliveryError):
                load(token_file)

    def test_gh_uses_only_explicit_pat_without_mutating_daily_environment(self) -> None:
        identity = GitHubPAT(repository="o/r", expected_login="o", _token=FAKE_PAT)
        ambient = {
            "GH_TOKEN": "old-personal", "GITHUB_TOKEN": "old-fallback",
            "GH_ENTERPRISE_TOKEN": "old-enterprise", "GITHUB_ENTERPRISE_TOKEN": "old-enterprise-2",
            "GH_DEBUG": "api", "DEBUG": "true", "GH_HOST": "wrong.invalid",
            "ANTHROPIC_AUTH_TOKEN": "worker-secret", "AGENT_GIT_PASSWORD": "old-push-secret",
        }
        with patch.dict(os.environ, ambient), patch("agent_delivery_loop.publish.subprocess.run") as command:
            command.return_value = subprocess.CompletedProcess([], 0, "{}", "")
            run_gh(["api", "user"], identity=identity)
            child = command.call_args.kwargs["env"]
            self.assertEqual(child["GH_TOKEN"], FAKE_PAT)
            self.assertEqual(child["GH_HOST"], "github.com")
            self.assertEqual(child["GH_PROMPT_DISABLED"], "1")
            for key in ambient.keys() - {"GH_TOKEN", "GH_HOST"}:
                self.assertNotIn(key, child)
            self.assertNotIn(FAKE_PAT, str(command.call_args.args))
            self.assertEqual(os.environ["GH_TOKEN"], "old-personal")

    def test_read_preflight_rejects_wrong_account_and_never_claims_write_verification(self) -> None:
        identity = GitHubPAT(repository="o/r", expected_login="o", _token=FAKE_PAT)
        with patch("agent_delivery_loop.publish.gh_json", return_value={"login": "other"}) as read:
            with self.assertRaises(AgentDeliveryError):
                verify_github_identity(identity)
            self.assertEqual(read.call_count, 1)
        with patch("agent_delivery_loop.publish.gh_json", side_effect=[
            {"login": "o"}, {"full_name": "o/r", "default_branch": "main"},
        ]):
            result = verify_github_identity(identity)
        self.assertTrue(result["repository_read_verified"])
        self.assertFalse(result["write_permissions_verified"])
        self.assertFalse(result["protection_verified"])
        self.assertNotIn(FAKE_PAT, str(result))


class PublishIdentityFlowTests(unittest.TestCase):
    """Regression: publish helpers must hand the REAL PAT object to their read
    helpers; an earlier change shadowed the identity parameter with the intent
    dict, which mocks returning plain values silently hid."""

    def test_push_and_merge_pass_the_live_pat_to_read_helpers(self) -> None:
        from agent_delivery_loop import publish
        from agent_delivery_loop.errors import AgentDeliveryError

        identity = GitHubPAT(repository="o/r", expected_login="o", _token=FAKE_PAT)
        candidate = "a" * 40

        def remote(repo_slug, branch, *, identity, proxy=None):
            identity.token_for(repo_slug)  # AttributeError if a dict is passed instead
            return candidate

        with tempfile.TemporaryDirectory() as temporary:
            worktree = Path(temporary) / "wt"
            worktree.mkdir()
            with patch.object(publish, "_push_once", side_effect=AgentDeliveryError("lost")), patch.object(
                publish, "remote_branch_sha", side_effect=remote
            ):
                result = publish.push_delivery_branch(
                    worktree=worktree, repo_slug="o/r", repo_url="https://github.com/o/r.git",
                    branch="agent/x-r1-aabbccdd", candidate_sha=candidate,
                    intent_sink=lambda i: None, identity=identity,
                )
        self.assertTrue(result["confirmed"])

        def gh_json_with_identity(args, *, identity, proxy=None):
            identity.token_for("o/r")
            if args[0] == "pr" and args[1] == "view":
                return {"state": "MERGED", "headRefOid": candidate, "mergeCommit": {"oid": "b" * 40}}
            raise AssertionError("unexpected call")

        with patch.object(publish, "gh_json", side_effect=gh_json_with_identity):
            merged = publish.merge_delivery_pr(
                repo_slug="o/r", pr_number=7, candidate_sha=candidate,
                intent_sink=lambda i: None, identity=identity,
            )
        self.assertTrue(merged["merged"])
        self.assertEqual(merged["merge_sha"], "b" * 40)


class CiAssociationAndDeadlineTests(unittest.TestCase):
    def _run_payload(self, pulls, run_id=1, conclusion="success", jobs=None):
        base = {
            "id": run_id, "run_attempt": 1, "head_sha": "a" * 40, "event": "pull_request",
            "path": ".github/workflows/ci.yml@refs/heads/main", "status": "completed",
            "conclusion": conclusion, "pull_requests": pulls, "html_url": "https://example.invalid/run/1",
        }
        return {
            "total_count": 1, "workflow_runs": [base],
        } if pulls is not None else {"total_count": 0, "workflow_runs": []}

    def _jobs_payload(self, names=(("validate", "success"),)):
        return {"total_count": len(names), "jobs": [
            {"name": name, "head_sha": "a" * 40, "run_id": 1, "status": "completed",
             "conclusion": conclusion} for name, conclusion in names
        ]}

    def test_merged_pr_run_with_empty_association_is_accepted(self) -> None:
        from agent_delivery_loop import publish

        identity = GitHubPAT(repository="o/r", expected_login="o", _token=FAKE_PAT)

        def gh(args, *, identity, proxy=None, timeout=60):
            identity.token_for("o/r")
            url = args[-1]
            if "/runs?" in url:
                return self._run_payload([])  # observed on merged PRs in this repository
            if "/jobs" in url:
                return self._jobs_payload()
            if args[-1].endswith("/runs/1"):
                return {"id": 1, "run_attempt": 1, "head_sha": "a" * 40, "status": "completed",
                        "conclusion": "success"}
            raise AssertionError(f"unexpected {url}")

        with patch.object(publish, "gh_json", side_effect=gh):
            snapshot = publish._ci_snapshot(identity, pr_number=7, head_sha="a" * 40, proxy=None)
        self.assertEqual(snapshot["workflow_run_id"], 1)
        self.assertEqual(snapshot["conclusion"], "success")

    def test_run_associated_with_a_different_pr_is_rejected_as_ambiguous(self) -> None:
        from agent_delivery_loop import publish

        identity = GitHubPAT(repository="o/r", expected_login="o", _token=FAKE_PAT)

        def gh(args, *, identity, proxy=None, timeout=60):
            identity.token_for("o/r")
            return self._run_payload([{"number": 9}])

        with patch.object(publish, "gh_json", side_effect=gh):
            snapshot = publish._ci_snapshot(identity, pr_number=7, head_sha="a" * 40, proxy=None)
        self.assertIsNone(snapshot["workflow_run_id"])

    def test_verify_ci_current_requires_unique_successful_validate(self) -> None:
        from agent_delivery_loop import publish

        identity = GitHubPAT(repository="o/r", expected_login="o", _token=FAKE_PAT)

        def gh(args, *, identity, proxy=None, timeout=60):
            identity.token_for("o/r")
            url = args[-1]
            if "/runs?" in url:
                return self._run_payload([])
            if "/jobs" in url:
                return self._jobs_payload([("validate", "success"), ("validate", "success")])
            if args[-1].endswith("/runs/1"):
                return {"id": 1, "run_attempt": 1, "head_sha": "a" * 40, "status": "completed",
                        "conclusion": "success"}
            raise AssertionError(url)

        with patch.object(publish, "gh_json", side_effect=gh):
            with self.assertRaises(AgentDeliveryError):
                publish.verify_ci_current(identity, repo_slug="o/r", pr_number=7, head_sha="a" * 40)


class ResumeIdentityBindingTests(unittest.TestCase):
    def test_resume_rejects_missing_or_mismatched_identity_metadata(self) -> None:
        from agent_delivery_loop.orchestrator import Orchestration, resume

        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"AGENT_STATE_DIR": str(Path(temporary) / "state")}):
                base = {
                    "plan_pr": "https://github.com/o/r/pull/1", "work_order_path": ".agents/work-orders/WO-X-r1.json",
                    "expected_source_sha": "f" * 40, "expected_wheel_sha256": "0" * 64,
                    "review_model": "m", "review_effort": "high", "review_timeout_seconds": 900,
                    "review_bundle_dir": "/tmp/b", "proxy": None, "ci_timeout_seconds": 900,
                    "auto_merge": True, "model": "m", "base_url": "https://x", "effort": "max",
                    "auth_config": "/tmp/a.json", "repo_path": ".", "install_receipt": "/tmp/r.json",
                }
                common = dict(
                    repo_path=Path.cwd(), plan_pr=base["plan_pr"], work_order_path=base["work_order_path"],
                    model="m", base_url="https://x", effort="max", auth_config=Path("/tmp/a.json"),
                    install_receipt=Path("/tmp/r.json"), expected_source_sha=base["expected_source_sha"],
                    expected_wheel_sha256=base["expected_wheel_sha256"], review_model="m",
                    review_effort="high", review_timeout_seconds=900,
                    review_bundle_dir=Path("/tmp/b"), proxy=None, ci_timeout_seconds=900, auto_merge=True,
                    github_pat_file=Path("/tmp/p.pat"), github_login="o",
                )
                legacy = Orchestration("orch-legacy")
                legacy.record["stage"] = "worker_completed"
                legacy.record["worker"] = {"run_id": "r", "delivery_branch": "b", "worker_commit": "a" * 40,
                                           "record_path": "/tmp/r.json"}
                legacy.record["candidate"] = {"sha": "a" * 40, "origin": "worker", "review_round": 0}
                legacy.save(parameters=dict(base))  # no github_identity key: legacy personal-gh record
                with self.assertRaises(AgentDeliveryError) as raised:
                    resume(orchestration_id="orch-legacy", **common)
                self.assertIn("identity", str(raised.exception))

                wrong = Orchestration("orch-wrong")
                wrong.record["stage"] = "worker_completed"
                wrong.record["worker"] = dict(legacy.record["worker"])
                wrong.record["candidate"] = dict(legacy.record["candidate"])
                wrong.record["github_identity"] = {"kind": "fine_grained_pat", "repository": "o/r",
                                                   "expected_login": "someone-else"}
                wrong.save(parameters=dict(base))
                with self.assertRaises(AgentDeliveryError) as raised:
                    resume(orchestration_id="orch-wrong", **common)
                self.assertIn("identity", str(raised.exception))


class VerifyCiCurrentSuccessTests(unittest.TestCase):
    def test_latest_attempt_is_selected_and_success_path_returns_bindings(self) -> None:
        from agent_delivery_loop import publish

        identity = GitHubPAT(repository="o/r", expected_login="o", _token=FAKE_PAT)

        def gh(args, *, identity, proxy=None, timeout=60):
            identity.token_for("o/r")
            url = args[-1]
            if "/runs?" in url:
                return {"total_count": 2, "workflow_runs": [
                    {"id": 5, "run_attempt": 1, "head_sha": "a" * 40, "event": "pull_request",
                     "path": ".github/workflows/ci.yml@refs/heads/main", "pull_requests": [],
                     "status": "completed", "conclusion": "success", "html_url": "u5"},
                    {"id": 6, "run_attempt": 2, "head_sha": "a" * 40, "event": "pull_request",
                     "path": ".github/workflows/ci.yml@refs/heads/main", "pull_requests": [],
                     "status": "completed", "conclusion": "success", "html_url": "u6"},
                ]}
            if "/attempts/2/jobs" in url:
                return {"total_count": 1, "jobs": [
                    {"name": "validate", "head_sha": "a" * 40, "run_id": 6, "status": "completed",
                     "conclusion": "success"},
                ]}
            if url.endswith("/actions/runs/6"):
                return {"id": 6, "run_attempt": 2, "head_sha": "a" * 40, "status": "completed",
                        "conclusion": "success"}
            raise AssertionError(url)

        with patch.object(publish, "gh_json", side_effect=gh):
            result = publish.verify_ci_current(identity, repo_slug="o/r", pr_number=7, head_sha="a" * 40)
        self.assertEqual(result["workflow_run_id"], 6)  # max by (id, attempt) selects the newest run
        self.assertEqual(result["run_attempt"], 2)
        self.assertEqual(result["checks"], [("validate", "SUCCESS")])
