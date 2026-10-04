"""Launch and capture one real, read-only review CLI process; never trust operator attestation."""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from .claude_worker import _process_group_exists, _stop_process_group
from .errors import AgentDeliveryError
from .review_handoff import MAX_CONTEXT_BYTES, _read_bytes, _read_json, _unique_object
from .store import default_state_dir, now_utc

MIN_REVIEW_TIMEOUT_SECONDS = 60
MAX_REVIEW_TIMEOUT_SECONDS = 3600
MAX_CAPTURED_OUTPUT_BYTES = 64 * 1024
REVIEW_BUNDLE_FILES = ("context.md", "review.schema.json", "prompt.txt", "request.json")
CREDENTIAL_ENV_NAMES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GIT_ASKPASS",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY",
)


class ReviewGateError(AgentDeliveryError):
    """A prior review process was not confirmed stopped; no new review may start."""


def _review_records_dir() -> Path:
    directory = default_state_dir() / "reviews"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def _write_record(path: Path, record: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise AgentDeliveryError("The review process record could not be persisted; stop trials.") from exc
    if json.loads(path.read_text(encoding="utf-8")) != record:
        raise AgentDeliveryError("The review process record failed read-back; stop trials.")


def _read_record(path: Path) -> dict[str, Any]:
    raw = _read_bytes(path, 64 * 1024)
    record = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(record, dict) or record.get("review_id") != path.stem:
        raise ValueError("Invalid review record")
    return record


def assert_review_processes_settled() -> None:
    """Block a new review while any earlier review process is not confirmed stopped."""
    directory = _review_records_dir()
    try:
        for path in sorted(directory.iterdir()):
            if path.suffix != ".json":
                continue
            record = _read_record(path)
            if record.get("stop_status") != "confirmed_stopped":
                raise ReviewGateError(
                    "A prior review CLI process was not confirmed stopped; new reviews are blocked pending manual review."
                )
    except (OSError, ValueError, AgentDeliveryError) as exc:
        raise ReviewGateError("Review process records are unreadable; new reviews are blocked.") from exc


def _codex_version(binary: str) -> str:
    scrubbed = {key: value for key, value in os.environ.items() if key not in CREDENTIAL_ENV_NAMES}
    try:
        result = subprocess.run(
            [binary, "--version"], text=True, capture_output=True, timeout=30, check=False,
            env=scrubbed, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentDeliveryError("The review CLI could not be started for a version probe.") from exc
    version = result.stdout.strip() or result.stderr.strip()
    if result.returncode != 0 or not version:
        raise AgentDeliveryError("The review CLI version probe failed; no review was started.")
    return version[:200]


def run_review_process(
    *,
    bundle_dir: Path,
    timeout_seconds: int,
    review_model: str,
    review_effort: str,
    codex_binary: str = "codex",
    proxy: str | None = None,
) -> dict[str, Any]:
    """Start one fresh, read-only, non-resuming review process and capture its real result."""
    if not MIN_REVIEW_TIMEOUT_SECONDS <= timeout_seconds <= MAX_REVIEW_TIMEOUT_SECONDS:
        raise AgentDeliveryError(f"--review-timeout must be {MIN_REVIEW_TIMEOUT_SECONDS} to {MAX_REVIEW_TIMEOUT_SECONDS} seconds.")
    bundle = bundle_dir.expanduser().resolve()
    for filename in REVIEW_BUNDLE_FILES:
        _read_bytes(bundle / filename, MAX_CONTEXT_BYTES)
    prompt = (bundle / "prompt.txt").read_bytes().decode("utf-8")
    request = _read_json(bundle / "request.json")
    version = _codex_version(codex_binary)

    assert_review_processes_settled()
    review_id = str(uuid.uuid4())
    record_dir = _review_records_dir()
    record_path = record_dir / f"{review_id}.json"
    output_dir = record_dir / f"{review_id}.output"
    output_dir.mkdir(mode=0o700)
    result_path = output_dir / "review-result.json"
    stdout_path = output_dir / "captured-stdout.txt"
    record: dict[str, Any] = {
        "schema_version": 1,
        "review_id": review_id,
        "mode": "read_only_review_cli",
        "codex_version": version,
        "review_model": review_model,
        "review_effort": review_effort,
        "base_sha": request.get("base_sha"),
        "head_sha": request.get("head_sha"),
        "context_sha256": request.get("context_sha256"),
        "stop_status": "start_unconfirmed",
        "started_at": now_utc(),
        "finished_at": None,
        "exit_code": None,
        "failure": None,
    }
    _write_record(record_path, record)

    child_env = {key: value for key, value in os.environ.items() if key not in CREDENTIAL_ENV_NAMES}
    if proxy:
        child_env.update({"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy})
    argv = [
        codex_binary, "exec",
        "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
        "--sandbox", "read-only",
        "--cd", str(bundle),
        "--model", review_model,
        "--config", f'model_reasoning_effort="{review_effort}"',
        "--color", "never",
        "--output-schema", str(bundle / "review.schema.json"),
        "--output-last-message", str(result_path),
        prompt,
    ]
    process: subprocess.Popen[bytes] | None = None
    timed_out = False
    interrupted = False
    try:
        with open(stdout_path, "wb") as captured:
            process = subprocess.Popen(
                argv, stdin=subprocess.DEVNULL, stdout=captured, stderr=subprocess.STDOUT,
                env=child_env, start_new_session=True,
            )
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
            except KeyboardInterrupt:
                interrupted = True
    except OSError as exc:
        # No process handle: conservatively treat the stop as unconfirmed.
        record.update({"stop_status": "stop_unconfirmed", "failure": "The review CLI could not be spawned.", "finished_at": None})
        _write_record(record_path, record)
        raise AgentDeliveryError("The review CLI could not be spawned; its stop is unconfirmed and reviews are blocked.") from exc
    finally:
        _truncate_if_needed(stdout_path)

    if timed_out or interrupted:
        stopped = process is None or not _process_group_exists(process.pid) or _stop_process_group(process)
        if not stopped:
            record.update({
                "stop_status": "stop_unconfirmed",
                "failure": "Review CLI stop was not confirmed; manual inspection is required.",
                "finished_at": None,
            })
            _write_record(record_path, record)
            raise AgentDeliveryError("The review CLI timed out and its stop could not be confirmed; reviews are blocked.")
        exit_code = process.returncode if process is not None else None
        record.update({
            "stop_status": "confirmed_stopped",
            "exit_code": exit_code,
            "failure": "Review CLI was cancelled after a confirmed stop." if interrupted else "Review CLI exceeded its time limit and was stopped.",
            "finished_at": now_utc(),
        })
        _write_record(record_path, record)
        raise AgentDeliveryError(
            ("The review CLI was interrupted and stopped; no review was accepted." if interrupted
             else "The review CLI exceeded its time limit and was stopped; no review was accepted.")
        )

    exit_code = process.returncode if process is not None else None
    stopped = process is None or not _process_group_exists(process.pid) or _stop_process_group(process)
    if not stopped:
        record.update({"stop_status": "stop_unconfirmed", "failure": "Review CLI exited but its group may still run.", "finished_at": None})
        _write_record(record_path, record)
        raise AgentDeliveryError("The review CLI exited but its process group may still be running; reviews are blocked.")
    # Stop confirmation is independent of result readability: persist it first so
    # an unreadable result cannot leave a permanent start_unconfirmed gate behind.
    record.update({"stop_status": "confirmed_stopped", "exit_code": exit_code, "finished_at": now_utc()})
    if exit_code != 0:
        record["failure"] = "The review CLI returned a non-zero exit code."
        _write_record(record_path, record)
        raise AgentDeliveryError(f"The review CLI exited with code {exit_code}; no review was accepted.")
    _write_record(record_path, record)
    result = _read_json(result_path)
    return {
        "review_id": review_id,
        "codex_version": version,
        "exit_code": exit_code,
        "stop_status": "confirmed_stopped",
        "result_path": str(result_path),
        "result": result,
    }


def _truncate_if_needed(path: Path) -> None:
    try:
        if path.stat().st_size > MAX_CAPTURED_OUTPUT_BYTES:
            data = path.read_bytes()[-MAX_CAPTURED_OUTPUT_BYTES:]
            descriptor = os.open(path, os.O_TRUNC | os.O_WRONLY)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(b"[truncated]\n" + data)
    except OSError:
        pass
