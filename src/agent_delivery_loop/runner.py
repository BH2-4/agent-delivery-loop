"""Manual execution of a Work Order authorized by a merged Plan PR."""

from __future__ import annotations

import hashlib
import importlib.util
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from .claude_worker import ClaudeConfig, WorkerResultError, preflight, run_claude
from .errors import AgentDeliveryError
from .git_ops import (
    changed_paths,
    commit_changes,
    create_worktree,
    ensure_diff_clean,
    ensure_plan_is_on_main,
    git,
    push_branch_with_app_token,
    repository_remote,
    repository_root,
    stage_changes,
)
from .github import GitHubClient, Repo, parse_plan_pr_ref
from .github_app import GitHubAppConfig, GitHubAppTokenProvider
from .store import RunStore, authorization_key, default_state_dir, now_utc, task_key
from .work_order import WorkOrder, parse_work_order


def _validate_paths(worktree: Path, order: WorkOrder, paths: list[str]) -> list[str]:
    if not paths:
        raise AgentDeliveryError("Claude Code produced no changes; no Delivery branch was committed.")
    for relative in paths:
        if relative.startswith((".agents/work-orders/", ".agents/policies/")):
            raise AgentDeliveryError("Worker changes to Work Orders and policy Skills are always prohibited.")
        if not order.allows_path(relative):
            raise AgentDeliveryError(f"Changed path is outside allowed_paths: {relative}.")
        candidate = worktree.joinpath(*PurePosixPath(relative).parts)
        try:
            candidate.resolve(strict=False).relative_to(worktree.resolve())
        except ValueError:
            raise AgentDeliveryError(f"Changed path resolves outside the isolated worktree: {relative}.") from None
        if candidate.is_symlink():
            raise AgentDeliveryError(f"Changed path is a symlink; first-version delivery rejects it: {relative}.")
    return paths


def _safe_changed_paths(worktree: Path, order: WorkOrder) -> list[str]:
    return _validate_paths(worktree, order, changed_paths(worktree))


def _delivery_body(
    order: WorkOrder,
    plan_url: str,
    merge_sha: str,
    run_id: str,
    skill_sha256: str,
    paths: list[str],
) -> str:
    files = "\n".join(f"- `{path}`" for path in paths)
    return (
        f"## Authorized task\n\n"
        f"- Work Order: `{order.task_id}` revision `{order.revision}`\n"
        f"- Plan PR: {plan_url}\n"
        f"- Plan merge commit: `{merge_sha}`\n"
        f"- Work Order SHA-256: `{order.sha256}`\n"
        f"- Delivery Skill: `{order.skill_ref}` (SHA-256 `{skill_sha256}`)\n"
        f"- Local run ID: `{run_id}`\n"
        "\n"
        f"## Changed paths\n\n{files}\n\n"
        "CI and an independent Steward review are pending. This runner does not approve or merge PRs."
    )


def execute_plan(
    *,
    repo_path: Path,
    plan_pr: str,
    work_order_path: str,
    publish: bool = False,
) -> dict[str, Any]:
    root = repository_root(repo_path.expanduser().resolve())
    local_repo = repository_remote(root)
    plan_repo, number, plan_url = parse_plan_pr_ref(plan_pr)
    if local_repo.slug.casefold() != plan_repo.slug.casefold():
        raise AgentDeliveryError("The Plan PR and local origin must refer to the same GitHub repository.")

    app_config: GitHubAppConfig | None = None
    if publish:
        app_config = GitHubAppConfig.from_environment(root)
        if app_config is None:
            raise AgentDeliveryError("App publishing was requested but no verified GitHub App configuration is present.")
        if importlib.util.find_spec("jwt") is None:
            raise AgentDeliveryError("Install the optional dependency with `pip install -e '.[github-app]'` before publishing.")

    config = ClaudeConfig.from_environment()
    store = RunStore(default_state_dir())
    run_id = str(uuid.uuid4())
    runtime_home = store.root / "runtime" / run_id
    version = preflight(config, runtime_home)

    client = GitHubClient(plan_repo)
    authorization = client.authorized_plan(number, work_order_path, plan_url)
    order = parse_work_order(authorization.order_bytes, expected_path=authorization.order_path)
    ensure_plan_is_on_main(root, plan_repo, authorization.merge_sha)
    skill_bytes = client.content(order.skill_ref, authorization.merge_sha)
    try:
        skill_text = skill_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentDeliveryError("The authorized Delivery Skill is not valid UTF-8.") from exc
    if not skill_text.strip() or len(skill_bytes) > 64 * 1024:
        raise AgentDeliveryError("The authorized Delivery Skill is empty or exceeds the 64 KiB limit.")

    task_identity_key = task_key(plan_repo.slug, order.task_id, order.revision)
    auth_key = authorization_key(plan_repo.slug, number, authorization.merge_sha, order.sha256)
    branch = f"agent/{order.task_id.lower()}-r{order.revision}-{run_id[:8]}"
    worktree = store.root / "worktrees" / run_id
    record: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": order.task_id,
        "revision": order.revision,
        "status": "starting",
        "started_at": now_utc(),
        "finished_at": None,
        "repository": plan_repo.slug,
        "plan_pr": plan_url,
        "plan_merge_sha": authorization.merge_sha,
        "work_order_path": authorization.order_path,
        "work_order_sha256": order.sha256,
        "skill_sha256": hashlib.sha256(skill_bytes).hexdigest(),
        "authorization_key": auth_key,
        "worker_profile": order.worker_profile,
        "claude_code_version": ".".join(map(str, version)),
        "requested_model": config.model,
        "provider_host": config.provider_host,
        "provider_route_sha256": config.provider_route_sha256,
        "session_id": None,
        "completion_status": None,
        "incomplete_items": [],
        "delivery_branch": branch,
        "changed_paths": [],
        "delivery_pr": None,
        "result_summary": "No delivery has been validated yet; raw model output and transcript are not retained.",
        "failure": None,
    }

    with store.claim(task_identity_key):
        store.write(task_identity_key, run_id, record)
        try:
            create_worktree(root, worktree, branch, authorization.merge_sha)
            skill_path = worktree / PurePosixPath(order.skill_ref)
            try:
                skill_path.resolve(strict=True).relative_to(worktree.resolve())
            except (OSError, ValueError):
                raise AgentDeliveryError("The authorized Delivery Skill must be a regular file inside the worktree.") from None
            if skill_path.is_symlink() or not skill_path.is_file() or skill_path.read_bytes() != skill_bytes:
                raise AgentDeliveryError("The isolated worktree Skill does not match the Skill at the Plan merge commit.")
            outcome = run_claude(worktree, skill_path, order, runtime_home, config)
            record["session_id"] = outcome.session_id
            record["completion_status"] = outcome.completion_status
            record["incomplete_items"] = outcome.incomplete_items
            record["status"] = "validating"
            store.write(task_identity_key, run_id, record)
            _safe_changed_paths(worktree, order)
            ensure_diff_clean(worktree)
            paths = _validate_paths(worktree, order, stage_changes(worktree))
            commit_sha = commit_changes(worktree, order.identity)
            record["changed_paths"] = paths
            record["result_summary"] = f"Validated {len(paths)} committed path(s); raw model output and transcript were not retained."
            record["delivery_commit"] = commit_sha
            record["status"] = "local_ready"

            if app_config is not None:
                token = GitHubAppTokenProvider(plan_repo, app_config).installation_token()
                push_branch_with_app_token(worktree, plan_repo, branch, token)
                app_client = GitHubClient(plan_repo, token=token)
                delivery_url = app_client.create_pull_request(
                    head=f"{plan_repo.owner}:{branch}",
                    title=f"Delivery: {order.task_id} r{order.revision}",
                    body=_delivery_body(
                        order,
                        plan_url,
                        authorization.merge_sha,
                        run_id,
                        record["skill_sha256"],
                        paths,
                    ),
                )
                record["delivery_pr"] = delivery_url
                record["status"] = "delivery_pr_open"
            record["finished_at"] = now_utc()
            store.write(task_identity_key, run_id, record)
            return {
                **record,
                "worktree_id": run_id,
                "delivery_commit": commit_sha,
            }
        except Exception as exc:
            record["status"] = "failed"
            record["finished_at"] = now_utc()
            if isinstance(exc, WorkerResultError):
                record["completion_status"] = exc.completion_status
                record["incomplete_items"] = exc.incomplete_items
                if exc.session_id:
                    record["session_id"] = exc.session_id
            record["failure"] = str(exc) if isinstance(exc, AgentDeliveryError) else "Unexpected worker failure; raw output was suppressed."
            store.write(task_identity_key, run_id, record)
            if isinstance(exc, AgentDeliveryError):
                raise AgentDeliveryError(f"{exc} Run ID: {run_id}.") from None
            raise AgentDeliveryError(record["failure"]) from None


def watch_once(*, repo_path: Path, publish: bool = False) -> dict[str, Any] | None:
    root = repository_root(repo_path.expanduser().resolve())
    repo = repository_remote(root)
    store = RunStore(default_state_dir())
    client = GitHubClient(repo)
    for number, path, url in client.merged_plan_candidates():
        authorization = client.authorized_plan(number, path, url)
        order = parse_work_order(authorization.order_bytes, expected_path=authorization.order_path)
        key = task_key(repo.slug, order.task_id, order.revision)
        if store.has_record(key):
            continue
        return execute_plan(
            repo_path=root,
            plan_pr=url,
            work_order_path=path,
            publish=publish,
        )
    return None
