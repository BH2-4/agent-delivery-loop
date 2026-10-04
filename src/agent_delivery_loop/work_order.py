"""Strict parsing and validation for the version 1 Work Order format."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .errors import AgentDeliveryError

WORK_ORDER_PATH_RE = re.compile(r"^\.agents/work-orders/WO-[A-Z0-9-]+-r[1-9][0-9]*\.json$")
TASK_ID_RE = re.compile(r"^WO-[A-Z0-9-]{3,48}$")
SKILL_REF_RE = re.compile(r"^\.agents/policies/[A-Za-z0-9._/-]+/SKILL\.md$")
ALLOWED_KEYS = {
    "schema_version",
    "task_id",
    "revision",
    "objective",
    "out_of_scope",
    "acceptance_criteria",
    "allowed_paths",
    "worker_profile",
    "skill_ref",
    "stop_conditions",
    "limits",
    "review_evidence",
}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OPTIONAL_KEYS = {"review_evidence"}


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    path: str
    ref: str
    purpose: str


@dataclass(frozen=True, slots=True)
class WorkOrder:
    schema_version: int
    task_id: str
    revision: int
    objective: str
    out_of_scope: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    allowed_paths: tuple[str, ...]
    worker_profile: str
    skill_ref: str
    stop_conditions: tuple[str, ...]
    max_turns: int
    timeout_seconds: int
    max_budget_usd: float
    review_evidence: tuple[EvidenceRef, ...]
    sha256: str

    @property
    def identity(self) -> str:
        return f"{self.task_id}-r{self.revision}"

    def allows_path(self, path: str) -> bool:
        candidate = PurePosixPath(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            return False
        for pattern in self.allowed_paths:
            if pattern.endswith("/**"):
                prefix = pattern[:-3].rstrip("/")
                if path == prefix or path.startswith(f"{prefix}/"):
                    return True
            elif path == pattern:
                return True
        return False


def _string(value: Any, name: str, *, maximum: int = 4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\0" in value:
        raise AgentDeliveryError(f"Work Order field '{name}' must be a non-empty string (max {maximum} characters).")
    return value.strip()


def _string_list(value: Any, name: str, *, minimum: int = 0, maximum: int = 30) -> tuple[str, ...]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise AgentDeliveryError(f"Work Order field '{name}' must contain {minimum} to {maximum} entries.")
    return tuple(_string(item, name, maximum=1000) for item in value)


def _safe_repo_path(value: Any, name: str, *, allow_tree_pattern: bool = False) -> str:
    path = _string(value, name, maximum=240)
    if "\\" in path or path.startswith("/") or ".." in PurePosixPath(path).parts:
        raise AgentDeliveryError(f"Work Order field '{name}' contains an unsafe repository path.")
    if "*" in path and not (allow_tree_pattern and path.endswith("/**") and path.count("*") == 2):
        raise AgentDeliveryError(f"Work Order field '{name}' only supports literal paths or directory/** patterns.")
    return path


def _evidence_entries(value: Any) -> tuple[EvidenceRef, ...]:
    """Optional pinned review evidence; repo-relative blobs at explicit full commit SHAs."""
    if value is None:
        return ()
    if not isinstance(value, list) or not 1 <= len(value) <= 20:
        raise AgentDeliveryError("Work Order review_evidence must contain 1 to 20 entries.")
    entries: list[EvidenceRef] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "ref", "purpose"}:
            raise AgentDeliveryError("Each review_evidence entry needs exactly path, ref, and purpose.")
        path = _safe_repo_path(item["path"], "review_evidence.path")
        ref = _string(item["ref"], "review_evidence.ref", maximum=40)
        if not SHA_RE.fullmatch(ref):
            raise AgentDeliveryError("review_evidence.ref must be a full lowercase commit SHA.")
        purpose = _string(item["purpose"], "review_evidence.purpose", maximum=500)
        key = f"{ref}:{path}"
        if key in seen:
            raise AgentDeliveryError("review_evidence contains a duplicate path and ref pair.")
        seen.add(key)
        entries.append(EvidenceRef(path=path, ref=ref, purpose=purpose))
    return tuple(entries)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def parse_work_order(raw: bytes, *, expected_path: str | None = None) -> WorkOrder:
    if len(raw) > 64 * 1024:
        raise AgentDeliveryError("Work Order exceeds the 64 KiB limit.")
    try:
        decoded = raw.decode("utf-8")
        payload = json.loads(decoded, object_pairs_hook=_object_without_duplicate_keys)
    except ValueError as exc:
        if str(exc) == "duplicate JSON object key":
            raise AgentDeliveryError("Work Order contains a duplicate JSON field.") from exc
        raise AgentDeliveryError("Work Order is not valid UTF-8 JSON.") from exc
    if not isinstance(payload, dict):
        raise AgentDeliveryError("Work Order must be a JSON object.")
    unknown = set(payload) - ALLOWED_KEYS
    missing = (ALLOWED_KEYS - OPTIONAL_KEYS) - set(payload)
    if unknown:
        raise AgentDeliveryError(f"Work Order contains unsupported fields: {', '.join(sorted(unknown))}.")
    if missing:
        raise AgentDeliveryError(f"Work Order is missing fields: {', '.join(sorted(missing))}.")
    if payload["schema_version"] != 1 or isinstance(payload["schema_version"], bool):
        raise AgentDeliveryError("Only Work Order schema_version 1 is supported.")
    task_id = _string(payload["task_id"], "task_id", maximum=52)
    if not TASK_ID_RE.fullmatch(task_id):
        raise AgentDeliveryError("Work Order task_id must match WO-[A-Z0-9-]{3,48}.")
    revision = payload["revision"]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise AgentDeliveryError("Work Order revision must be a positive integer.")
    objective = _string(payload["objective"], "objective")
    out_of_scope = _string_list(payload["out_of_scope"], "out_of_scope")
    acceptance = _string_list(payload["acceptance_criteria"], "acceptance_criteria", minimum=1)
    allowed_raw = payload["allowed_paths"]
    if not isinstance(allowed_raw, list) or not 1 <= len(allowed_raw) <= 100:
        raise AgentDeliveryError("Work Order allowed_paths must contain 1 to 100 entries.")
    allowed_paths = tuple(_safe_repo_path(item, "allowed_paths", allow_tree_pattern=True) for item in allowed_raw)
    if len(set(allowed_paths)) != len(allowed_paths):
        raise AgentDeliveryError("Work Order allowed_paths contains duplicate entries.")
    if payload["worker_profile"] != "claude-code-v1":
        raise AgentDeliveryError("Only the fixed worker_profile 'claude-code-v1' is supported.")
    skill_ref = _safe_repo_path(payload["skill_ref"], "skill_ref")
    if not SKILL_REF_RE.fullmatch(skill_ref):
        raise AgentDeliveryError("skill_ref must point to a SKILL.md under .agents/policies/.")
    stops = _string_list(payload["stop_conditions"], "stop_conditions", minimum=1)
    limits = payload["limits"]
    if not isinstance(limits, dict) or set(limits) != {"max_turns", "timeout_seconds", "max_budget_usd"}:
        raise AgentDeliveryError("Work Order limits must define max_turns, timeout_seconds, and max_budget_usd only.")
    max_turns = limits["max_turns"]
    timeout_seconds = limits["timeout_seconds"]
    budget = limits["max_budget_usd"]
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or not 1 <= max_turns <= 100:
        raise AgentDeliveryError("limits.max_turns must be an integer from 1 to 100.")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or not 60 <= timeout_seconds <= 14400:
        raise AgentDeliveryError("limits.timeout_seconds must be an integer from 60 to 14400.")
    if not isinstance(budget, (int, float)) or isinstance(budget, bool) or not 0 < budget <= 100:
        raise AgentDeliveryError("limits.max_budget_usd must be greater than 0 and no more than 100.")
    if expected_path is not None:
        canonical_path = f".agents/work-orders/{task_id}-r{revision}.json"
        if not WORK_ORDER_PATH_RE.fullmatch(expected_path) or expected_path != canonical_path:
            raise AgentDeliveryError("Work Order filename must match its task ID and revision.")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return WorkOrder(
        schema_version=1,
        task_id=task_id,
        revision=revision,
        objective=objective,
        out_of_scope=out_of_scope,
        acceptance_criteria=acceptance,
        allowed_paths=allowed_paths,
        worker_profile="claude-code-v1",
        skill_ref=skill_ref,
        stop_conditions=stops,
        max_turns=max_turns,
        timeout_seconds=timeout_seconds,
        max_budget_usd=float(budget),
        review_evidence=_evidence_entries(payload.get("review_evidence")),
        sha256=hashlib.sha256(canonical).hexdigest(),
    )
