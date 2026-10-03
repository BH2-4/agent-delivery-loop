"""Private local run records and single-host task exclusion."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .errors import AgentDeliveryError

WORKER_STATES = {"not_started", "start_unconfirmed", "running", "stopped", "stop_unconfirmed"}
SAFE_RUN_STATUSES = {
    "not_started": {"not_started", "failed"},
    "stopped": {"validating", "local_ready", "delivery_pr_open", "cancelled", "failed"},
}


class RunStateError(AgentDeliveryError):
    """An unreadable or unpersisted safety record must never permit a launch."""


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate record field")
        result[key] = value
    return result


def default_state_dir() -> Path:
    override = os.environ.get("AGENT_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".agent-delivery-loop").resolve()


def authorization_key(repo: str, number: int, merge_sha: str, order_sha: str) -> str:
    identity = f"{repo.casefold()}|{number}|{merge_sha}|{order_sha}".encode()
    return hashlib.sha256(identity).hexdigest()


def task_key(repo: str, task_id: str, revision: int) -> str:
    identity = f"{repo.casefold()}|{task_id}|r{revision}".encode()
    return hashlib.sha256(identity).hexdigest()


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.runs = self.root / "runs"
        self.locks = self.root / "locks"
        self.cleanup_failure = self.locks / "worker.cleanup-failed.json"
        self.runs.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.locks.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.root.chmod(0o700)
            self.runs.chmod(0o700)
            self.locks.chmod(0o700)
        _fsync_directory(self.root)
        _fsync_directory(self.root.parent)

    @contextmanager
    def claim(self, key: str) -> Iterator[None]:
        host_descriptor = os.open(self.locks / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
        task_descriptor: int | None = None
        try:
            try:
                fcntl.flock(host_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AgentDeliveryError("Another Work Order is already running on this host.") from exc
            self.assert_worker_available()
            task_descriptor = os.open(self.locks / f"{key}.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(task_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise AgentDeliveryError("This Work Order is already running on this host.") from exc
            yield
        finally:
            if task_descriptor is not None:
                os.close(task_descriptor)
            os.close(host_descriptor)

    def assert_worker_available(self) -> None:
        try:
            # Honor older failure markers, but never create another state source.
            try:
                self.cleanup_failure.lstat()
            except FileNotFoundError:
                pass
            else:
                raise RunStateError("A legacy Worker cleanup marker remains. Stop trials and obtain manual safety review.")
            # iterdir propagates read errors; glob/exists can silently hide them.
            for directory in self.runs.iterdir():
                if directory.is_symlink() or not directory.is_dir():
                    raise ValueError("Unexpected run directory")
                for path in directory.iterdir():
                    if path.suffix == ".tmp":
                        continue
                    record = self._read_record(path)
                    worker_status = record["worker_status"]
                    if record["status"] not in SAFE_RUN_STATUSES.get(worker_status, set()):
                        raise RunStateError(
                            "A prior run has no confirmed safe Worker end. Stop trials and obtain manual safety review."
                        )
        except (OSError, ValueError) as exc:
            raise RunStateError("Run safety records cannot be read or are invalid. Worker start is blocked; stop trials.") from exc

    def _read_record(self, path: Path) -> dict[str, Any]:
        if path.suffix != ".json" or path.is_symlink() or not path.is_file():
            raise ValueError("Unexpected run record")
        record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("schema_version"), int)
            or isinstance(record["schema_version"], bool)
            or record["schema_version"] != 1
            or record.get("run_id") != path.stem
            or not isinstance(record.get("status"), str)
            or not isinstance(record.get("worker_status"), str)
            or record["worker_status"] not in WORKER_STATES
        ):
            raise ValueError("Missing or invalid Worker safety state")
        return record

    def record_path(self, key: str, run_id: str) -> Path:
        return self.runs / key / f"{run_id}.json"

    def has_record(self, key: str) -> bool:
        directory = self.runs / key
        try:
            return any(path.suffix == ".json" for path in directory.iterdir())
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise RunStateError("Run records cannot be read. Worker start is blocked; stop trials.") from exc

    def write(self, key: str, run_id: str, record: dict[str, Any]) -> None:
        directory = self.runs / key
        path = self.record_path(key, run_id)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
            _fsync_directory(self.runs)
            payload = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
            descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _fsync_directory(directory)
            if self._read_record(path) != record:
                raise ValueError("Persisted record did not match")
        except (OSError, ValueError, TypeError) as exc:
            raise RunStateError("Run safety state could not be persisted and verified. Stop trials; no safe end was recorded.") from exc
        finally:
            # An interrupted update leaves the previous JSON safety gate intact.
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                raise RunStateError("Temporary run-state cleanup failed. Stop trials and inspect the safety records.") from exc


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
