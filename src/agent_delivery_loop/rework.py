"""Bounded rework: a fresh Worker session on the existing Delivery branch.

The original run record, original candidate commit, real exit code, and receipts
are never rewritten. Each rework gets its own run record linked to the original
run, reuses the exact Worker launch/stop/safety machinery, and produces a new
traceable candidate commit on the same branch.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from typing import Any

from .claude_worker import (
    ClaudeConfig,
    WorkerCancelled,
    WorkerCleanupError,
    WorkerLifecycle,
    WorkerResultError,
    WorkerStartCancelled,
    run_claude,
)
from .errors import AgentDeliveryError
from .git_ops import commit_changes, ensure_diff_clean, stage_changes
from .runner import _safe_changed_paths, _validate_paths
from .store import RunStore, now_utc, task_key
from .work_order import WorkOrder

MAX_REWORK_ROUNDS = 2
MAX_DIRECTIVE_BYTES = 32 * 1024


class ReworkLimitError(AgentDeliveryError):
    """The persisted rework budget is exhausted; no further rework may start."""


def bounded_rework_directive(receipt: dict[str, Any]) -> str:
    """Render the reviewed findings verbatim as the rework directive."""
    findings = receipt.get("findings")
    if not isinstance(findings, list) or not findings:
        raise AgentDeliveryError("changes_required findings are missing; no rework directive can be built.")
    directive = "\n".join(
        f"- [{item.get('severity')}] {item.get('file')}:{item.get('line')} — {item.get('problem')} "
        f"Recommendation: {item.get('recommendation')}"
        for item in findings if isinstance(item, dict)
    )
    if not directive:
        raise AgentDeliveryError("changes_required findings are empty; no rework directive can be built.")
    encoded = directive.encode("utf-8")
    if len(encoded) > MAX_DIRECTIVE_BYTES:
        raise AgentDeliveryError("Review findings exceed the bounded rework directive size; stop instead of truncating.")
    return directive


def run_bounded_rework(
    *,
    store: RunStore,
    repo_slug: str,
    worktree: Path,
    order: WorkOrder,
    worker_config: ClaudeConfig,
    original_run_id: str,
    delivery_branch: str,
    parent_sha: str,
    rework_index: int,
    directive: str,
    rework_count_so_far: int,
) -> dict[str, Any]:
    """Run one bounded rework round; returns the new candidate head and linkage."""
    if rework_count_so_far >= MAX_REWORK_ROUNDS or not 1 <= rework_index <= MAX_REWORK_ROUNDS:
        raise ReworkLimitError(
            f"Bounded rework budget ({MAX_REWORK_ROUNDS}) is exhausted; the candidate cannot be reworked further."
        )
    store.assert_worker_available()
    key = task_key(repo_slug, order.task_id, order.revision)
    rework_id = str(uuid.uuid4())
    runtime_home = store.root / "runtime" / rework_id
    record: dict[str, Any] = {
        "schema_version": 1,
        "run_id": rework_id,
        "mode": "bounded_rework",
        "original_run_id": original_run_id,
        "rework_index": rework_index,
        "task_id": order.task_id,
        "revision": order.revision,
        "status": "starting",
        "worker_status": "start_unconfirmed",
        "started_at": now_utc(),
        "finished_at": None,
        "session_id": None,
        "completion_status": None,
        "incomplete_items": [],
        "delivery_branch": delivery_branch,
        "parent_commit": parent_sha,
        "directive_sha256": hashlib.sha256(directive.encode("utf-8")).hexdigest(),
        "changed_paths": [],
        "failure": None,
    }
    lifecycle = WorkerLifecycle()
    with store.claim(key):
        # Same pre-creation gate as the original Worker: persisted and read back.
        store.write(key, rework_id, record)
        try:
            skill_path = worktree / order.skill_ref
            if skill_path.is_symlink() or not skill_path.is_file():
                raise AgentDeliveryError("The authorized Delivery Skill is missing from the rework worktree.")
            outcome = run_claude(
                worktree, skill_path, order, runtime_home, worker_config,
                lifecycle=lifecycle, rework_directive=directive,
            )
            record["session_id"] = outcome.session_id
            record["completion_status"] = outcome.completion_status
            record["incomplete_items"] = outcome.incomplete_items
            record["status"] = "validating"
            record["worker_status"] = "stopped"
            store.write(key, rework_id, record)
            _safe_changed_paths(worktree, order)
            ensure_diff_clean(worktree)
            staged = stage_changes(worktree)
            _validate_paths(worktree, order, staged.paths)
            committed = commit_changes(worktree, f"{order.identity} rework r{rework_index}", staged)
            paths = _validate_paths(worktree, order, committed.paths)
            record["changed_paths"] = list(paths)
            record["rework_commit"] = committed.commit_sha
            record["status"] = "local_ready"
            record["finished_at"] = now_utc()
            store.write(key, rework_id, record)
            return {
                "rework_id": rework_id,
                "rework_index": rework_index,
                "session_id": outcome.session_id,
                "parent_commit": parent_sha,
                "rework_commit": committed.commit_sha,
                "changed_paths": list(paths),
            }
        except WorkerStartCancelled as exc:
            record.update({"status": "not_started", "worker_status": "not_started", "finished_at": now_utc(),
                           "completion_status": "not_started", "session_id": None, "failure": str(exc)})
            store.write(key, rework_id, record)
            raise AgentDeliveryError(f"Rework was cancelled before the Worker started. Rework ID: {rework_id}.") from None
        except WorkerCancelled as exc:
            lifecycle.status = "stopped"
            lifecycle.session_id = exc.session_id
            record.update({"status": "cancelled", "worker_status": "stopped", "finished_at": now_utc(),
                           "completion_status": "cancelled", "session_id": exc.session_id,
                           "failure": "Cancellation completed after the rework process group stopped."})
            store.write(key, rework_id, record)
            raise AgentDeliveryError(f"Rework was cancelled after cleanup. Rework ID: {rework_id}.") from None
        except WorkerCleanupError as exc:
            lifecycle.status = "stop_unconfirmed"
            lifecycle.session_id = exc.session_id
            record.update({"status": "cleanup_failed", "worker_status": "stop_unconfirmed", "finished_at": None,
                           "session_id": exc.session_id, "failure": str(exc)})
            store.write(key, rework_id, record)
            raise AgentDeliveryError(
                f"Rework cleanup failed; future runs are blocked pending manual inspection. Rework ID: {rework_id}."
            ) from None
        except KeyboardInterrupt:
            if lifecycle.status == "not_started":
                record.update({"status": "not_started", "worker_status": "not_started", "finished_at": now_utc(),
                               "completion_status": "not_started", "failure": "Cancelled before the rework Worker started."})
                store.write(key, rework_id, record)
                raise AgentDeliveryError(f"Rework cancelled before start. Rework ID: {rework_id}.") from None
            if lifecycle.status == "stopped":
                record.update({"status": "cancelled", "worker_status": "stopped", "finished_at": now_utc(),
                               "completion_status": "cancelled", "session_id": lifecycle.session_id,
                               "failure": "Run cancelled after the rework process group was confirmed stopped."})
                store.write(key, rework_id, record)
                raise AgentDeliveryError(f"Rework cancelled after cleanup. Rework ID: {rework_id}.") from None
            record.update({"status": "cleanup_failed", "worker_status": "stop_unconfirmed", "finished_at": None,
                           "session_id": lifecycle.session_id,
                           "failure": "Rework stop was not confirmed after cancellation; manual inspection is required."})
            store.write(key, rework_id, record)
            raise AgentDeliveryError(
                f"Rework stop was not confirmed; future runs are blocked. Rework ID: {rework_id}."
            ) from None
        except Exception as exc:
            if lifecycle.status not in {"not_started", "stopped"}:
                record.update({"status": "cleanup_failed", "worker_status": "stop_unconfirmed", "finished_at": None,
                               "session_id": lifecycle.session_id,
                               "failure": "An error escaped while rework Worker safety was unconfirmed; stop trials."})
                store.write(key, rework_id, record)
                raise AgentDeliveryError(
                    f"Rework Worker safety is unconfirmed; future runs remain blocked. Rework ID: {rework_id}."
                ) from None
            record.update({"status": "failed", "worker_status": lifecycle.status, "finished_at": now_utc(),
                           "session_id": lifecycle.session_id})
            if isinstance(exc, WorkerResultError):
                record["completion_status"] = exc.completion_status
                record["incomplete_items"] = exc.incomplete_items
            record["failure"] = str(exc) if isinstance(exc, AgentDeliveryError) else "Unexpected rework failure; output suppressed."
            store.write(key, rework_id, record)
            raise AgentDeliveryError(f"{record['failure']} Rework ID: {rework_id}.") from None
