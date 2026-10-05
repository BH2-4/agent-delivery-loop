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
