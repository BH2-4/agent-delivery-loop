"""Deterministic single-shot delivery orchestration: fixed inputs, real captured results, fail-closed gates."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from .claude_worker import _stop_process_group
from .errors import AgentDeliveryError
from .git_ops import git, repository_remote, repository_root
from .github import GitHubClient, parse_plan_pr_ref
from .review_cli import (
    CREDENTIAL_ENV_NAMES,
    MAX_REVIEW_TIMEOUT_SECONDS,
    MIN_REVIEW_TIMEOUT_SECONDS,
    run_review_process,
)
from .review_handoff import check_review, prepare_review
from .runner import _delivery_body  # reuse the authorized Delivery PR body format
from .store import default_state_dir, now_utc, task_key
from .work_order import parse_work_order

SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MAX_AGENT_RUN_OUTPUT_BYTES = 64 * 1024
GH_TIMEOUT_SECONDS = 60
CI_POLL_SECONDS = 20
REQUIRED_CHECK_NAME = "validate"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


class Orchestration:
    def __init__(self, orchestration_id: str) -> None:
        self.id = orchestration_id
        self.record_dir = default_state_dir() / "orchestrations"
        self.record_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.output_dir = self.record_dir / f"{orchestration_id}.output"
        self.output_dir.mkdir(mode=0o700)
        self.record: dict[str, Any] = {
            "schema_version": 1,
            "orchestration_id": orchestration_id,
            "mode": "single_shot_delivery",
            "stage": "starting",
            "started_at": now_utc(),
            "finished_at": None,
            "failure": None,
        }

    def save(self, **updates: Any) -> None:
        self.record.update(updates)
        payload = json.dumps(self.record, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        path = self.record_dir / f"{self.id}.json"
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def fail(self, stage: str, message: str) -> None:
        self.save(stage=stage, failure=message, finished_at=now_utc())

    def finish(self, stage: str, **updates: Any) -> None:
        self.save(stage=stage, finished_at=now_utc(), **updates)


def _run_bounded(argv: list[str], *, timeout: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, text=True, capture_output=True, timeout=timeout, check=False,
            env=env if env is not None else os.environ.copy(), stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentDeliveryError(f"A required command could not run or timed out: {argv[0]}.") from exc


def _gh(args: list[str], *, timeout: int = GH_TIMEOUT_SECONDS, proxy: str | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if proxy:
        env.update({"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy})
    result = _run_bounded(["gh", *args], timeout=timeout, env=env)
    if result.returncode != 0:
        raise AgentDeliveryError(f"gh {' '.join(args[:2])} failed with exit code {result.returncode}; no state was assumed.")
    return result


def _gh_json(args: list[str], *, proxy: str | None = None, timeout: int = GH_TIMEOUT_SECONDS) -> Any:
    """Run gh with an explicit --json field list already in argv and parse unique-key JSON."""
    result = _gh(args, proxy=proxy, timeout=timeout)
    try:
        return json.loads(result.stdout, object_pairs_hook=_unique_object)
    except ValueError as exc:
        raise AgentDeliveryError("gh returned output that is not unique-key JSON.") from exc


def verify_installation(*, receipt_path: Path, expected_source_sha: str, expected_wheel_sha256: str) -> dict[str, Any]:
    """The installed entry must match the approved source and wheel, and must not be an editable checkout."""
    if not SHA_RE.fullmatch(expected_source_sha) or not SHA256_RE.fullmatch(expected_wheel_sha256):
        raise AgentDeliveryError("Expected install provenance must be full SHA-256/40-hex values.")
    try:
        receipt = json.loads(receipt_path.read_bytes(), object_pairs_hook=_unique_object)
    except (OSError, ValueError) as exc:
        raise AgentDeliveryError("Install receipt is missing or invalid.") from exc
    required = {"schema_version", "source_sha", "wheel_sha256", "venv_bin", "wheel_path"}
    if not isinstance(receipt, dict) or not required.issubset(receipt) or receipt.get("schema_version") != 1:
        raise AgentDeliveryError("Install receipt is missing required provenance fields.")
    if receipt["source_sha"] != expected_source_sha or receipt["wheel_sha256"] != expected_wheel_sha256:
        raise AgentDeliveryError("Installed provenance does not match the approved source or wheel digest.")
    venv_bin = Path(receipt["venv_bin"]).expanduser()
    entry = venv_bin / "agent-run"
    orchestrator_entry = venv_bin / "agent-delivery"
    for path in (entry, orchestrator_entry, venv_bin / "python"):
        if not path.is_file() or not os.access(path, os.X_OK):
            raise AgentDeliveryError("The recorded virtual environment is missing a required executable entry.")
    wheel = Path(receipt["wheel_path"]).expanduser()
    if not wheel.is_file():
        raise AgentDeliveryError("The recorded wheel artifact no longer exists; provenance cannot be re-verified.")
    if hashlib.sha256(wheel.read_bytes()).hexdigest() != expected_wheel_sha256:
        raise AgentDeliveryError("The wheel artifact digest no longer matches the approved digest.")
    probe = _run_bounded(
        [str(venv_bin / "python"), "-c", "import agent_delivery_loop; print(agent_delivery_loop.__file__)"],
        timeout=60,
    )
    if probe.returncode != 0:
        raise AgentDeliveryError("The installed package could not be imported from the recorded environment.")
    origin = Path(probe.stdout.strip()).resolve()
    site_packages = (venv_bin.parent / "lib").resolve()
    if site_packages not in origin.parents:
        raise AgentDeliveryError("Installed package resolves outside the virtual environment; editable installs are not allowed.")
    # The orchestrator itself must run from that same installed tree, not a source checkout.
    if site_packages not in Path(__file__).resolve().parents:
        raise AgentDeliveryError("The orchestrator is not running from the recorded installed environment.")
    return {
        "source_sha": receipt["source_sha"],
        "wheel_sha256": receipt["wheel_sha256"],
        "agent_run_entry": str(entry),
        "python": str(venv_bin / "python"),
    }


def _spawn_agent_run(
    *, entry: str, repo_root: Path, plan_pr: str, work_order_path: str,
    model: str, base_url: str, effort: str, auth_config: Path,
    timeout_seconds: int, proxy: str | None, stdout_path: Path,
) -> dict[str, Any]:
    argv = [
        entry, "--plan-pr", plan_pr, "--work-order-path", work_order_path,
        "--repo-path", str(repo_root),
        "--model", model, "--base-url", base_url, "--effort", effort,
        "--auth-config", str(auth_config),
    ]
    env = {key: value for key, value in os.environ.items() if key not in CREDENTIAL_ENV_NAMES}
    if proxy:
        env.update({"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy})
    try:
        with open(stdout_path, "wb") as captured:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=captured, stderr=subprocess.STDOUT,
                env=env, start_new_session=True,
            )
            try:
                exit_code = process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                if not _stop_process_group(process):
                    raise AgentDeliveryError(
                        "The spawned agent-run exceeded its time limit and its process group could not be confirmed stopped; runs are blocked."
                    )
                raise AgentDeliveryError("The spawned agent-run exceeded its time limit and was stopped; no delivery continued.")
    except OSError as exc:
        raise AgentDeliveryError("The installed agent-run entry could not be started.") from exc
    output = stdout_path.read_bytes()[:MAX_AGENT_RUN_OUTPUT_BYTES]
    if exit_code != 0:
        raise AgentDeliveryError(f"The installed agent-run exited with code {exit_code}; its sanitized output was preserved.")
    try:
        result = json.loads(output.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError) as exc:
        raise AgentDeliveryError("agent-run output was not readable JSON; no delivery continued.") from exc
    result["agent_run_exit_code"] = exit_code
    return result


def _wait_for_ci(*, repo_slug: str, pr_number: int, head_sha: str, timeout_seconds: int, proxy: str | None) -> dict[str, Any]:
    """Poll real gh check data; missing, pending-forever, cancelled, or failing checks never pass."""
    deadline = time.monotonic() + timeout_seconds
    last: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        checks = _gh_json(["pr", "checks", str(pr_number), "--repo", repo_slug, "--json", "name,state,link"], proxy=proxy)
        if not isinstance(checks, list):
            raise AgentDeliveryError("gh pr checks did not return a check list.")
        last = [item for item in checks if isinstance(item, dict)]
        states = {str(item.get("state", "")).upper() for item in last}
        if last and states == {"SUCCESS"}:
            required = [item for item in last if item.get("name") == REQUIRED_CHECK_NAME]
            if len(required) != 1:
                raise AgentDeliveryError(f"The required '{REQUIRED_CHECK_NAME}' check is missing from the PR checks.")
            current_head = _gh_json(
                ["pr", "view", str(pr_number), "--repo", repo_slug, "--json", "headRefOid"], proxy=proxy
            )
            if not isinstance(current_head, dict) or current_head.get("headRefOid") != head_sha:
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


def deliver(
    *,
    repo_path: Path, plan_pr: str, work_order_path: str,
    model: str, base_url: str, effort: str, auth_config: Path,
    install_receipt: Path, expected_source_sha: str, expected_wheel_sha256: str,
    review_model: str, review_effort: str, review_timeout_seconds: int,
    review_bundle_dir: Path, proxy: str | None = None,
    ci_timeout_seconds: int = 900, auto_merge: bool = False,
) -> dict[str, Any]:
    if not MIN_REVIEW_TIMEOUT_SECONDS <= review_timeout_seconds <= MAX_REVIEW_TIMEOUT_SECONDS:
        raise AgentDeliveryError("Review timeout is out of the allowed range.")
    orchestration = Orchestration(str(uuid.uuid4()))
    try:
        # Stage 1: the running orchestration entry must be the reviewed, installed wheel.
        install = verify_installation(
            receipt_path=install_receipt, expected_source_sha=expected_source_sha,
            expected_wheel_sha256=expected_wheel_sha256,
        )
        orchestration.save(stage="install_verified", install=install)

        if proxy:
            os.environ.update({"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy})
        root = repository_root(repo_path.expanduser().resolve())
        local_repo = repository_remote(root)
        plan_repo, number, plan_url = parse_plan_pr_ref(plan_pr)
        if local_repo.slug.casefold() != plan_repo.slug.casefold():
            raise AgentDeliveryError("The Plan PR and the local repository origin must refer to the same repository.")
        client = GitHubClient(plan_repo)
        authorization = client.authorized_plan(number, work_order_path, plan_url)
        order = parse_work_order(authorization.order_bytes, expected_path=authorization.order_path)
        orchestration.save(
            stage="authorization_verified",
            authorization={
                "plan_pr": plan_url, "plan_merge_sha": authorization.merge_sha,
                "work_order_path": authorization.order_path, "work_order_sha256": order.sha256,
                "task_id": order.task_id, "revision": order.revision,
            },
        )

        # Stage 2: run the real Worker through the installed entry; capture its exit code.
        agent_run_output = _spawn_agent_run(
            entry=install["agent_run_entry"], repo_root=root, plan_pr=plan_pr,
            work_order_path=work_order_path, model=model, base_url=base_url,
            effort=effort, auth_config=auth_config,
            timeout_seconds=order.timeout_seconds + 300, proxy=proxy,
            stdout_path=orchestration.output_dir / "agent-run-stdout.json",
        )
        run_id = agent_run_output.get("run_id")
        delivery_branch = agent_run_output.get("delivery_branch")
        delivery_commit = agent_run_output.get("delivery_commit")
        if not isinstance(run_id, str) or not isinstance(delivery_branch, str) or not SHA_RE.fullmatch(str(delivery_commit)):
            raise AgentDeliveryError("agent-run did not report a run ID, delivery branch, and full delivery commit.")
        record_path = default_state_dir() / "runs" / task_key(plan_repo.slug, order.task_id, order.revision) / f"{run_id}.json"
        record = json.loads(record_path.read_bytes(), object_pairs_hook=_unique_object)
        if (
            record.get("run_id") != run_id or record.get("worker_status") != "stopped"
            or record.get("status") != "local_ready" or record.get("completion_status") != "complete"
            or record.get("failure") is not None or record.get("delivery_commit") != delivery_commit
        ):
            raise AgentDeliveryError("The run record does not prove a stopped Worker with a completed local candidate.")
        for path in record.get("changed_paths", []):
            if not order.allows_path(path):
                raise AgentDeliveryError(f"Changed path is outside the authorized Work Order scope: {path}.")
        orchestration.save(
            stage="worker_completed",
            worker={
                "run_id": run_id, "session_id": record.get("session_id"),
                "agent_run_exit_code": agent_run_output["agent_run_exit_code"],
                "delivery_branch": delivery_branch, "delivery_commit": delivery_commit,
                "changed_paths": record.get("changed_paths"),
            },
        )

        # Stage 3: bounded real review through the read-only CLI, with a captured exit code.
        bundle = prepare_review(
            output_dir=review_bundle_dir, repo_path=root, plan_pr=plan_pr,
            work_order_path=work_order_path, run_record=record_path, head_sha=str(delivery_commit),
        )
        review_run = run_review_process(
            bundle_dir=review_bundle_dir, timeout_seconds=review_timeout_seconds,
            review_model=review_model, review_effort=review_effort, proxy=proxy,
        )
        checked = check_review(
            bundle_dir=review_bundle_dir, result_path=Path(review_run["result_path"]),
            review_exit_code=review_run["exit_code"],
            repo_path=root, plan_pr=plan_pr, work_order_path=work_order_path,
            run_record=record_path, head_sha=str(delivery_commit),
            exit_code_source="captured_by_orchestrator",
        )
        if checked.get("verdict") != "pass":
            raise AgentDeliveryError("The independent review did not pass; nothing was published.")
        orchestration.save(
            stage="review_passed",
            review={
                "review_id": review_run["review_id"], "codex_version": review_run["codex_version"],
                "exit_code": review_run["exit_code"], "stop_status": review_run["stop_status"],
                "context_sha256": bundle.get("context_sha256"),
                "unverified_count": checked.get("unverified_count"),
            },
        )

        # Stage 4: publish with the one-shot personal gh identity (never passed to the Worker).
        token = _gh(["auth", "token"], proxy=proxy).stdout.strip()
        if not token:
            raise AgentDeliveryError("gh did not provide an authentication token for the one-shot push.")
        with tempfile.TemporaryDirectory(prefix="adl-askpass-") as temporary:
            askpass = Path(temporary) / "askpass"
            askpass.write_text(
                '#!/bin/sh\ncase "$1" in *Username*) printf \'%s\\n\' "x-access-token" ;;'
                ' *Password*) printf \'%s\\n\' "$AGENT_GIT_PASSWORD" ;; *) exit 1 ;; esac\n',
                encoding="utf-8",
            )
            askpass.chmod(0o700)
            push_env = {key: value for key, value in os.environ.items() if key not in CREDENTIAL_ENV_NAMES}
            push_env.update({"GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0", "AGENT_GIT_PASSWORD": token})
            if proxy:
                push_env.update({"https_proxy": proxy, "http_proxy": proxy})
            worktree = default_state_dir() / "worktrees" / run_id
            pushed = git(
                worktree, "-c", "credential.helper=", "push", plan_repo.https_url,
                f"HEAD:refs/heads/{delivery_branch}", env=push_env, check=False,
            )
        if pushed.returncode != 0:
            raise AgentDeliveryError("The delivery branch could not be pushed with the one-shot identity.")
        remote_head = _gh(
            ["api", f"repos/{plan_repo.slug}/git/ref/heads/{delivery_branch}", "--jq", ".object.sha"], proxy=proxy
        ).stdout.strip()
        if not SHA_RE.fullmatch(remote_head) or remote_head != delivery_commit:
            raise AgentDeliveryError("Remote delivery branch does not match the reviewed head; publishing stopped.")
        pulls = _gh_json(
            ["pr", "list", "--repo", plan_repo.slug, "--head", delivery_branch, "--base", "main", "--state", "open",
             "--json", "number,url"],
            proxy=proxy,
        )
        if isinstance(pulls, list) and len(pulls) == 1 and isinstance(pulls[0], dict):
            pr_number = pulls[0]["number"]
            pr_url = pulls[0]["url"]
        elif isinstance(pulls, list) and not pulls:
            body_path = orchestration.output_dir / "delivery-pr-body.md"
            body_path.write_text(
                _delivery_body(order, plan_url, authorization.merge_sha, run_id, record["skill_sha256"], record["changed_paths"])
                + "\n\nOrchestrated single-shot delivery; review and CI gates verified before merge.\n",
                encoding="utf-8",
            )
            created = _gh(["pr", "create", "--repo", plan_repo.slug, "--head", delivery_branch, "--base", "main",
                           "--title", f"Delivery: {order.task_id} r{order.revision}", "--body-file", str(body_path)], proxy=proxy)
            match = re.search(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+/pull/[0-9]+", created.stdout)
            if not match:
                raise AgentDeliveryError("Delivery PR creation result was uncertain; inspect GitHub before retrying.")
            pr_url = match.group(0)
            pr_number = int(pr_url.rsplit("/", 1)[1])
        else:
            raise AgentDeliveryError("Delivery PR lookup was ambiguous; inspect GitHub before retrying.")
        orchestration.save(stage="delivery_pr_open", delivery_pr={"number": pr_number, "url": pr_url, "head": delivery_commit})

        # Stage 5: wait for the real CI bound to this head.
        ci = _wait_for_ci(
            repo_slug=plan_repo.slug, pr_number=pr_number, head_sha=delivery_commit,
            timeout_seconds=ci_timeout_seconds, proxy=proxy,
        )
        orchestration.save(stage="ci_passed", ci=ci)

        # Stage 6: conditional merge, then verify the actual GitHub result.
        merge = {"merged": False, "merge_sha": None, "main_contains_head": False}
        if auto_merge:
            _gh(["pr", "merge", str(pr_number), "--repo", plan_repo.slug, "--merge"], proxy=proxy, timeout=120)
            detail = _gh_json(
                ["pr", "view", str(pr_number), "--repo", plan_repo.slug, "--json", "state,mergeCommit"], proxy=proxy
            )
            merged = isinstance(detail, dict) and detail.get("state") == "MERGED"
            if not merged:
                raise AgentDeliveryError("Merge was requested but GitHub does not report the Delivery PR as merged.")
            merge_commit = detail.get("mergeCommit")
            merge = {
                "merged": True,
                "merge_sha": merge_commit.get("oid") if isinstance(merge_commit, dict) else None,
                "main_contains_head": False,
            }
            git(root, "fetch", "--no-tags", plan_repo.https_url, "refs/heads/main:refs/remotes/adl-main")
            if git(root, "merge-base", "--is-ancestor", delivery_commit, "refs/remotes/adl-main", check=False).returncode != 0:
                raise AgentDeliveryError("Merged PR head is not reachable from the fetched main; verify on GitHub.")
            merge["main_contains_head"] = True
        result = orchestration.finish(
            "completed" if merge["merged"] else "awaiting_user_merge",
            delivery_pr={"number": pr_number, "url": pr_url, "head": delivery_commit},
            merge=merge, auto_merge_requested=auto_merge,
        )
        return {**orchestration.record, "orchestration_id": orchestration.id}
    except AgentDeliveryError as exc:
        orchestration.fail(orchestration.record.get("stage", "starting"), str(exc))
        raise
    except Exception as exc:
        orchestration.fail(orchestration.record.get("stage", "starting"), "Unexpected orchestration failure; sensitive output suppressed.")
        raise AgentDeliveryError("Unexpected orchestration failure; sensitive output suppressed.") from exc
