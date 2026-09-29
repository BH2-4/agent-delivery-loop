"""Narrow GitHub App identity for pushing one branch and opening its PR."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AgentDeliveryError
from .github import GitHubClient, Repo


@dataclass(frozen=True, slots=True)
class GitHubAppConfig:
    app_id: str
    installation_id: str
    private_key_path: Path
    key_isolation_verified: bool
    main_protection_verified: bool

    @classmethod
    def from_environment(cls, repo_root: Path) -> GitHubAppConfig | None:
        app_id = os.environ.get("AGENT_GITHUB_APP_ID", "").strip()
        installation_id = os.environ.get("AGENT_GITHUB_INSTALLATION_ID", "").strip()
        key_value = os.environ.get("AGENT_GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
        key_isolation_verified = os.environ.get("AGENT_APP_KEY_ISOLATION_VERIFIED") == "1"
        main_protection_verified = os.environ.get("AGENT_MAIN_PROTECTION_VERIFIED") == "1"
        any_attestation = any(
            os.environ.get(name)
            for name in ("AGENT_APP_KEY_ISOLATION_VERIFIED", "AGENT_MAIN_PROTECTION_VERIFIED")
        )
        if not any((app_id, installation_id, key_value, any_attestation)):
            return None
        if not app_id.isdigit() or not installation_id.isdigit() or not key_value:
            raise AgentDeliveryError("GitHub App configuration is incomplete; no credential was used.")
        key_path = Path(key_value).expanduser().resolve()
        if not key_path.is_file():
            raise AgentDeliveryError("GitHub App private key file was not found.")
        try:
            key_path.relative_to(repo_root.resolve())
        except ValueError:
            pass
        else:
            raise AgentDeliveryError("GitHub App private key must be stored outside the repository.")
        if os.name == "posix" and key_path.stat().st_mode & 0o077:
            raise AgentDeliveryError("GitHub App private key must be readable only by its owner (mode 0600).")
        if not key_isolation_verified or not main_protection_verified:
            raise AgentDeliveryError(
                "GitHub App write requires separate key-isolation and main-protection attestations after verification."
            )
        return cls(app_id, installation_id, key_path, key_isolation_verified, main_protection_verified)


class GitHubAppTokenProvider:
    def __init__(self, repo: Repo, config: GitHubAppConfig) -> None:
        self.repo = repo
        self.config = config

    def installation_token(self) -> str:
        try:
            import jwt
        except ImportError as exc:
            raise AgentDeliveryError("Install the optional github-app dependency before publishing.") from exc
        try:
            private_key = self.config.private_key_path.read_bytes()
            now = int(time.time())
            app_jwt = jwt.encode(
                {"iat": now - 30, "exp": now + 8 * 60, "iss": self.config.app_id},
                private_key,
                algorithm="RS256",
            )
        except (OSError, ValueError, TypeError) as exc:
            raise AgentDeliveryError("GitHub App private key could not be used.") from exc
        # The installation-token endpoint requires an App JWT. The resulting
        # installation token is intentionally returned only to the publisher.
        client = GitHubClient(self.repo, token=app_jwt)
        response = client.request(
            "POST",
            f"/app/installations/{self.config.installation_id}/access_tokens",
            {
                "repositories": [self.repo.name],
                "permissions": {"contents": "write", "pull_requests": "write"},
            },
        )
        if not isinstance(response, dict):
            raise AgentDeliveryError("GitHub did not issue an installation token.")
        permissions: Any = response.get("permissions")
        if not isinstance(permissions, dict):
            raise AgentDeliveryError("GitHub App token did not report its permissions.")
        required = {"contents": "write", "pull_requests": "write"}
        if any(permissions.get(name) != level for name, level in required.items()):
            raise AgentDeliveryError("GitHub App token lacks the exact write permissions needed for delivery.")
        if set(permissions) - {"metadata", "contents", "pull_requests"}:
            raise AgentDeliveryError("GitHub App token has permissions outside the first-version allowlist.")
        token = response.get("token")
        if not isinstance(token, str) or not token:
            raise AgentDeliveryError("GitHub did not return a usable installation token.")
        return token
