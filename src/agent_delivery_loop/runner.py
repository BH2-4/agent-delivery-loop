"""Manual execution of a Work Order authorized by a merged Plan PR."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any

from .claude_worker import (
    ClaudeConfig,
    WorkerCancelled,
    WorkerCleanupError,
    WorkerLifecycle,
    WorkerResultError,
    WorkerStartCancelled,
    preflight,
    run_claude,
)
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
from .github_identity import GitHubPAT
from .store import RunStateError, RunStore, authorization_key, default_state_dir, now_utc, task_key
from .work_order import WorkOrder, parse_work_order


def _release_own_spawn_gate(gate_path: Path | None) -> None:
    """Release exactly this launch's spawn gate after persisting a verified-safe end.

    Only the trusted runner that owns the gate calls this, and only right after it has
    durably written its own safe terminal record (Worker confirmed stopped, or never
    started). The exact-file binding replaces every time-window heuristic: no record
    content, mtime, or timestamp can release someone else's gate, and any failure path
    that never reaches a safe terminal write keeps the gate blocking.
    """

    def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate gate field")
            result[key] = value
        return result

    if gate_path is None:
        return
    try:
        record = json.loads(gate_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
        if isinstance(record, dict) and record.get("kind") == "orchestrator_spawn_gate":
            gate_path.unlink()
    except (OSError, ValueError):
        # An unreadable or malformed gate keeps blocking; that is the safe direction.
        return


def _validate_paths(worktree: Path, order: WorkOrder, paths: Sequence[str]) -> list[str]:
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
    return list(paths)


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


def _record_cancelled_run(
    store: RunStore,
    task_identity_key: str,
    run_id: str,
    record: dict[str, Any],
    *,
    session_id: str | None,
    reason: str,
) -> None:
    record["status"] = "cancelled"
    record["worker_status"] = "stopped"
    record["finished_at"] = now_utc()
    record["completion_status"] = "cancelled"
    record["session_id"] = session_id
    record["failure"] = reason
    store.write(task_identity_key, run_id, record)


def _record_not_started_run(
    store: RunStore,
    task_identity_key: str,
    run_id: str,
    record: dict[str, Any],
    *,
    reason: str,
) -> None:
    record["status"] = "not_started"
    record["worker_status"] = "not_started"
    record["finished_at"] = now_utc()
    record["completion_status"] = "not_started"
    record["session_id"] = None
    record["failure"] = reason
    store.write(task_identity_key, run_id, record)


def _record_cleanup_failed_run(
    store: RunStore,
    task_identity_key: str,
    run_id: str,
    record: dict[str, Any],
    *,
    session_id: str | None,
    reason: str,
) -> None:
    record["status"] = "cleanup_failed"
    record["worker_status"] = "stop_unconfirmed"
    record["finished_at"] = None
    record["completion_status"] = "cleanup_failed"
    record["session_id"] = session_id
    record["failure"] = reason
    store.write(task_identity_key, run_id, record)


def _validate_worker_config_root(root: Path, config: ClaudeConfig) -> None:
    try:
        config_root = config.target_repo_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise AgentDeliveryError("Explicit Claude configuration was not bound to this repository.") from None
    if config_root != root.resolve(strict=True):
        raise AgentDeliveryError("Explicit Claude configuration was not bound to this repository.")


def _unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _read_spawn_gate(gate_path: Path) -> dict[str, Any]:
    """Read one spawn gate, rejecting symlinks, non-regular files, and duplicate keys."""
    import stat as stat_module

    info = gate_path.lstat()
    if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
        raise ValueError("not a regular file")
    record = json.loads(gate_path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
    if not isinstance(record, dict) or record.get("kind") != "orchestrator_spawn_gate":
        raise ValueError("not a spawn gate")
    return record


def execute_plan(
    *,
    repo_path: Path,
    plan_pr: str,
    work_order_path: str,
    worker_config: ClaudeConfig,
    publish: bool = False,
    pat_identity: GitHubPAT | None = None,
    spawn_gate_name: str | None = None,
) -> dict[str, Any]:
    root = repository_root(repo_path.expanduser().resolve())
    _validate_worker_config_root(root, worker_config)
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
            raise AgentDeliveryError("Install the optional dependency with `pip install '.[github-app]'` before publishing.")

    config = worker_config
    store = RunStore(default_state_dir())
    # The orchestrator's durable BLOCKING gate was written and read-back verified before
    # this child was created; the exemption applies to exactly that one validated record
    # in EVERY availability check (including the preflight below). A gate that is
    # missing, misnamed, a symlink, corrupt JSON, duplicate-keyed, or of the wrong kind
    # is rejected — never silently exempted.
    exempt_record: Path | None = None
    if spawn_gate_name is not None:
        parts = spawn_gate_name.split("/")
        candidate_gate = store.runs / spawn_gate_name if len(parts) == 2 else None
        if (
            candidate_gate is None
            or not parts[1].startswith("spawn-")
            or not parts[1].endswith(".json")
        ):
            raise AgentDeliveryError("The orchestrator spawn gate is invalid or missing; refusing to run.")
        try:
            gate_record = _read_spawn_gate(candidate_gate)
        except (OSError, ValueError):
            raise AgentDeliveryError(
                "The orchestrator spawn gate is invalid or missing; refusing to run."
            ) from None
        if str(gate_record.get("repository", "")).casefold() != plan_repo.slug.casefold():
            raise AgentDeliveryError(
                "The orchestrator spawn gate is bound to a different repository; refusing to run."
            )
        exempt_record = candidate_gate
    store.assert_worker_available(exempt_record=exempt_record)
    run_id = str(uuid.uuid4())
    runtime_home = store.root / "runtime" / run_id
    version = preflight(config, runtime_home)

    if pat_identity is not None:
        # The trusted Python runner performs its authorization reads with the same explicit
        # PAT snapshot the orchestrator validated; anonymous shared-exit quota is no longer
        # part of the PAT-mode path. token_for re-checks the repository binding.
        client = GitHubClient(plan_repo, token=pat_identity.token_for(plan_repo.slug))
    else:
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
    # Late binding check: the exempted gate must also name THIS authorized task.
    if exempt_record is not None:
        try:
            gate_record = _read_spawn_gate(exempt_record)
        except (OSError, ValueError):
            raise AgentDeliveryError("The orchestrator spawn gate became invalid; refusing to run.") from None
        if gate_record.get("task_key") != task_identity_key:
            raise AgentDeliveryError(
                "The orchestrator spawn gate is bound to a different task; refusing to run."
            )
    auth_key = authorization_key(plan_repo.slug, number, authorization.merge_sha, order.sha256)
    branch = f"agent/{order.task_id.lower()}-r{order.revision}-{run_id[:8]}"
    worktree = store.root / "worktrees" / run_id
    record: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "task_id": order.task_id,
        "revision": order.revision,
        "status": "starting",
        "worker_status": "start_unconfirmed",
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
        "requested_effort": config.effort,
        "auth_source": config.auth_source,
        "auth_env_name": config.auth_name,
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
    worker_lifecycle = WorkerLifecycle()

    # exempt_record was validated before the preflight above and covers the claim too.
    with store.claim(task_identity_key, exempt_record=exempt_record):
        # This durable, read-back-verified record is the gate BEFORE any Worker
        # creation. A later failed/interrupted update cannot release it.
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
            outcome = run_claude(
                worktree,
                skill_path,
                order,
                runtime_home,
                config,
                lifecycle=worker_lifecycle,
            )
            record["session_id"] = outcome.session_id
            record["completion_status"] = outcome.completion_status
            record["incomplete_items"] = outcome.incomplete_items
            record["status"] = "validating"
            record["worker_status"] = "stopped"
            store.write(task_identity_key, run_id, record)
            _safe_changed_paths(worktree, order)
            ensure_diff_clean(worktree)
            staged = stage_changes(worktree)
            _validate_paths(worktree, order, staged.paths)
            committed = commit_changes(worktree, order.identity, staged)
            commit_sha = committed.commit_sha
            paths = _validate_paths(worktree, order, committed.paths)
            record["changed_paths"] = list(paths)
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
            _release_own_spawn_gate(exempt_record)  # own verified-safe end persisted
            return {
                **record,
                "worktree_id": run_id,
                "delivery_commit": commit_sha,
            }
        except WorkerStartCancelled as exc:
            _record_not_started_run(
                store,
                task_identity_key,
                run_id,
                record,
                reason=str(exc),
            )
            _release_own_spawn_gate(exempt_record)  # Worker never started
            raise AgentDeliveryError(f"Work Order was cancelled before the Worker started. Run ID: {run_id}.") from None
        except WorkerCancelled as exc:
            worker_lifecycle.status = "stopped"
            worker_lifecycle.session_id = exc.session_id
            _record_cancelled_run(
                store,
                task_identity_key,
                run_id,
                record,
                session_id=exc.session_id,
                reason="Cancellation completed after the Worker process group stopped.",
            )
            _release_own_spawn_gate(exempt_record)  # own verified-safe end persisted
            raise AgentDeliveryError(f"Work Order was cancelled after Worker cleanup. Run ID: {run_id}.") from None
        except WorkerCleanupError as exc:
            worker_lifecycle.status = "stop_unconfirmed"
            worker_lifecycle.session_id = exc.session_id
            _record_cleanup_failed_run(
                store,
                task_identity_key,
                run_id,
                record,
                session_id=exc.session_id,
                reason=str(exc),
            )
            raise AgentDeliveryError(
                f"Worker cleanup failed; future runs are blocked pending manual inspection. Run ID: {run_id}."
            ) from None
        except KeyboardInterrupt:
            if worker_lifecycle.status == "not_started":
                _record_not_started_run(
                    store,
                    task_identity_key,
                    run_id,
                    record,
                    reason="Cancellation occurred before the Worker started.",
                )
                _release_own_spawn_gate(exempt_record)  # Worker never started
                raise AgentDeliveryError(f"Work Order was cancelled before the Worker started. Run ID: {run_id}.") from None
            if worker_lifecycle.status == "stopped":
                _record_cancelled_run(
                    store,
                    task_identity_key,
                    run_id,
                    record,
                    session_id=worker_lifecycle.session_id,
                    reason="Run cancelled after the Worker process group was confirmed stopped.",
                )
                _release_own_spawn_gate(exempt_record)  # own verified-safe end persisted
                raise AgentDeliveryError(f"Work Order was cancelled after Worker cleanup. Run ID: {run_id}.") from None
            _record_cleanup_failed_run(
                store,
                task_identity_key,
                run_id,
                record,
                session_id=worker_lifecycle.session_id,
                reason="Worker stop was not confirmed after cancellation; manual inspection is required.",
            )
            raise AgentDeliveryError(
                f"Worker stop was not confirmed; future runs are blocked pending manual inspection. Run ID: {run_id}."
            ) from None
        except RunStateError:
            # Do not try to repair or overwrite a failed safety-state update.
            raise
        except Exception as exc:
            if worker_lifecycle.status not in {"not_started", "stopped"}:
                _record_cleanup_failed_run(
                    store, task_identity_key, run_id, record,
                    session_id=worker_lifecycle.session_id,
                    reason="An error escaped while Worker safety was unconfirmed; stop trials and obtain manual safety review.",
                )
                raise AgentDeliveryError(f"Worker safety is unconfirmed; future runs remain blocked. Run ID: {run_id}.") from None
            record["status"] = "failed"
            record["worker_status"] = worker_lifecycle.status
            record["session_id"] = worker_lifecycle.session_id
            record["finished_at"] = now_utc()
            if isinstance(exc, WorkerResultError):
                record["completion_status"] = exc.completion_status
                record["incomplete_items"] = exc.incomplete_items
                if exc.session_id:
                    record["session_id"] = exc.session_id
            record["failure"] = str(exc) if isinstance(exc, AgentDeliveryError) else "Unexpected worker failure; raw output was suppressed."
            store.write(task_identity_key, run_id, record)
            if worker_lifecycle.status in {"not_started", "stopped"}:
                _release_own_spawn_gate(exempt_record)  # own verified-safe end persisted
            if isinstance(exc, AgentDeliveryError):
                raise AgentDeliveryError(f"{exc} Run ID: {run_id}.") from None
            raise AgentDeliveryError(record["failure"]) from None


def watch_once(
    *,
    repo_path: Path,
    worker_config: ClaudeConfig,
    publish: bool = False,
) -> dict[str, Any] | None:
    root = repository_root(repo_path.expanduser().resolve())
    _validate_worker_config_root(root, worker_config)
    repo = repository_remote(root)
    store = RunStore(default_state_dir())
    store.assert_worker_available()
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
            worker_config=worker_config,
            publish=publish,
        )
    return None
