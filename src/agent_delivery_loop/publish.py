"""Intent-persisted GitHub write operations with read-only result reconciliation.

Every external write persists its intent and full target identity first; if the
checkpoint cannot be saved the write never runs. A lost or ambiguous response is
never treated as "did not happen" and never blindly replayed: the real remote
state is queried read-only and either accepted or escalated to a stop.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .errors import AgentDeliveryError
from .git_ops import git

SHA_RE = re.compile(r"[0-9a-f]{40}")
GH_TIMEOUT_SECONDS = 60
CI_POLL_SECONDS = 20
REQUIRED_CHECK_NAME = "validate"
CREDENTIAL_ENV_NAMES = (
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GIT_ASKPASS",
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY",
)

IntentSink = Callable[[dict[str, Any]], None]


class WriteReconciliationError(AgentDeliveryError):
    """A write's real remote result could not be confirmed; nothing may be repeated."""


def _proxy_env(proxy: str | None) -> dict[str, str]:
    if not proxy:
        return {}
    return {
        "ALL_PROXY": proxy, "all_proxy": proxy,
        "HTTPS_PROXY": proxy, "https_proxy": proxy,
        "HTTP_PROXY": proxy, "http_proxy": proxy,
    }


def run_gh(args: list[str], *, proxy: str | None = None, timeout: int = GH_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("ALL_PROXY", None)
    env.pop("all_proxy", None)
    env.update(_proxy_env(proxy))
    try:
        return subprocess.run(
            ["gh", *args], text=True, capture_output=True, timeout=timeout, check=False,
            env=env, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentDeliveryError(f"A gh command could not run or timed out: gh {args[0]}.") from exc


def gh_json(args: list[str], *, proxy: str | None = None) -> Any:
    result = run_gh(args, proxy=proxy)
    if result.returncode != 0:
        raise AgentDeliveryError(f"gh {' '.join(args[:2])} failed with exit code {result.returncode}; no state was assumed.")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise AgentDeliveryError("gh returned output that is not JSON.") from exc


def remote_branch_sha(repo_slug: str, branch: str, *, proxy: str | None = None) -> str | None:
    """Read the real remote branch head; None only on a definitive 404."""
    result = run_gh(["api", f"repos/{repo_slug}/git/ref/heads/{branch}", "--jq", ".object.sha"], proxy=proxy)
    if result.returncode != 0:
        stderr = result.stderr or ""
        if "Not Found" in stderr or "404" in stderr:
            return None
        raise AgentDeliveryError(f"Remote branch state for {branch} could not be read; refusing to assume anything.")
    sha = result.stdout.strip()
    if not SHA_RE.fullmatch(sha):
        raise AgentDeliveryError(f"Remote branch state for {branch} was not a full SHA; refusing to assume anything.")
    return sha


def _push_once(worktree: Path, repo_url: str, branch: str, token: str, proxy: str | None) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory(prefix="adl-askpass-") as temporary:
        askpass = Path(temporary) / "askpass"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in *Username*) printf \'%s\\n\' "x-access-token" ;;'
            ' *Password*) printf \'%s\\n\' "$AGENT_GIT_PASSWORD" ;; *) exit 1 ;; esac\n',
            encoding="utf-8",
        )
        askpass.chmod(0o700)
        env = {key: value for key, value in os.environ.items() if key not in CREDENTIAL_ENV_NAMES}
        env.update({"GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0", "AGENT_GIT_PASSWORD": token})
        if proxy:
            env.update(_proxy_env(proxy))
        return git(worktree, "-c", "credential.helper=", "push", repo_url, f"HEAD:refs/heads/{branch}", env=env, check=False)


def push_delivery_branch(
    *, worktree: Path, repo_slug: str, repo_url: str, branch: str, candidate_sha: str,
    intent_sink: IntentSink, token_provider: Callable[[], str], proxy: str | None = None,
) -> dict[str, Any]:
    identity = {
        "operation": "push", "repository": repo_slug, "branch": branch,
        "base_branch": "main", "candidate_sha": candidate_sha,
    }
    intent_sink({**identity, "state": "intent"})
    for attempt in range(2):
        try:
            result = _push_once(worktree, repo_url, branch, token_provider(), proxy)
        except AgentDeliveryError:
            result = None
        remote = remote_branch_sha(repo_slug, branch, proxy=proxy)
        if remote == candidate_sha:
            intent_sink({**identity, "state": "confirmed", "attempts": attempt + 1})
            return {"operation": "push", "branch": branch, "head": remote, "confirmed": True}
        if remote is None:
            if result is not None and result.returncode == 0:
                raise WriteReconciliationError(
                    "Push reported success but the remote branch is absent; the real state could not be confirmed."
                )
            if attempt == 0:
                continue  # Definitively absent: one bounded re-push is duplicate-safe.
            raise WriteReconciliationError("The delivery branch could not be pushed and is still absent remotely.")
        raise WriteReconciliationError(
            f"Remote branch {branch} points at {remote}, not the reviewed {candidate_sha}; refusing to overwrite anything."
        )
    raise WriteReconciliationError("Push reconciliation exhausted its bounded attempts.")


def ensure_delivery_pr(
    *, repo_slug: str, branch: str, candidate_sha: str, title: str, body_file: Path,
    intent_sink: IntentSink, proxy: str | None = None,
) -> dict[str, Any]:
    identity = {
        "operation": "create_pr", "repository": repo_slug, "branch": branch,
        "base_branch": "main", "candidate_sha": candidate_sha,
    }

    def existing() -> dict[str, Any] | None:
        pulls = gh_json(
            ["pr", "list", "--repo", repo_slug, "--head", branch, "--base", "main", "--state", "open",
             "--json", "number,url,headRefOid"], proxy=proxy,
        )
        if not isinstance(pulls, list):
            raise AgentDeliveryError("Delivery PR lookup did not return a list.")
        if len(pulls) > 1:
            raise WriteReconciliationError(f"Multiple open Delivery PRs match branch {branch}; inspect GitHub before continuing.")
        if not pulls:
            return None
        pull = pulls[0]
        if pull.get("headRefOid") != candidate_sha:
            raise WriteReconciliationError(
                f"The matching Delivery PR head is {pull.get('headRefOid')}, not the reviewed {candidate_sha}."
            )
        return {"number": pull["number"], "url": pull["url"], "reused": True}

    found = existing()
    if found is not None:
        intent_sink({**identity, "state": "confirmed", "reused": True})
        return found
    intent_sink({**identity, "state": "intent"})
    created_error: AgentDeliveryError | None = None
    try:
        created = run_gh(
            ["pr", "create", "--repo", repo_slug, "--head", branch, "--base", "main",
             "--title", title, "--body-file", str(body_file)], proxy=proxy,
        )
    except AgentDeliveryError as exc:
        created = None  # A timeout may still have created the PR; reconcile below.
        created_error = exc
    if created is not None and created.returncode == 0:
        match = re.search(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/pull/[0-9]+", created.stdout)
        if match:
            found = existing()
            if found is not None:
                intent_sink({**identity, "state": "confirmed", "reused": False})
                return found
    # Ambiguous, failed, or timed-out creation: reconcile read-only; never create a second time blind.
    found = existing()
    if found is not None:
        intent_sink({**identity, "state": "confirmed_after_reconcile", "reused": True})
        return found
    raise WriteReconciliationError(
        "Delivery PR creation result could not be confirmed by a read-only lookup; inspect GitHub before retrying."
        + (f" (last error: {created_error})" if created_error else "")
    )


def merge_delivery_pr(
    *, repo_slug: str, pr_number: int, candidate_sha: str, intent_sink: IntentSink, proxy: str | None = None,
) -> dict[str, Any]:
    identity = {
        "operation": "merge_pr", "repository": repo_slug, "pr_number": pr_number,
        "branch_target": "main", "candidate_sha": candidate_sha,
    }
    intent_sink({**identity, "state": "intent"})
    for attempt in range(2):
        detail = gh_json(
            ["pr", "view", str(pr_number), "--repo", repo_slug, "--json", "state,mergeCommit,headRefOid"], proxy=proxy
        )
        if not isinstance(detail, dict):
            raise WriteReconciliationError("Delivery PR state could not be read before merging.")
        if detail.get("state") == "MERGED":
            merge_commit = detail.get("mergeCommit")
            if detail.get("headRefOid") != candidate_sha:
                raise WriteReconciliationError(
                    f"The merged Delivery PR head is {detail.get('headRefOid')}, not the reviewed {candidate_sha}; "
                    "the real result does not match the approved merge."
                )
            intent_sink({**identity, "state": "confirmed", "already_merged": attempt > 0 or None})
            return {
                "merged": True, "merge_sha": merge_commit.get("oid") if isinstance(merge_commit, dict) else None,
            }
        if detail.get("state") != "OPEN" or detail.get("headRefOid") != candidate_sha:
            raise WriteReconciliationError(
                f"Delivery PR is {detail.get('state')} at head {detail.get('headRefOid')}; "
                f"merge is bound to OPEN at {candidate_sha} and was not executed."
            )
        try:
            run_gh(["pr", "merge", str(pr_number), "--repo", repo_slug, "--merge"], proxy=proxy, timeout=120)
        except AgentDeliveryError:
            pass  # A lost merge response may still have merged the PR; the loop re-reads state.
    # Final read-only confirmation after the last bounded merge attempt.
    detail = gh_json(
        ["pr", "view", str(pr_number), "--repo", repo_slug, "--json", "state,mergeCommit,headRefOid"], proxy=proxy
    )
    if isinstance(detail, dict) and detail.get("state") == "MERGED":
        if detail.get("headRefOid") != candidate_sha:
            raise WriteReconciliationError(
                f"The merged Delivery PR head is {detail.get('headRefOid')}, not the reviewed {candidate_sha}."
            )
        merge_commit = detail.get("mergeCommit")
        intent_sink({**identity, "state": "confirmed_after_reconcile"})
        return {
            "merged": True, "merge_sha": merge_commit.get("oid") if isinstance(merge_commit, dict) else None,
        }
    raise WriteReconciliationError("Merge could not be confirmed after bounded attempts; inspect GitHub.")


def wait_for_ci(
    *, repo_slug: str, pr_number: int, head_sha: str, timeout_seconds: int, proxy: str | None = None,
) -> dict[str, Any]:
    """Poll real gh check data; missing, pending-forever, cancelled, or failing checks never pass."""
    deadline = time.monotonic() + timeout_seconds
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        checks = gh_json(["pr", "checks", str(pr_number), "--repo", repo_slug, "--json", "name,state,link"], proxy=proxy)
        if not isinstance(checks, list):
            raise AgentDeliveryError("gh pr checks did not return a check list.")
        last = [item for item in checks if isinstance(item, dict)]
        states = {str(item.get("state", "")).upper() for item in last}
        if last and states == {"SUCCESS"}:
            required = [item for item in last if item.get("name") == REQUIRED_CHECK_NAME]
            if len(required) != 1:
                raise AgentDeliveryError(f"The required '{REQUIRED_CHECK_NAME}' check is missing from the PR checks.")
            current = gh_json(["pr", "view", str(pr_number), "--repo", repo_slug, "--json", "headRefOid"], proxy=proxy)
            if not isinstance(current, dict) or current.get("headRefOid") != head_sha:
                raise AgentDeliveryError("PR head changed while waiting for CI; the checked version is no longer current.")
            return {
                "checks": sorted((item.get("name"), str(item.get("state")).upper()) for item in last),
                "links": [item.get("link") for item in last],
            }
        if last and states & {"FAILURE", "CANCELLED", "TIMED_OUT", "SKIPPED"}:
            failed = [(item.get("name"), str(item.get("state")).upper()) for item in last]
            raise AgentDeliveryError(f"CI reported a non-success terminal state: {failed}.")
        time.sleep(CI_POLL_SECONDS)
    raise AgentDeliveryError(f"CI did not reach a terminal state within {timeout_seconds} seconds; nothing was merged.")
