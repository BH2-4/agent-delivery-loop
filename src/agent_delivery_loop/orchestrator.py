"""Deterministic single-shot delivery orchestration with bounded rework and safe resume.

Stage machine with reliable checkpoints. Every external write goes through the
intent-persisted, read-only-reconciled publish path. Reviews are classified:
pass proceeds, changes_required enters the bounded rework loop (max two rounds,
persisted across resume), blocked stops without weakening anything, and
infrastructure failures keep the captured receipt for re-verification instead of
re-running the Worker or the review model.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from .claude_worker import ClaudeConfig
from .errors import AgentDeliveryError
from .git_ops import git, repository_remote, repository_root
from .github import GitHubClient, parse_plan_pr_ref
from .github_identity import GitHubPAT
from .publish import (
    ensure_delivery_pr,
    merge_delivery_pr,
    probe_github_access,
    push_delivery_branch,
    verify_ci_current,
    wait_for_ci,
)
from .review_cli import (
    CREDENTIAL_ENV_NAMES,
    MAX_REVIEW_TIMEOUT_SECONDS,
    MIN_REVIEW_TIMEOUT_SECONDS,
    run_review_process,
)
from .review_handoff import _read_json, check_review, prepare_review, resolve_review_evidence
from .rework import MAX_REWORK_ROUNDS, ReworkLimitError, bounded_rework_directive, run_bounded_rework
from .runner import _delivery_body
from .store import RunStore, default_state_dir, task_key
from .work_order import parse_work_order

SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
MAX_AGENT_RUN_OUTPUT_BYTES = 64 * 1024
PRE_WORKER_STAGES = {"created", "install_verified", "evidence_ready"}


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
        self.output_dir.mkdir(exist_ok=True, mode=0o700)
        self.record: dict[str, Any] = {
            "schema_version": 1,
            "orchestration_id": orchestration_id,
            "mode": "single_shot_delivery",
            "stage": "created",
            "started_at": _now(),
            "finished_at": None,
            "failure": None,
            "rework": {"count": 0, "entries": []},
            "candidate": None,
        }

    def load(self) -> None:
        path = self.record_dir / f"{self.id}.json"
        record = _read_json(path)
        if not isinstance(record, dict) or record.get("orchestration_id") != self.id:
            raise AgentDeliveryError("The orchestration record is unreadable or mismatched; resume is blocked.")
        self.record = record
        self.output_dir.mkdir(exist_ok=True, mode=0o700)

    def save(self, **updates: Any) -> None:
        self.record.update(updates)
        payload = json.dumps(self.record, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        path = self.record_dir / f"{self.id}.json"
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            # A failed checkpoint must block dependent external writes, not pass silently.
            raise AgentDeliveryError("The orchestration checkpoint could not be persisted; stopping before further effects.") from exc

    def write_intent_sink(self, intent: dict[str, Any]) -> None:
        self.save(write_intent=intent)

    def fail(self, message: str) -> None:
        try:
            self.save(failure=message, finished_at=_now())
        except AgentDeliveryError:
            pass

    def finish(self, stage: str, **updates: Any) -> None:
        self.save(stage=stage, finished_at=_now(), **updates)


def _now() -> str:
    from .store import now_utc

    return now_utc()


def _run_bounded(argv: list[str], *, timeout: int, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv, text=True, capture_output=True, timeout=timeout, check=False,
            env=env if env is not None else os.environ.copy(), stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentDeliveryError(f"A required command could not run or timed out: {argv[0]}.") from exc


def verify_installation(*, receipt_path: Path, expected_source_sha: str, expected_wheel_sha256: str) -> dict[str, Any]:
    """The installed entry must match the approved source and wheel, and must not be an editable checkout."""
    import hashlib

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


class AgentRunNotStartedError(AgentDeliveryError):
    """Popen itself failed; no child process was ever created."""


AGENT_RUN_INTERRUPT_GRACE_SECONDS = 20.0


def _stop_agent_run_bounded(process: subprocess.Popen[str]) -> bool:
    """Two-layer-aware stop of the trusted Python agent-run runner.

    The runner and its inner Claude Worker live in independent process groups, so
    stopping the outer group is never proof that Claude stopped. SIGINT goes to the
    outer group first: a Python runner turns it into KeyboardInterrupt and runs its
    own Worker cleanup — including stopping and confirming the inner Claude group and
    persisting the safety record. The leader exiting is not enough: same-group
    descendants that ignore SIGINT must be caught by checking the group and
    escalating to the hard SIGTERM/SIGKILL stop. A True return confirms the OUTER
    process group is gone; whether the inner run ended safely is proven only by its
    persisted run record, which the spawn gate resolution checks separately.
    """
    from .claude_worker import _process_group_exists, _stop_process_group

    if process.poll() is not None:
        return not _process_group_exists(process.pid)
    try:
        os.killpg(process.pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        process.wait(timeout=AGENT_RUN_INTERRUPT_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return _stop_process_group(process)
    if not _process_group_exists(process.pid):
        return True
    return _stop_process_group(process)


def _spawn_gate_record(orchestration_id: str, phase: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": f"spawn-{orchestration_id[:8]}",
        "status": {"pending": "not_started", "started": "starting"}[phase],
        "worker_status": {"pending": "not_started", "started": "start_unconfirmed"}[phase],
        "kind": "orchestrator_spawn_gate",
        "phase": phase,
        "orchestration_id": orchestration_id,
        "registered_at": _now(),
    }


def _preflight_spawn_gates(store: Any) -> None:
    """Refuse to spawn while ANY unresolved spawn gate remains (any task, either phase)."""
    for directory in store.runs.iterdir():
        if not directory.is_dir():
            continue
        for path in directory.glob("spawn-*.json"):
            raise AgentDeliveryError(
                f"An unresolved agent-run spawn gate remains ({path.name}); manual safety review is required before any new run."
            )


def _parse_iso_timestamp(value: Any) -> Any:
    from datetime import datetime

    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _resolve_spawn_gate(store: Any, task_key_value: str, gate_path: Path, registered_epoch: float) -> bool:
    """Remove the gate only with trusted safe-end evidence bound to THIS spawn window.

    Three independent bindings, all required: (1) the gate's registration timestamp
    parses and the evidence record's runner-written `started_at` parses and is at or
    after it (datetime comparison — malformed values fail closed); (2) the evidence
    file's mtime lies at or after the gate's own last write (the post-Popen flip);
    (3) that mtime is not in the future beyond a small skew allowance, so a stale
    record with a clock-shifted mtime can never masquerade as this window. Evidence =
    a same-task run record with worker_status 'stopped' and a status the safety table
    accepts. Absent that, the gate stays and blocks every launch path.
    """
    try:
        window_start = max(gate_path.stat().st_mtime, registered_epoch)
        gate = _read_json(gate_path)
        registered_at = _parse_iso_timestamp(gate.get("registered_at"))
        if registered_at is None:
            return False
        now = time.time()
        for path in (store.runs / task_key_value).glob("*.json"):
            if path.name.startswith("spawn-"):
                continue
            record = _read_json(path)
            started_at = _parse_iso_timestamp(record.get("started_at"))
            mtime = path.stat().st_mtime
            if (
                record.get("worker_status") == "stopped"
                and record.get("status") in {"validating", "local_ready", "delivery_pr_open", "cancelled", "failed"}
                and started_at is not None
                and started_at >= registered_at
                and window_start <= mtime <= now + 5.0
            ):
                gate_path.unlink()
                return True
    except (OSError, ValueError):
        return False
    return False


def _spawn_agent_run(
    *, entry: str, repo_root: Path, plan_pr: str, work_order_path: str,
    model: str, base_url: str, effort: str, auth_config: Path,
    timeout_seconds: int, proxy: str | None, stdout_path: Path,
    identity: GitHubPAT, store: Any, task_key_value: str, gate_path: Path,
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
    # One-shot credential channel for the trusted agent-run child only: the exact value of
    # this orchestrator's in-memory PAT snapshot, so parent and child can never diverge onto
    # different tokens. The variable never enters argv or a file, the names are on every
    # credential scrub list, the child pops it before any model subprocess exists, and the
    # channel disappears with the child process on success, failure, timeout or cancel.
    repo_slug = parse_plan_pr_ref(plan_pr)[0].slug
    env["AGENT_DELIVERY_PAT"] = identity.token_for(repo_slug)
    env["AGENT_DELIVERY_PAT_LOGIN"] = identity.expected_login
    # The child is told which durable spawn gate tracks it so its own availability
    # check can exempt exactly that one record (name only; not a credential).
    env["AGENT_DELIVERY_SPAWN_GATE"] = f"{task_key_value}/{gate_path.name}"
    # A stdout-capture open failure also proves no child was created; both it and a
    # Popen failure convert to AgentRunNotStartedError so the caller removes the gate.
    # Every later OSError is an unknown outcome and keeps the gate.
    try:
        captured = open(stdout_path, "wb")
    except OSError as exc:
        raise AgentRunNotStartedError("The agent-run output capture could not be opened.") from exc
    with captured:
        try:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=captured, stderr=subprocess.STDOUT,
                env=env, start_new_session=True,
            )
        except OSError as exc:
            raise AgentRunNotStartedError("The installed agent-run entry could not be started.") from exc
        # The handle exists: flip the durable gate to the blocking phase before the
        # child can do real work, so every later launch sees an unconfirmed spawn.
        # If the flip cannot be persisted, stop the child and leave the pending gate.
        try:
            store.write(task_key_value, gate_path.stem, _spawn_gate_record(
                gate_path.stem.removeprefix("spawn-"), "started",
            ))
        except BaseException:
            _stop_agent_run_bounded(process)
            raise
        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            if not _stop_agent_run_bounded(process):
                raise AgentDeliveryError(
                    "The spawned agent-run exceeded its time limit and its process group could not be confirmed stopped; runs are blocked."
                )
            raise AgentDeliveryError("The spawned agent-run exceeded its time limit and was stopped; no delivery continued.")
        except BaseException:
            # Interruption or cancellation must not leave the trusted child (and its
            # one-shot PAT channel) running unconfirmed. A confirmed outer stop
            # re-raises the interruption; an unconfirmed stop blocks future runs.
            if not _stop_agent_run_bounded(process):
                raise AgentDeliveryError(
                    "agent-run was interrupted and its process group could not be confirmed stopped; runs are blocked."
                ) from None
            raise
    output = stdout_path.read_bytes()[:MAX_AGENT_RUN_OUTPUT_BYTES]
    if exit_code != 0:
        raise AgentDeliveryError(f"The installed agent-run exited with code {exit_code}; its sanitized output was preserved.")
    try:
        result = json.loads(output.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError) as exc:
        raise AgentDeliveryError("agent-run output was not readable JSON; no delivery continued.") from exc
    result["agent_run_exit_code"] = exit_code
    return result


def _proxy_hygiene(proxy: str | None) -> None:
    if not proxy:
        return
    for legacy in ("ALL_PROXY", "all_proxy"):
        os.environ.pop(legacy, None)
    os.environ.update({"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy})


def _worker_record_path(repo_slug: str, order_task_id: str, revision: int, run_id: str) -> Path:
    return default_state_dir() / "runs" / task_key(repo_slug, order_task_id, revision) / f"{run_id}.json"


def _verify_worker_result(record_path: Path, run_id: str, delivery_commit: str, order) -> dict[str, Any]:
    record = _read_json(record_path)
    if (
        record.get("run_id") != run_id or record.get("worker_status") != "stopped"
        or record.get("status") != "local_ready" or record.get("completion_status") != "complete"
        or record.get("failure") is not None or record.get("delivery_commit") != delivery_commit
    ):
        raise AgentDeliveryError("The run record does not prove a stopped Worker with a completed local candidate.")
    for path in record.get("changed_paths", []):
        if not order.allows_path(path):
            raise AgentDeliveryError(f"Changed path is outside the authorized Work Order scope: {path}.")
    return record


class _Params:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def _load_pat(repo_path: Path, plan_pr: str, pat_file: Path, login: str) -> GitHubPAT:
    root = repository_root(repo_path.expanduser().resolve())
    repo, _, _ = parse_plan_pr_ref(plan_pr)
    if repository_remote(root).slug.casefold() != repo.slug.casefold():
        raise AgentDeliveryError("The Plan PR and local origin must identify the same repository before loading PAT.")
    return GitHubPAT.from_file(path=pat_file, repo_root=root, repository=repo.slug, expected_login=login)


def deliver(
    *,
    repo_path: Path, plan_pr: str, work_order_path: str,
    model: str, base_url: str, effort: str, auth_config: Path,
    install_receipt: Path, expected_source_sha: str, expected_wheel_sha256: str,
    review_model: str, review_effort: str, review_timeout_seconds: int,
    review_bundle_dir: Path, proxy: str | None = None,
    ci_timeout_seconds: int = 900, auto_merge: bool = False,
    github_pat_file: Path, github_login: str,
) -> dict[str, Any]:
    if not MIN_REVIEW_TIMEOUT_SECONDS <= review_timeout_seconds <= MAX_REVIEW_TIMEOUT_SECONDS:
        raise AgentDeliveryError("Review timeout is out of the allowed range.")
    identity = _load_pat(repo_path, plan_pr, github_pat_file, github_login)
    orchestration = Orchestration(str(uuid.uuid4()))
    params = _Params(
        repo_path=repo_path, plan_pr=plan_pr, work_order_path=work_order_path,
        model=model, base_url=base_url, effort=effort, auth_config=auth_config,
        install_receipt=install_receipt, expected_source_sha=expected_source_sha,
        expected_wheel_sha256=expected_wheel_sha256, review_model=review_model,
        review_effort=review_effort, review_timeout_seconds=review_timeout_seconds,
        review_bundle_dir=review_bundle_dir, proxy=proxy,
        ci_timeout_seconds=ci_timeout_seconds, auto_merge=auto_merge,
        github_identity=identity,
    )
    orchestration.save(
        parameters={
            "plan_pr": plan_pr, "work_order_path": work_order_path,
            "expected_source_sha": expected_source_sha, "expected_wheel_sha256": expected_wheel_sha256,
            "review_model": review_model, "review_effort": review_effort,
            "review_timeout_seconds": review_timeout_seconds,
            "review_bundle_dir": str(review_bundle_dir), "proxy": proxy,
            "ci_timeout_seconds": ci_timeout_seconds, "auto_merge": auto_merge,
            "model": model, "base_url": base_url, "effort": effort,
            "auth_config": str(auth_config), "repo_path": str(repo_path),
            "install_receipt": str(install_receipt),
            "github_identity": identity.metadata(),
        }
    )
    return _continue(orchestration, params, fresh=True)


def resume(
    *,
    orchestration_id: str,
    repo_path: Path, plan_pr: str, work_order_path: str,
    model: str, base_url: str, effort: str, auth_config: Path,
    install_receipt: Path, expected_source_sha: str, expected_wheel_sha256: str,
    review_model: str, review_effort: str, review_timeout_seconds: int,
    review_bundle_dir: Path, proxy: str | None = None,
    ci_timeout_seconds: int = 900, auto_merge: bool = False,
    github_pat_file: Path, github_login: str,
) -> dict[str, Any]:
    if not MIN_REVIEW_TIMEOUT_SECONDS <= review_timeout_seconds <= MAX_REVIEW_TIMEOUT_SECONDS:
        raise AgentDeliveryError("Review timeout is out of the allowed range.")
    orchestration = Orchestration(orchestration_id)
    orchestration.load()
    if orchestration.record.get("mode") != "single_shot_delivery":
        raise AgentDeliveryError("The record does not belong to a single-shot delivery orchestration.")
    if orchestration.record.get("finished_at") and orchestration.record.get("stage") == "completed":
        raise AgentDeliveryError("This orchestration already completed; start a new one instead of resuming.")
    if orchestration.record.get("stage") in PRE_WORKER_STAGES:
        raise AgentDeliveryError(
            "No Worker result exists yet for this orchestration; run a fresh deliver instead of resuming."
        )
    recorded = orchestration.record.get("parameters") or {}
    strict_keys = (
        ("plan_pr", plan_pr), ("work_order_path", work_order_path),
        ("expected_source_sha", expected_source_sha), ("expected_wheel_sha256", expected_wheel_sha256),
    )
    advisory_keys = (
        ("review_model", review_model), ("review_effort", review_effort),
        ("review_timeout_seconds", review_timeout_seconds), ("ci_timeout_seconds", ci_timeout_seconds),
        ("auto_merge", auto_merge),
    )
    for key, expected in strict_keys:
        if recorded.get(key) != expected:
            raise AgentDeliveryError(f"Resume parameter mismatch for '{key}'; the recorded orchestration binds different values.")
    for key, expected in advisory_keys:
        if key in recorded and recorded[key] != expected:
            raise AgentDeliveryError(f"Resume parameter mismatch for '{key}'; the recorded orchestration binds different values.")
    repo, _, _ = parse_plan_pr_ref(plan_pr)
    expected_identity = {"kind": "fine_grained_pat", "repository": repo.slug, "expected_login": github_login}
    if recorded.get("github_identity") != expected_identity:
        raise AgentDeliveryError(
            "Resume PAT identity is missing or mismatched; legacy personal-gh records cannot be silently migrated."
        )
    identity = _load_pat(repo_path, plan_pr, github_pat_file, github_login)
    params = _Params(
        repo_path=repo_path, plan_pr=plan_pr, work_order_path=work_order_path,
        model=model, base_url=base_url, effort=effort, auth_config=auth_config,
        install_receipt=install_receipt, expected_source_sha=expected_source_sha,
        expected_wheel_sha256=expected_wheel_sha256, review_model=review_model,
        review_effort=review_effort, review_timeout_seconds=review_timeout_seconds,
        review_bundle_dir=review_bundle_dir, proxy=proxy,
        ci_timeout_seconds=ci_timeout_seconds, auto_merge=auto_merge,
        github_identity=identity,
    )
    orchestration.save(stage=orchestration.record["stage"], resumed_at=_now(), failure=None)
    return _continue(orchestration, params, fresh=False)


def _continue(orchestration: Orchestration, params: _Params, *, fresh: bool) -> dict[str, Any]:
    try:
        return _continue_inner(orchestration, params, fresh=fresh)
    except AgentDeliveryError as exc:
        orchestration.fail(str(exc))
        raise
    except Exception as exc:
        orchestration.fail("Unexpected orchestration failure; sensitive output suppressed.")
        raise AgentDeliveryError("Unexpected orchestration failure; sensitive output suppressed.") from exc


def _continue_inner(orchestration: Orchestration, params: _Params, *, fresh: bool) -> dict[str, Any]:
    # Stage 1: provenance of the running entry (re-verified on every resume).
    install = verify_installation(
        receipt_path=params.install_receipt, expected_source_sha=params.expected_source_sha,
        expected_wheel_sha256=params.expected_wheel_sha256,
    )
    orchestration.save(stage="install_verified", install=install)
    _proxy_hygiene(params.proxy)

    root = repository_root(params.repo_path.expanduser().resolve())
    local_repo = repository_remote(root)
    plan_repo, number, plan_url = parse_plan_pr_ref(params.plan_pr)
    if local_repo.slug.casefold() != plan_repo.slug.casefold():
        raise AgentDeliveryError("The Plan PR and the local repository origin must refer to the same repository.")
    identity = params.github_identity
    identity.token_for(plan_repo.slug)
    verified_identity = probe_github_access(identity, pr_number=number, proxy=params.proxy)
    orchestration.save(github_identity=verified_identity)
    client = GitHubClient(plan_repo, token=identity.token_for(plan_repo.slug))
    authorization = client.authorized_plan(number, params.work_order_path, plan_url)
    order = parse_work_order(authorization.order_bytes, expected_path=authorization.order_path)
    git(root, "fetch", "--no-tags", plan_repo.https_url, "+refs/heads/main:refs/remotes/adl-main")
    evidence_ready = resolve_review_evidence(root, order.review_evidence)
    orchestration.save(
        stage="evidence_ready",
        authorization={
            "plan_pr": plan_url, "plan_merge_sha": authorization.merge_sha,
            "work_order_path": authorization.order_path, "work_order_sha256": order.sha256,
            "task_id": order.task_id, "revision": order.revision,
        },
        evidence_ready={
            "entries": [{key: item[key] for key in ("path", "ref", "sha256")} for item in evidence_ready],
            "total_evidence_bytes": sum(item["bytes"] for item in evidence_ready),
        },
    )

    store = RunStore(default_state_dir())
    worktree = None
    if fresh:
        task_key_value = task_key(plan_repo.slug, order.task_id, order.revision)
        # Durable, read-back-verified gate BEFORE any child exists; a later launch on
        # this host sees it and refuses until trusted safe-end evidence resolves it.
        store.assert_worker_available()
        _preflight_spawn_gates(store)
        gate_path = store.record_path(task_key_value, f"spawn-{orchestration.id[:8]}")
        store.write(task_key_value, gate_path.stem, _spawn_gate_record(orchestration.id, "pending"))
        gate_registered_epoch = gate_path.stat().st_mtime
        try:
            agent_run_output = _spawn_agent_run(
                entry=install["agent_run_entry"], repo_root=root, plan_pr=params.plan_pr,
                work_order_path=params.work_order_path, model=params.model, base_url=params.base_url,
                effort=params.effort, auth_config=params.auth_config,
                timeout_seconds=order.timeout_seconds + 300, proxy=params.proxy,
                stdout_path=orchestration.output_dir / "agent-run-stdout.json",
                identity=identity, store=store, task_key_value=task_key_value, gate_path=gate_path,
            )
        except AgentRunNotStartedError:
            gate_path.unlink(missing_ok=True)  # Popen failed; no child was ever created.
            raise
        except BaseException:
            # Unknown safe-end: keep the gate unless this spawn window produced a
            # trusted 'stopped' run record (e.g. the runner finished its own cleanup).
            _resolve_spawn_gate(store, task_key_value, gate_path, gate_registered_epoch)
            raise
        run_id = agent_run_output.get("run_id")
        delivery_branch = agent_run_output.get("delivery_branch")
        delivery_commit = agent_run_output.get("delivery_commit")
        if not isinstance(run_id, str) or not isinstance(delivery_branch, str) or not SHA_RE.fullmatch(str(delivery_commit)):
            _resolve_spawn_gate(store, task_key_value, gate_path, gate_registered_epoch)
            raise AgentDeliveryError("agent-run did not report a run ID, delivery branch, and full delivery commit.")
        record_path = _worker_record_path(plan_repo.slug, order.task_id, order.revision, run_id)
        worker_record = _verify_worker_result(record_path, run_id, str(delivery_commit), order)
        _resolve_spawn_gate(store, task_key_value, gate_path, gate_registered_epoch)
        worktree = store.root / "worktrees" / run_id
        orchestration.save(
            stage="worker_completed",
            worker={
                "run_id": run_id, "session_id": worker_record.get("session_id"),
                "agent_run_exit_code": agent_run_output["agent_run_exit_code"],
                "delivery_branch": delivery_branch, "worker_commit": delivery_commit,
                "changed_paths": worker_record.get("changed_paths"),
                "record_path": str(record_path),
            },
            candidate={"sha": delivery_commit, "origin": "worker", "review_round": 0},
        )
    else:
        worker = orchestration.record.get("worker")
        candidate = orchestration.record.get("candidate")
        if not isinstance(worker, dict) or not isinstance(candidate, dict) or not SHA_RE.fullmatch(str(candidate.get("sha", ""))):
            raise AgentDeliveryError("The resumed record lacks a usable Worker result and candidate; resume is blocked.")
        run_id = worker["run_id"]
        delivery_branch = worker["delivery_branch"]
        record_path = Path(worker["record_path"])
        worker_record = _verify_worker_result(record_path, run_id, worker["worker_commit"], order)
        worktree = store.root / "worktrees" / run_id
        delivery_commit = candidate["sha"]

    # Review / rework loop: candidate is always the current local branch head.
    review_state = _review_loop(
        orchestration, params, root, plan_repo, order, authorization, record_path, worktree, delivery_branch,
        skill_sha256=worker_record.get("skill_sha256"), identity=identity,
    )
    candidate = review_state["candidate"]

    # Publish chain: intent-persisted writes, read-only reconciliation.
    push_delivery_branch(
        worktree=worktree, repo_slug=plan_repo.slug, repo_url=plan_repo.https_url,
        branch=delivery_branch, candidate_sha=candidate,
        intent_sink=orchestration.write_intent_sink,
        identity=identity, proxy=params.proxy,
    )
    orchestration.save(stage="pushed", pushed={"branch": delivery_branch, "head": candidate})
    body_path = orchestration.output_dir / "delivery-pr-body.md"
    rework_entries = orchestration.record.get("rework", {}).get("entries", [])
    body_path.write_text(
        _delivery_body(order, plan_url, authorization.merge_sha, run_id, worker_record.get("skill_sha256", ""), _changed(record_path, candidate, orchestration))
        + "".join(
            f"\n- Bounded rework r{entry['rework_index']}: `{entry['rework_commit']}` (session `{entry['session_id']}`)\n"
            for entry in rework_entries
        )
        + "\n\nOrchestrated single-shot delivery; review and CI gates verified before merge.\n",
        encoding="utf-8",
    )
    pr = ensure_delivery_pr(
        repo_slug=plan_repo.slug, branch=delivery_branch, candidate_sha=candidate,
        title=f"Delivery: {order.task_id} r{order.revision}", body_file=body_path,
        intent_sink=orchestration.write_intent_sink, identity=identity, proxy=params.proxy,
    )
    orchestration.save(stage="pr_open", delivery_pr={**pr, "head": candidate})
    ci = wait_for_ci(
        repo_slug=plan_repo.slug, pr_number=pr["number"], head_sha=candidate,
        timeout_seconds=params.ci_timeout_seconds, identity=identity, proxy=params.proxy,
    )
    orchestration.save(stage="ci_passed", ci=ci)
    merge = {"merged": False, "merge_sha": None, "main_contains_head": False}
    if params.auto_merge:
        # Re-verify the fixed CI for this exact head immediately before merging so a
        # newer run/attempt cannot hide behind the earlier wait's success.
        pre_merge_ci = verify_ci_current(
            params.github_identity, repo_slug=plan_repo.slug, pr_number=pr["number"],
            head_sha=candidate, proxy=params.proxy,
        )
        orchestration.save(stage="pre_merge_ci_verified", pre_merge_ci=pre_merge_ci)
        merge = merge_delivery_pr(
            repo_slug=plan_repo.slug, pr_number=pr["number"], candidate_sha=candidate,
            intent_sink=orchestration.write_intent_sink, identity=identity, proxy=params.proxy,
        )
        git(root, "fetch", "--no-tags", plan_repo.https_url, "refs/heads/main:refs/remotes/adl-main")
        if git(root, "merge-base", "--is-ancestor", candidate, "refs/remotes/adl-main", check=False).returncode != 0:
            raise AgentDeliveryError("Merged PR head is not reachable from the fetched main; verify on GitHub.")
        merge["main_contains_head"] = True
    result = orchestration.finish(
        "completed" if merge.get("merged") else "awaiting_user_merge",
        delivery_pr={**pr, "head": candidate}, merge=merge, auto_merge_requested=params.auto_merge,
    )
    return {**orchestration.record, "orchestration_id": orchestration.id}


def _changed(record_path: Path, candidate: str, orchestration: Orchestration) -> list[str]:
    paths: list[str] = list(_read_json(record_path).get("changed_paths", []))
    for entry in orchestration.record.get("rework", {}).get("entries", []):
        for path in entry.get("changed_paths", []):
            if path not in paths:
                paths.append(path)
    return paths


def _review_loop(
    orchestration: Orchestration, params: _Params, root: Path, plan_repo, order,
    authorization, record_path: Path, worktree: Path, delivery_branch: str,
    skill_sha256: str | None = None, *, identity: GitHubPAT | None = None,
) -> dict[str, Any]:
    """Run reviews until pass, bounded rework in between; never re-runs the Worker.

    The trusted Python packet preparation and receipt verification read GitHub with the
    orchestrator's explicit identity when one is bound; the review model subprocess
    never receives it (the packet carries only pinned public content).
    """
    from .store import RunStore as _RunStore

    store = _RunStore(default_state_dir())
    round_index = int((orchestration.record.get("candidate") or {}).get("review_round", 0))
    while True:
        candidate = orchestration.record["candidate"]["sha"]
        branch_head = git(worktree, "rev-parse", "--verify", f"refs/heads/{delivery_branch}").stdout.strip()
        if branch_head != candidate:
            raise AgentDeliveryError(
                f"Local Delivery branch head {branch_head} does not match the tracked candidate {candidate}."
            )
        bundle_dir = params.review_bundle_dir.with_name(params.review_bundle_dir.name + ("" if round_index == 0 else f"-r{round_index}"))
        bump = round_index
        while bundle_dir.exists():  # Never overwrite; a resumed round moves to a fresh bundle.
            bump += 1
            bundle_dir = params.review_bundle_dir.with_name(params.review_bundle_dir.name + f"-r{bump}")
        prior = orchestration.record.get("review") or {}
        reusable = (
            prior.get("candidate") == candidate
            and prior.get("review_round") == round_index
            and prior.get("verdict") in {"pass", "changes_required", "blocked"}
            and Path(str(prior.get("result_path", ""))).is_file()
        )
        if reusable:
            # A completed review for THIS candidate and round is never re-rolled on resume,
            # whatever stage the orchestration stopped at: pass re-verifies the same receipt,
            # changes_required proceeds to the rework decision, blocked keeps stopping.
            if prior.get("bundle_dir"):
                bundle_dir = Path(prior["bundle_dir"])
            review_run = {
                "review_id": prior["review_id"], "codex_version": prior.get("codex_version"),
                "exit_code": prior["exit_code"], "stop_status": prior["stop_status"],
                "result_path": prior["result_path"],
            }
        else:
            prepare_review(
                output_dir=bundle_dir, repo_path=root, plan_pr=params.plan_pr,
                work_order_path=params.work_order_path, run_record=record_path, head_sha=candidate,
                pat_identity=identity,
            )
            review_run = run_review_process(
                bundle_dir=bundle_dir, timeout_seconds=params.review_timeout_seconds,
                review_model=params.review_model, review_effort=params.review_effort, proxy=params.proxy,
            )
            review_run = {key: review_run[key] for key in ("review_id", "codex_version", "exit_code", "stop_status", "result_path")}
        linkage = {
            **review_run, "review_round": round_index, "candidate": candidate, "verdict": None,
            "bundle_dir": str(bundle_dir),
        }
        receipt = _read_json(Path(review_run["result_path"]))
        verdict = receipt.get("verdict")
        linkage["verdict"] = verdict
        orchestration.save(stage="review_completed_pending_check", review=linkage)
        if verdict == "pass":
            check_review(
                bundle_dir=bundle_dir, result_path=Path(review_run["result_path"]),
                review_exit_code=review_run["exit_code"],
                repo_path=root, plan_pr=params.plan_pr, work_order_path=params.work_order_path,
                run_record=record_path, head_sha=candidate,
                exit_code_source="captured_by_orchestrator",
                pat_identity=identity,
            )
            orchestration.save(stage="review_passed", review={**linkage, "verdict": "pass"})
            return {"candidate": candidate}
        if verdict == "changes_required":
            rework_state = orchestration.record.get("rework") or {"count": 0, "entries": []}
            if rework_state["count"] >= MAX_REWORK_ROUNDS:
                raise ReworkLimitError(
                    "The review requested changes but the bounded rework budget (2) is exhausted; stopping without merging."
                )
            directive = bounded_rework_directive(receipt)
            rework_index = rework_state["count"] + 1
            orchestration.save(
                stage=f"rework_r{rework_index}_started",
                review={**linkage, "verdict": "changes_required"},
                rework={**rework_state, "count": rework_index},
            )
            outcome = run_bounded_rework(
                store=store, repo_slug=plan_repo.slug, worktree=worktree, order=order,
                expected_skill_sha256=skill_sha256,
                worker_config=ClaudeConfig.from_explicit(
                    model=params.model, base_url=params.base_url, effort=params.effort,
                    auth_config=params.auth_config, repo_root=root,
                ),
                original_run_id=orchestration.record["worker"]["run_id"],
                delivery_branch=delivery_branch, parent_sha=candidate,
                rework_index=rework_index, directive=directive,
                rework_count_so_far=rework_state["count"],
            )
            entries = orchestration.record["rework"]["entries"]
            entries.append({
                "rework_index": rework_index, "rework_id": outcome["rework_id"],
                "session_id": outcome["session_id"], "parent_commit": outcome["parent_commit"],
                "rework_commit": outcome["rework_commit"], "changed_paths": outcome["changed_paths"],
            })
            orchestration.save(
                stage=f"rework_r{rework_index}_completed",
                rework={"count": rework_index, "entries": entries},
                candidate={"sha": outcome["rework_commit"], "origin": "rework", "review_round": round_index + 1},
            )
            round_index += 1
            continue
        if verdict == "blocked":
            orchestration.save(stage="review_blocked", review={**linkage, "verdict": "blocked"})
            raise AgentDeliveryError(
                "The independent review returned blocked: evidence, authorization, or infrastructure is insufficient. "
                "No rework is run on blocked; inspect the receipt and stop."
            )
        raise AgentDeliveryError(f"The review receipt carried an unknown verdict: {verdict!r}.")
