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
        if self.cleanup_failure.exists():
            raise AgentDeliveryError(
                "A prior Worker process group could not be confirmed stopped. Inspect the recorded run and "
                "worker processes before manually clearing the cleanup failure marker."
            )

    def mark_worker_cleanup_failed(self, run_id: str) -> None:
        """Block subsequent local runs after an unconfirmed Worker shutdown."""
        payload = json.dumps(
            {"run_id": run_id, "recorded_at": now_utc(), "status": "cleanup_failed"},
            sort_keys=True,
        ).encode("utf-8") + b"\n"
        descriptor = os.open(self.cleanup_failure, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())

    def record_path(self, key: str, run_id: str) -> Path:
        return self.runs / key / f"{run_id}.json"

    def has_record(self, key: str) -> bool:
        directory = self.runs / key
        return directory.is_dir() and any(directory.glob("*.json"))

    def write(self, key: str, run_id: str, record: dict[str, Any]) -> None:
        directory = self.runs / key
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            directory.chmod(0o700)
        path = self.record_path(key, run_id)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
