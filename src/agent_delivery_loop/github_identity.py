"""Explicit, repository-bound PAT snapshots; never consult ambient gh credentials."""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from .errors import AgentDeliveryError
from .store import default_state_dir

LOGIN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}")
REPOSITORY_RE = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+")
PAT_RE = re.compile(r"github_pat_[A-Za-z0-9_]+")
MAX_PAT_BYTES = 4096


@dataclass(frozen=True, slots=True)
class GitHubPAT:
    repository: str
    expected_login: str
    _token: str = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if not REPOSITORY_RE.fullmatch(self.repository) or not LOGIN_RE.fullmatch(self.expected_login):
            raise AgentDeliveryError("PAT repository or expected login is invalid.")
        if not PAT_RE.fullmatch(self._token) or len(self._token) > MAX_PAT_BYTES:
            raise AgentDeliveryError("A fine-grained PAT is required; classic or ambiguous credentials are rejected.")

    @classmethod
    def from_file(cls, *, path: Path, repo_root: Path, repository: str, expected_login: str) -> GitHubPAT:
        candidate = path.expanduser().absolute()
        resolved = candidate.resolve()
        forbidden = (repo_root.resolve(), default_state_dir().resolve())
        if any(resolved == root or root in resolved.parents for root in forbidden):
            raise AgentDeliveryError("PAT must be outside the repository and Worker state directory.")
        if any((parent / ".git").exists() for parent in resolved.parents):
            raise AgentDeliveryError("PAT must not be stored inside a Git checkout.")
        descriptor = None
        try:
            parent = resolved.parent.stat()
            if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
                raise AgentDeliveryError("PAT parent directory must be owned by this user with mode 0700.")
            descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) not in (0o400, 0o600) or info.st_nlink != 1
                or not 0 < info.st_size <= MAX_PAT_BYTES
            ):
                raise AgentDeliveryError("PAT must be a private, user-owned regular file (0400/0600), not a link.")
            token = os.read(descriptor, MAX_PAT_BYTES + 1).decode("ascii").strip()
        except (OSError, UnicodeError, ValueError):
            raise AgentDeliveryError("PAT file could not be safely read; no gh login fallback is allowed.") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return cls(repository=repository, expected_login=expected_login, _token=token)

    def token_for(self, repository: str) -> str:
        if repository.casefold() != self.repository.casefold():
            raise AgentDeliveryError("PAT operation targets a different repository; refusing the operation.")
        return self._token

    def metadata(self) -> dict[str, str]:
        """Only these non-secret fields may enter checkpoints or CLI results."""
        return {
            "kind": "fine_grained_pat",
            "repository": self.repository,
            "expected_login": self.expected_login,
        }
