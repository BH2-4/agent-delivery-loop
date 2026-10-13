"""GitHub-authoritative claims and at-most-once external Worker launch grants.

Only a successful atomic POST of a NEW start ref grants permission to launch.
Reading our own ref, even after an ambiguous response, never grants permission.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from .errors import AgentDeliveryError
from .github import GitHubAPIError, GitHubClient, GitHubWritePendingError


def execution_key(repository: str, task_id: str, version: str) -> str:
    if not all(isinstance(x, str) and x.strip() for x in (repository, task_id, version)):
        raise AgentDeliveryError("Execution identity requires repository, task and an explicit version.")
    identity = json.dumps([repository.casefold(), task_id, version], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode()).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate execution field")
        result[key] = value
    return result


class ExecutionStore:
    """Two retained refs per execution; completed starts advance only by fast-forward."""

    def __init__(self, client: GitHubClient) -> None:
        self.client = client
        self.repository = client.repo.slug.casefold()
        self.api = f"/repos/{client.repo.slug}/git"

    def _ref(self, kind: str, key: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", key):
            raise AgentDeliveryError("Invalid execution key.")
        return f"refs/heads/delivery-{kind}/{key}"

    def _read(self, kind: str, key: str) -> dict[str, Any] | None:
        ref = self._ref(kind, key)
        try:
            result = self.client.request("GET", f"{self.api}/ref/{ref.removeprefix('refs/')}")
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                # A private repository's hidden/unauthorized 404 is not evidence of
                # absence. The subsequent atomic write must still succeed to launch.
                return None
            raise
        try:
            if result["ref"] != ref or result["object"]["type"] != "commit":
                raise ValueError("Unexpected ref")
            sha = result["object"]["sha"]
            commit = self.client.request("GET", f"{self.api}/commits/{sha}")
            tree = self.client.request("GET", f"{self.api}/trees/{commit['tree']['sha']}")
            entries = tree["tree"]
            if len(entries) != 1 or entries[0]["path"] != "execution.json" or entries[0]["type"] != "blob" or entries[0]["mode"] != "100644":
                raise ValueError("Unexpected execution tree")
            blob = self.client.request("GET", f"{self.api}/blobs/{entries[0]['sha']}")
            if blob["encoding"] != "base64" or len(blob["content"]) > 64 * 1024:
                raise ValueError("Invalid execution blob")
            record = json.loads(base64.b64decode(blob["content"]).decode(), object_pairs_hook=_unique_object)
            if (record["schema_version"] != 1 or record["execution_key"] != key
                or record["repository"] != self.repository or record["kind"] != kind
                or record["status"] not in ({"claimed"} if kind == "claims" else {"pending_confirmation", "completed"})
                or not all(isinstance(record[name], str) and record[name] for name in ("claimant_id", "request_id"))
                or not isinstance(record["binding"], dict)):
                raise ValueError("Invalid execution binding")
            if kind == "starts" and (not isinstance(record.get("claim_sha"), str) or not isinstance(record.get("grant_id"), str)):
                raise ValueError("Missing launch identity")
            return {**record, "record_sha": sha}
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise AgentDeliveryError("Shared execution record is invalid; launch is blocked pending inspection.") from exc

    def _commit(self, record: dict[str, Any], parents: list[str]) -> str:
        tree = self.client.request("POST", f"{self.api}/trees", {"tree": [{
            "path": "execution.json", "mode": "100644", "type": "blob",
            "content": json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        }]}, expected_status=201)
        tree_sha = self._confirmed_sha(tree)
        commit = self.client.request("POST", f"{self.api}/commits", {
            "message": f"delivery {record['kind']}: {record['execution_key']}",
            "tree": tree_sha, "parents": parents,
        }, expected_status=201)
        return self._confirmed_sha(commit)

    @staticmethod
    def _confirmed_sha(response: Any) -> str:
        sha = response.get("sha") if isinstance(response, dict) else None
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitHubWritePendingError("GitHub did not confirm the execution commit.")
        return sha

    def _create(self, kind: str, key: str, record: dict[str, Any]) -> dict[str, Any]:
        ref = self._ref(kind, key)
        try:
            sha = self._commit(record, [])
            result = self.client.request("POST", f"{self.api}/refs", {"ref": ref, "sha": sha}, expected_status=201)
            if not isinstance(result, dict) or result.get("ref") != ref or not isinstance(result.get("object"), dict) or result["object"].get("sha") != sha or result["object"].get("type") != "commit":
                raise GitHubWritePendingError("GitHub did not confirm the exact newly created execution ref.")
            return {**record, "record_sha": sha, "result": "new", "new_start": kind == "starts"}
        except GitHubWritePendingError:
            # Read-only inspection is diagnostic. It cannot turn unknown into new.
            return {**record, "result": "unknown", "new_start": False,
                    "status": "pending_confirmation", "reason": "write_outcome_unknown"}
        except GitHubAPIError as exc:
            if exc.status_code not in (409, 422):
                raise
            existing = self._read(kind, key)
            if existing is None:
                raise  # Validation/abuse errors are not occupation.
            return {**existing, "result": "existing", "new_start": False}

    def inspect(self, key: str) -> dict[str, Any]:
        claim = self._read("claims", key)
        start = self._read("starts", key)
        if start and (claim is None or start["claim_sha"] != claim["record_sha"] or start["claimant_id"] != claim["claimant_id"] or start["binding"] != claim["binding"]):
            raise AgentDeliveryError("Start record disagrees with claim; launch is blocked.")
        return {"execution_key": key, "repository": self.repository, "status": start["status"] if start else "claimed" if claim else "absent",
                "claimant_id": claim["claimant_id"] if claim else None,
                "claim_sha": claim["record_sha"] if claim else None,
                "start_sha": start["record_sha"] if start else None,
                "request_id": start["request_id"] if start else claim["request_id"] if claim else None,
                "grant_id": start["grant_id"] if start else None,
                "session_id": start.get("session_id") if start else None,
                "reason": start.get("reason") if start else None}

    def acquire(self, key: str, *, claimant_id: str, request_id: str, binding: dict[str, Any]) -> dict[str, Any]:
        claim = self._read("claims", key)
        if claim is None:
            claim = self._create("claims", key, {
                "schema_version": 1, "repository": self.repository, "execution_key": key,
                "kind": "claims", "status": "claimed", "claimant_id": claimant_id,
                "request_id": request_id, "binding": binding, "created_at": datetime.now(UTC).isoformat(),
            })
            if claim["result"] == "unknown":
                return claim
        if claim["binding"] != binding:
            raise AgentDeliveryError("Execution key already has different authorization; use an explicitly authorized new version.")
        start = self._read("starts", key)
        if start is not None:
            # Check the relationship, even if this caller is not the claimant.
            return {**self.inspect(key), "result": "existing", "new_start": False}
        if claim["claimant_id"] != claimant_id:
            return {**self.inspect(key), "result": "not_owner", "new_start": False}
        return self._create("starts", key, {
            "schema_version": 1, "repository": self.repository, "execution_key": key,
            "kind": "starts", "status": "pending_confirmation", "claimant_id": claimant_id,
            "request_id": request_id, "binding": binding, "claim_sha": claim["record_sha"],
            "grant_id": str(uuid.uuid4()), "created_at": datetime.now(UTC).isoformat(),
            "reason": "external_launch_or_receipt_unconfirmed", "session_id": None,
        })

    def complete(self, grant: dict[str, Any], *, session_id: str) -> None:
        if grant.get("result") != "new" or grant.get("new_start") is not True:
            raise AgentDeliveryError("Only this invocation's new launch may record its completion.")
        record = {k: v for k, v in grant.items() if k not in {"record_sha", "result", "new_start"}}
        record.update(status="completed", reason=None, session_id=session_id, completed_at=datetime.now(UTC).isoformat())
        sha = self._commit(record, [grant["record_sha"]])
        ref = self._ref("starts", grant["execution_key"])
        result = self.client.request("PATCH", f"{self.api}/refs/{ref.removeprefix('refs/')}", {"sha": sha, "force": False}, expected_status=200)
        if result.get("object", {}).get("sha") != sha:
            raise GitHubWritePendingError("Completion was not confirmed; inspect the retained start record.")
