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
from urllib.parse import urlencode

from .errors import AgentDeliveryError
from .git_ops import git
from .github_identity import GitHubPAT

SHA_RE = re.compile(r"[0-9a-f]{40}")
GH_TIMEOUT_SECONDS = 60
CI_POLL_SECONDS = 20
REQUIRED_CHECK_NAME = "validate"
REQUIRED_WORKFLOW = "ci.yml"
CI_PAGE_LIMIT = 100
CREDENTIAL_ENV_NAMES = (
    "GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GIT_ASKPASS",
    "GITHUB_ENTERPRISE_TOKEN", "AGENT_GIT_PASSWORD", "SSH_ASKPASS",
    "AGENT_DELIVERY_PAT", "AGENT_DELIVERY_PAT_LOGIN",
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY",
)

# Bounded transient-retry policy for identified read-only gh invocations. The budget is
# monotonic-clock and shared by every attempt of one call; it never extends an outer
# CI deadline, and non-read-only invocations (writes) always stay single-attempt.
GH_READ_RETRY_DELAYS_SECONDS = (2.0, 8.0)
GH_READ_BUDGET_SECONDS = 45.0
_GH_WRITE_SHAPES = ("-X", "--method", "-f", "--field", "-F", "--raw-field", "--input")
_GH_TRANSIENT_PATTERNS = (
    "tls handshake timeout", "connection reset", "connection refused", "connection closed",
    "i/o timeout", "net/http", "dial tcp", "context deadline", "unexpected eof",
    "proxy error", "server error", "http 500", "http 502", "http 503", "http 504", "http 5",
)
_GH_RATE_LIMIT_MARKERS = ("rate limit", "ratelimit", "http 429", "too many requests")

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


def run_gh(
    args: list[str], *, identity: GitHubPAT,
    proxy: str | None = None, timeout: int = GH_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    env = {key: value for key, value in os.environ.items() if key not in CREDENTIAL_ENV_NAMES}
    for key in ("GH_DEBUG", "DEBUG", "GH_HOST", "GH_REPO", "GH_CONFIG_DIR"):
        env.pop(key, None)
    env.pop("ALL_PROXY", None)
    env.pop("all_proxy", None)
    env.update(_proxy_env(proxy))
    try:
        # Isolate gh's configuration without overwriting the user's daily login.
        with tempfile.TemporaryDirectory(prefix="adl-gh-config-") as config:
            env.update({
                "GH_TOKEN": identity.token_for(identity.repository),
                "GH_HOST": "github.com", "GH_CONFIG_DIR": config,
                "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "GH_PAGER": "cat",
            })
            return subprocess.run(
                ["gh", *args], text=True, capture_output=True, timeout=timeout, check=False,
                env=env, stdin=subprocess.DEVNULL,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentDeliveryError(f"A gh command could not run or timed out: gh {args[0]}.") from exc


def gh_json(
    args: list[str], *, identity: GitHubPAT, proxy: str | None = None,
    timeout: int = GH_TIMEOUT_SECONDS,
) -> Any:
    result = run_gh_read(args, identity=identity, proxy=proxy, timeout=timeout)
    if result.returncode != 0:
        raise AgentDeliveryError(f"gh {' '.join(args[:2])} failed with exit code {result.returncode}; no state was assumed.")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise AgentDeliveryError("gh returned output that is not JSON.") from exc


def _is_read_only_gh_args(args: list[str]) -> bool:
    """Only invocations whose actual HTTP semantics are provably GET may be retried."""
    if not args:
        return False
    if args[0] == "api":
        return not any(flag in args for flag in _GH_WRITE_SHAPES)
    return args[:2] == ["pr", "view"]


def _classify_gh_failure(stderr: str) -> str:
    lowered = (stderr or "").lower()
    if any(marker in lowered for marker in _GH_RATE_LIMIT_MARKERS):
        return "rate_limited"
    if any(pattern in lowered for pattern in _GH_TRANSIENT_PATTERNS):
        return "transient_network"
    return "not_transient"


def run_gh_read(
    args: list[str], *, identity: GitHubPAT, proxy: str | None = None,
    timeout: int = GH_TIMEOUT_SECONDS, return_hard_failures: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a provably read-only gh invocation with bounded transient-network retries.

    Writes and ambiguous shapes stay single-attempt by construction: callers that must not
    be retried keep using run_gh directly. Rate limiting, authentication/permission denials
    and unrecognised failures stop immediately; only transient transport failures retry,
    inside one monotonic-clock budget shared by all attempts and bounded by `timeout`.
    `return_hard_failures` lets a caller that must inspect definitive non-transient
    statuses itself (e.g. 404 handling) receive the raw result instead of an exception.
    """
    if not _is_read_only_gh_args(args):
        return run_gh(args, identity=identity, proxy=proxy, timeout=timeout)
    budget = min(float(timeout), GH_READ_BUDGET_SECONDS)
    deadline = time.monotonic() + budget
    attempts = 0
    result: subprocess.CompletedProcess[str] | None = None
    while attempts < len(GH_READ_RETRY_DELAYS_SECONDS) + 1:
        attempts += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0 and attempts > 1:
            break
        result = run_gh(args, identity=identity, proxy=proxy, timeout=max(1, int(remaining)) if remaining > 0 else timeout)
        if result.returncode == 0:
            return result
        category = _classify_gh_failure(result.stderr or "")
        if category != "transient_network" or attempts > len(GH_READ_RETRY_DELAYS_SECONDS):
            break
        delay = min(GH_READ_RETRY_DELAYS_SECONDS[attempts - 1], max(0.0, deadline - time.monotonic()))
        if delay <= 0:
            break
        time.sleep(delay)
    assert result is not None
    if result.returncode != 0:
        category = _classify_gh_failure(result.stderr or "")
        if return_hard_failures and category != "transient_network":
            return result
        detail = {
            "rate_limited": " (rate limited; not retried — see reset info in gh output)",
            "transient_network": f" (transient network failure persisted after {attempts} bounded attempt(s))",
            "not_transient": "",
        }[category]
        raise AgentDeliveryError(
            f"gh {' '.join(args[:2])} failed with exit code {result.returncode}{detail}; no state was assumed."
        )
    return result


def verify_github_identity(identity: GitHubPAT, *, proxy: str | None = None) -> dict[str, Any]:
    """Read-only account and repository checks, not proof of token write scope."""
    account = gh_json(["api", "user"], identity=identity, proxy=proxy)
    if not isinstance(account, dict) or str(account.get("login", "")).casefold() != identity.expected_login.casefold():
        raise AgentDeliveryError("PAT account does not match the explicitly expected GitHub login.")
    repo = gh_json(["api", f"repos/{identity.repository}"], identity=identity, proxy=proxy)
    if (
        not isinstance(repo, dict) or str(repo.get("full_name", "")).casefold() != identity.repository.casefold()
        or repo.get("default_branch") != "main"
    ):
        raise AgentDeliveryError("PAT repository lookup did not confirm the target repository and main branch.")
    return {
        **identity.metadata(), "account_read_verified": True, "repository_read_verified": True,
        "write_permissions_verified": False, "protection_verified": False,
    }


def probe_github_access(identity: GitHubPAT, *, pr_number: int, proxy: str | None = None) -> dict[str, Any]:
    """Probe actual PR and Actions REST reads once; never publish, wait, or merge."""
    result = verify_github_identity(identity, proxy=proxy)
    pull = gh_json(
        ["pr", "view", str(pr_number), "--repo", identity.repository, "--json", "headRefOid,baseRefName"],
        identity=identity, proxy=proxy,
    )
    if not isinstance(pull, dict) or not SHA_RE.fullmatch(str(pull.get("headRefOid", ""))) or pull.get("baseRefName") != "main":
        raise AgentDeliveryError("PAT PR probe did not return a full head SHA targeting main.")
    snapshot = _ci_snapshot(identity, pr_number=pr_number, head_sha=pull["headRefOid"], proxy=proxy)
    if snapshot["workflow_run_id"] is None or not snapshot["checks"]:
        raise AgentDeliveryError("PAT CI probe needs an existing PR with a matching workflow run and readable jobs.")
    return {
        **result, "status": "read_access_verified", "pr_number": pr_number,
        "head_sha": pull["headRefOid"], "pr_read_verified": True, "ci_read_verified": True,
        "ci_check_count": len(snapshot["checks"]), "ci_source": "github_actions_rest",
        "workflow_run_id": snapshot["workflow_run_id"],
        "delivery_verified": False,
    }


def _bounded_items(payload: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise AgentDeliveryError("Actions REST returned an invalid list; no CI state was assumed.")
    items = payload[key]
    count = payload.get("total_count")
    if type(count) is not int or count < 0 or count > CI_PAGE_LIMIT or len(items) != count:
        raise AgentDeliveryError("Actions REST list is incomplete or exceeds the bounded scan; no CI state was assumed.")
    if any(not isinstance(item, dict) for item in items):
        raise AgentDeliveryError("Actions REST returned an invalid item; no CI state was assumed.")
    return items


def _ci_snapshot(
    identity: GitHubPAT, *, pr_number: int, head_sha: str, proxy: str | None,
    request_timeout=None,
) -> dict[str, Any]:
    def per_call() -> int:
        return request_timeout() if callable(request_timeout) else GH_TIMEOUT_SECONDS
    """Read the fixed workflow's newest run for this exact PR/head, then its latest jobs."""
    query = urlencode({"event": "pull_request", "head_sha": head_sha, "per_page": CI_PAGE_LIMIT})
    runs = _bounded_items(gh_json(
        ["api", f"repos/{identity.repository}/actions/workflows/{REQUIRED_WORKFLOW}/runs?{query}"],
        identity=identity, proxy=proxy, timeout=per_call(),
    ), "workflow_runs")
    matches = []
    for run in runs:
        pulls = run.get("pull_requests")
        if not isinstance(pulls, list) or any(not isinstance(pull, dict) for pull in pulls):
            raise AgentDeliveryError("Actions run lacks explicit PR association; no CI state was assumed.")
        # Real-world contract: merged pull_request runs may carry an EMPTY association
        # list (observed on this repository). head_sha already binds the run to this
        # PR's exact candidate commit, so an empty list is accepted; a run explicitly
        # associated with a different PR is excluded from matching.
        numbers = {pull.get("number") for pull in pulls}
        if numbers and pr_number not in numbers:
            continue
        if (
            run.get("head_sha") != head_sha or run.get("event") != "pull_request"
            or str(run.get("path", "")).split("@", 1)[0] != f".github/workflows/{REQUIRED_WORKFLOW}"
            or type(run.get("id")) is not int or run["id"] < 1
            or type(run.get("run_attempt")) is not int or run["run_attempt"] < 1
        ):
            raise AgentDeliveryError("Actions run does not match the fixed PR/head/workflow identity.")
        matches.append(run)
    if not matches:
        return {"workflow_run_id": None, "checks": [], "links": [], "status": "missing", "conclusion": None}
    run = max(matches, key=lambda item: (item["id"], item["run_attempt"]))
    jobs = _bounded_items(gh_json(
        ["api", f"repos/{identity.repository}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs?per_page={CI_PAGE_LIMIT}"],
        identity=identity, proxy=proxy, timeout=per_call(),
    ), "jobs")
    checks = []
    for job in jobs:
        if (
            not isinstance(job.get("name"), str) or not job["name"]
            or job.get("head_sha") != head_sha or job.get("run_id") != run["id"]
            or job.get("status") not in {"queued", "in_progress", "completed", "waiting", "pending", "requested"}
        ):
            raise AgentDeliveryError("Actions job identity or status does not match the selected run.")
        state = str(job.get("conclusion", "")).upper() if job["status"] == "completed" else "PENDING"
        if state in {"NONE", ""}:
            raise AgentDeliveryError("A completed Actions job has no definite conclusion.")
        checks.append((job["name"], state))
    confirmed = gh_json(
        ["api", f"repos/{identity.repository}/actions/runs/{run['id']}"],
        identity=identity, proxy=proxy, timeout=per_call(),
    )
    if not isinstance(confirmed, dict) or any(
        confirmed.get(key) != run.get(key) for key in ("id", "head_sha", "run_attempt", "status", "conclusion")
    ):
        raise AgentDeliveryError("Actions run changed during job verification; no CI state was assumed.")
    return {
        "workflow_run_id": run["id"], "run_attempt": run["run_attempt"],
        "checks": sorted(checks), "links": [run.get("html_url")],
        "status": run.get("status"), "conclusion": run.get("conclusion"),
    }


def remote_branch_sha(repo_slug: str, branch: str, *, identity: GitHubPAT, proxy: str | None = None) -> str | None:
    """Read the real remote branch head; None only on a definitive 404."""
    identity.token_for(repo_slug)
    result = run_gh_read(
        ["api", f"repos/{repo_slug}/git/ref/heads/{branch}", "--jq", ".object.sha"],
        identity=identity, proxy=proxy, return_hard_failures=True,
    )
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
    intent_sink: IntentSink, identity: GitHubPAT, proxy: str | None = None,
) -> dict[str, Any]:
    token = identity.token_for(repo_slug)
    if repo_url != f"https://github.com/{repo_slug}.git" or not branch.startswith("agent/"):
        raise AgentDeliveryError("PAT push must target a Delivery branch in the bound repository.")
    target = {
        "operation": "push", "repository": repo_slug, "branch": branch,
        "base_branch": "main", "candidate_sha": candidate_sha,
    }
    intent_sink({**target, "state": "intent"})
    for attempt in range(2):
        try:
            result = _push_once(worktree, repo_url, branch, token, proxy)
        except AgentDeliveryError:
            result = None
        remote = remote_branch_sha(repo_slug, branch, identity=identity, proxy=proxy)
        if remote == candidate_sha:
            intent_sink({**target, "state": "confirmed", "attempts": attempt + 1})
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
    intent_sink: IntentSink, identity: GitHubPAT, proxy: str | None = None,
) -> dict[str, Any]:
    identity.token_for(repo_slug)
    target = {
        "operation": "create_pr", "repository": repo_slug, "branch": branch,
        "base_branch": "main", "candidate_sha": candidate_sha,
    }

    def existing() -> dict[str, Any] | None:
        pulls = gh_json(
            ["pr", "list", "--repo", repo_slug, "--head", branch, "--base", "main", "--state", "open",
             "--json", "number,url,headRefOid"], identity=identity, proxy=proxy,
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
        intent_sink({**target, "state": "confirmed", "reused": True})
        return found
    intent_sink({**target, "state": "intent"})
    created_error: AgentDeliveryError | None = None
    try:
        created = run_gh(
            ["pr", "create", "--repo", repo_slug, "--head", branch, "--base", "main",
             "--title", title, "--body-file", str(body_file)], identity=identity, proxy=proxy,
        )
    except AgentDeliveryError as exc:
        created = None  # A timeout may still have created the PR; reconcile below.
        created_error = exc
    if created is not None and created.returncode == 0:
        match = re.search(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/pull/[0-9]+", created.stdout)
        if match:
            found = existing()
            if found is not None:
                intent_sink({**target, "state": "confirmed", "reused": False})
                return found
    # Ambiguous, failed, or timed-out creation: reconcile read-only; never create a second time blind.
    found = existing()
    if found is not None:
        intent_sink({**target, "state": "confirmed_after_reconcile", "reused": True})
        return found
    raise WriteReconciliationError(
        "Delivery PR creation result could not be confirmed by a read-only lookup; inspect GitHub before retrying."
        + (f" (last error: {created_error})" if created_error else "")
    )


def merge_delivery_pr(
    *, repo_slug: str, pr_number: int, candidate_sha: str, intent_sink: IntentSink,
    identity: GitHubPAT, proxy: str | None = None,
) -> dict[str, Any]:
    identity.token_for(repo_slug)
    target = {
        "operation": "merge_pr", "repository": repo_slug, "pr_number": pr_number,
        "branch_target": "main", "candidate_sha": candidate_sha,
    }
    intent_sink({**target, "state": "intent"})
    for attempt in range(2):
        detail = gh_json(
            ["pr", "view", str(pr_number), "--repo", repo_slug, "--json", "state,mergeCommit,headRefOid"],
            identity=identity, proxy=proxy,
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
            intent_sink({**target, "state": "confirmed", "already_merged": attempt > 0 or None})
            return {
                "merged": True, "merge_sha": merge_commit.get("oid") if isinstance(merge_commit, dict) else None,
            }
        if detail.get("state") != "OPEN" or detail.get("headRefOid") != candidate_sha:
            raise WriteReconciliationError(
                f"Delivery PR is {detail.get('state')} at head {detail.get('headRefOid')}; "
                f"merge is bound to OPEN at {candidate_sha} and was not executed."
            )
        try:
            # Bind the write itself: the PR may change after the read-only preflight.
            run_gh(
                ["pr", "merge", str(pr_number), "--repo", repo_slug, "--merge",
                 "--match-head-commit", candidate_sha],
                identity=identity, proxy=proxy, timeout=120,
            )
        except AgentDeliveryError:
            pass  # A lost merge response may still have merged the PR; the loop re-reads state.
    # Final read-only confirmation after the last bounded merge attempt.
    detail = gh_json(
        ["pr", "view", str(pr_number), "--repo", repo_slug, "--json", "state,mergeCommit,headRefOid"],
        identity=identity, proxy=proxy,
    )
    if isinstance(detail, dict) and detail.get("state") == "MERGED":
        if detail.get("headRefOid") != candidate_sha:
            raise WriteReconciliationError(
                f"The merged Delivery PR head is {detail.get('headRefOid')}, not the reviewed {candidate_sha}."
            )
        merge_commit = detail.get("mergeCommit")
        intent_sink({**target, "state": "confirmed_after_reconcile"})
        return {
            "merged": True, "merge_sha": merge_commit.get("oid") if isinstance(merge_commit, dict) else None,
        }
    raise WriteReconciliationError("Merge could not be confirmed after bounded attempts; inspect GitHub.")


def verify_ci_current(
    identity: GitHubPAT, *, repo_slug: str, pr_number: int, head_sha: str, proxy: str | None = None,
) -> dict[str, Any]:
    """One fresh CI verification for the exact head right before a merge decision."""
    identity.token_for(repo_slug)
    snapshot = _ci_snapshot(identity, pr_number=pr_number, head_sha=head_sha, proxy=proxy)
    checks = snapshot["checks"]
    if (
        snapshot["status"] != "completed" or snapshot["conclusion"] != "success"
        or not checks or any(state != "SUCCESS" for _, state in checks)
        or sum(name == REQUIRED_CHECK_NAME for name, _ in checks) != 1
    ):
        raise AgentDeliveryError(
            "The fixed CI workflow is not currently successful for the reviewed head; merge is not allowed."
        )
    return {
        "workflow_run_id": snapshot["workflow_run_id"], "run_attempt": snapshot.get("run_attempt"),
        "checks": checks,
    }


def wait_for_ci(
    *, repo_slug: str, pr_number: int, head_sha: str, timeout_seconds: int,
    identity: GitHubPAT, proxy: str | None = None,
) -> dict[str, Any]:
    """Poll fixed Actions workflow via PAT-compatible REST; missing/failed checks never pass."""
    identity.token_for(repo_slug)
    deadline = time.monotonic() + timeout_seconds

    def remaining_timeout() -> int:
        left = deadline - time.monotonic()
        if left <= 0:
            raise AgentDeliveryError("CI wait exceeded its total time budget before completion.")
        return max(1, min(GH_TIMEOUT_SECONDS, int(left)))

    while True:
        current = gh_json(
            ["pr", "view", str(pr_number), "--repo", repo_slug, "--json", "headRefOid,baseRefName"],
            identity=identity, proxy=proxy, timeout=remaining_timeout(),
        )
        if not isinstance(current, dict) or current.get("headRefOid") != head_sha or current.get("baseRefName") != "main":
            raise AgentDeliveryError("PR head/target changed while waiting for CI; nothing was merged.")
        snapshot = _ci_snapshot(
            identity, pr_number=pr_number, head_sha=head_sha, proxy=proxy,
            request_timeout=remaining_timeout,
        )
        if time.monotonic() >= deadline:
            raise AgentDeliveryError("CI wait exceeded its total time budget before completion.")
        if snapshot["status"] == "completed":
            checks = snapshot["checks"]
            if (
                snapshot["conclusion"] != "success" or not checks
                or any(state != "SUCCESS" for _, state in checks)
                or sum(name == REQUIRED_CHECK_NAME for name, _ in checks) != 1
            ):
                raise AgentDeliveryError("The fixed CI workflow or its required validate job did not definitely succeed.")
            # Re-read after the snapshot to bind success to the still-current head.
            after = gh_json(
                ["pr", "view", str(pr_number), "--repo", repo_slug, "--json", "headRefOid,baseRefName"],
                identity=identity, proxy=proxy, timeout=remaining_timeout(),
            )
            if time.monotonic() >= deadline:
                raise AgentDeliveryError("CI wait exceeded its total time budget before completion.")
            if after != current:
                raise AgentDeliveryError("PR identity changed during CI verification; nothing was merged.")
            return {**snapshot, "source": "github_actions_rest", "head_sha": head_sha, "workflow": REQUIRED_WORKFLOW}
        if snapshot["status"] not in {"missing", "queued", "in_progress", "waiting", "pending", "requested"}:
            raise AgentDeliveryError("Actions workflow status is unknown; nothing was merged.")
        left = deadline - time.monotonic()
        if left <= 0:
            raise AgentDeliveryError("CI wait exceeded its total time budget before completion.")
        time.sleep(min(CI_POLL_SECONDS, left))
    raise AgentDeliveryError(f"CI did not reach a terminal state within {timeout_seconds} seconds; nothing was merged.")
