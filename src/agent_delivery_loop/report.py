"""Offline sanitized summary of one stored orchestration record.

Read-only by construction: the state directory location is shared with store.py,
but no store, lock, or orchestration object is ever instantiated, so producing a
report cannot create directories, change permissions, start a model, or touch
credentials. Only the fixed inline whitelist below is ever emitted; blockage is
expressed solely through the recorded stage, never inferred from free text.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any

from .errors import AgentDeliveryError
from .store import default_state_dir

# A real record holds identities, refs, and bounded path lists (a few KiB); the
# limit only exists to reject hostile growth before parsing.
MAX_ORCHESTRATION_RECORD_BYTES = 256 * 1024
SHA_RE = re.compile(r"[0-9a-f]{40}")
REPORT_FIELDS = (
    "orchestration_id",
    "task",
    "revision",
    "stage",
    "run_id",
    "session_id",
    "delivery_commit",
    "delivery_pr",
)
# The complete stage vocabulary of the sole record writer (orchestrator.py): its
# fixed checkpoint literals in pipeline order, plus the bounded rework loop's
# rework_r{1..MAX_REWORK_ROUNDS}_{started,completed} stages (rework.py caps the
# budget at 2, so the set is finite). Any other stage value is a structural
# error: free text must never be echoed into a summary.
KNOWN_STAGES = frozenset({
    "created",
    "install_verified",
    "evidence_ready",
    "worker_completed",
    "pushed",
    "pr_open",
    "ci_passed",
    "pre_merge_ci_verified",
    "review_completed_pending_check",
    "review_passed",
    "review_blocked",
    "rework_r1_started",
    "rework_r1_completed",
    "rework_r2_started",
    "rework_r2_completed",
    "awaiting_user_merge",
    "completed",
})


class ReportError(AgentDeliveryError):
    """Fixed, sanitized classifications for report input problems."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate field")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError("Non-finite JSON constant")


def _canonical_uuid(value: Any) -> str:
    if not isinstance(value, str):
        raise ReportError("A report identity is not a canonical UUID.")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise ReportError("A report identity is not a canonical UUID.") from None
    if str(parsed) != value:
        raise ReportError("A report identity is not a canonical UUID.")
    return value


def _optional_mapping(record: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ReportError("The orchestration record has an unexpected structure.")
    return value


def _unexpected() -> ReportError:
    return ReportError("The orchestration record has an unexpected structure.")


def _read_record(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        raise ReportError("The orchestration record was not found.") from None
    except OSError:
        raise ReportError("The orchestration record could not be opened.") from None
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_ORCHESTRATION_RECORD_BYTES:
            raise ReportError("The orchestration record is not a bounded regular file.")
        raw = stream.read(MAX_ORCHESTRATION_RECORD_BYTES + 1)
        if len(raw) > MAX_ORCHESTRATION_RECORD_BYTES:
            raise ReportError("The orchestration record is not a bounded regular file.")
        return raw


def build_report(orchestration_id: str, *, state_dir: Path | None = None) -> dict[str, Any]:
    """Return the whitelisted summary of one orchestration record; never writes state.

    Missing optional sections map to null; a record whose whitelisted fields carry
    unexpected types is rejected rather than guessed at. A missing PR field says
    nothing about the remote repository: this function is fully offline.
    """
    identifier = _canonical_uuid(orchestration_id)
    directory = (default_state_dir() if state_dir is None else state_dir).expanduser()
    raw = _read_record(directory / "orchestrations" / f"{identifier}.json")
    try:
        record = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (UnicodeError, ValueError):
        raise ReportError("The orchestration record is not valid JSON.") from None
    if not isinstance(record, dict):
        raise ReportError("The orchestration record is not a JSON object.")
    if record.get("orchestration_id") != identifier:
        raise ReportError("The orchestration record does not match the requested orchestration ID.")
    stage = record.get("stage")
    if not isinstance(stage, str) or stage not in KNOWN_STAGES:
        raise _unexpected()
    task: str | None = None
    revision: int | None = None
    authorization = _optional_mapping(record, "authorization")
    if authorization is not None:
        task = authorization.get("task_id")
        revision = authorization.get("revision")
        if task is not None and not isinstance(task, str):
            raise _unexpected()
        if revision is not None and (not isinstance(revision, int) or isinstance(revision, bool)):
            raise _unexpected()
    run_id: str | None = None
    session_id: str | None = None
    worker = _optional_mapping(record, "worker")
    if worker is not None:
        run_id = worker.get("run_id")
        session_id = worker.get("session_id")
        if run_id is not None:
            run_id = _canonical_uuid(run_id)
        if session_id is not None:
            session_id = _canonical_uuid(session_id)
    delivery_commit: str | None = None
    delivery_number: int | None = None
    delivery = _optional_mapping(record, "delivery_pr")
    if delivery is not None:
        delivery_commit = delivery.get("head")
        delivery_number = delivery.get("number")
        if delivery_commit is not None and (
            not isinstance(delivery_commit, str) or not SHA_RE.fullmatch(delivery_commit)
        ):
            raise _unexpected()
        if delivery_number is not None and (
            not isinstance(delivery_number, int) or isinstance(delivery_number, bool)
        ):
            raise _unexpected()
    return {
        "orchestration_id": identifier,
        "task": task,
        "revision": revision,
        "stage": stage,
        "run_id": run_id,
        "session_id": session_id,
        "delivery_commit": delivery_commit,
        "delivery_pr": delivery_number,
    }
