"""Current-session pull protocol for generic tasks authorized by merged Plan PRs.

This module never starts another Agent. It discovers and verifies an immutable task
package, obtains a one-time start grant, and writes a separate shared receipt.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence

from .errors import AgentDeliveryError
from .execution import ExecutionStore, execution_key
from .github import GitHubAPIError, GitHubClient, GitHubWritePendingError, Repo, parse_plan_pr_ref
from .github_identity import GitHubPAT

PULL_PROTOCOL_VERSION = "agent-pull/1"
TASK_FILE_RE = re.compile(r"^\.agents/pull-tasks/(?P<task>[A-Za-z0-9][A-Za-z0-9._-]*)-r(?P<revision>[1-9][0-9]*)\.json$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_TASK_BYTES = 128 * 1024
MAX_CONTEXT_BYTES = 2 * 1024 * 1024
MAX_RECORD_BYTES = 64 * 1024
REPORT_STATUSES = {"running", "completed", "blocked", "failed"}
TERMINAL_REPORT_STATUSES = {"completed", "blocked", "failed"}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_repo_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise AgentDeliveryError(f"{label} must be a repository-relative POSIX path.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise AgentDeliveryError(f"{label} must not escape the repository root.")
    if len(value) > 500:
        raise AgentDeliveryError(f"{label} is too long.")
    return value


def _string_list(value: Any, *, label: str, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or (not allow_empty and not value):
        raise AgentDeliveryError(f"{label} must be a list of strings.")
    if len(value) > 100:
        raise AgentDeliveryError(f"{label} contains too many entries.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > 2000 or "\x00" in item:
            raise AgentDeliveryError(f"{label} entries must be non-empty strings no longer than 2000 characters.")
        text = item.strip()
        if text in result:
            raise AgentDeliveryError(f"{label} must not contain duplicates.")
        result.append(text)
    return result


def parse_pull_task(raw: bytes, path: str) -> dict[str, Any]:
    match = TASK_FILE_RE.fullmatch(path)
    if not match:
        raise AgentDeliveryError("Task path must be .agents/pull-tasks/<task-id>-r<revision>.json.")
    if not 0 < len(raw) <= MAX_TASK_BYTES:
        raise AgentDeliveryError("Pull task file is empty or exceeds the size limit.")
    try:
        task = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError) as exc:
        raise AgentDeliveryError("Pull task must be valid UTF-8 JSON without duplicate object fields.") from exc
    if not isinstance(task, dict):
        raise AgentDeliveryError("Pull task must be a JSON object.")
    allowed = {
        "schema_version", "task_id", "revision", "goal", "instructions", "scope",
        "acceptance_criteria", "required_capabilities", "context", "target",
    }
    if set(task) - allowed:
        raise AgentDeliveryError("Pull task contains unsupported fields.")
    if task.get("schema_version") != 1 or isinstance(task.get("schema_version"), bool):
        raise AgentDeliveryError("Only pull task schema_version 1 is supported.")
    task_id = task.get("task_id")
    revision = task.get("revision")
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise AgentDeliveryError("Pull task task_id is invalid.")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise AgentDeliveryError("Pull task revision must be a positive integer.")
    if task_id != match.group("task") or revision != int(match.group("revision")):
        raise AgentDeliveryError("Task path, task_id, and revision do not match.")
    for key in ("goal", "instructions"):
        value = task.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > 50_000:
            raise AgentDeliveryError(f"Pull task {key} must be a non-empty string up to 50000 characters.")
    scope = task.get("scope")
    if not isinstance(scope, dict) or set(scope) != {"constraints"}:
        raise AgentDeliveryError('Pull task scope must be an object with a "constraints" list.')
    constraints = _string_list(scope.get("constraints"), label="scope.constraints", allow_empty=False)
    criteria = _string_list(task.get("acceptance_criteria"), label="acceptance_criteria", allow_empty=False)
    capabilities = _string_list(task.get("required_capabilities"), label="required_capabilities")
    if any(len(item) > 100 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", item) for item in capabilities):
        raise AgentDeliveryError("Required capabilities must use short identifiers (letters, digits, dot, underscore, colon, or hyphen).")
    context = task.get("context")
    if not isinstance(context, list) or len(context) > 100:
        raise AgentDeliveryError("Pull task context must be a list with at most 100 entries.")
    checked_context: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for item in context:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise AgentDeliveryError('Each context reference must contain exactly "path" and "sha256".')
        context_path = _safe_repo_path(item.get("path"), label="Context path")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise AgentDeliveryError("Context sha256 must be 64 lowercase hexadecimal characters.")
        if context_path in seen_paths:
            raise AgentDeliveryError("Pull task context paths must be unique.")
        seen_paths.add(context_path)
        checked_context.append({"path": context_path, "sha256": digest})
    target = task.get("target")
    if target is not None and (not isinstance(target, str) or not target.strip() or len(target) > 2000):
        raise AgentDeliveryError("Optional target must be a non-empty string up to 2000 characters.")
    return {
        "schema_version": 1,
        "task_id": task_id,
        "revision": revision,
        "goal": task["goal"].strip(),
        "instructions": task["instructions"].strip(),
        "scope": {"constraints": constraints},
        "acceptance_criteria": criteria,
        "required_capabilities": capabilities,
        "context": checked_context,
        **({"target": target.strip()} if target is not None else {}),
    }


def _authority_branch(value: str) -> str:
    if (
        not isinstance(value, str) or not value or len(value) > 250
        or value.startswith("-") or value.endswith(("/", ".")) or ".." in value
        or "//" in value or "\\" in value or "@{" in value
        or not re.fullmatch(r"[A-Za-z0-9._/-]+", value)
    ):
        raise AgentDeliveryError("Authority branch name is invalid.")
    return value


def _validate_merged_plan(client: GitHubClient, number: int, task_path: str, branch: str, url: str) -> tuple[str, dict[str, Any]]:
    _safe_repo_path(task_path, label="Task path")
    if not TASK_FILE_RE.fullmatch(task_path):
        raise AgentDeliveryError("Task path must be .agents/pull-tasks/<task-id>-r<revision>.json.")
    pull = client.get_pull(number)
    if pull.get("state") != "closed" or not pull.get("merged_at"):
        raise AgentDeliveryError("Plan PR has not been merged; execution is not authorized.")
    base = pull.get("base")
    base_repo = base.get("repo") if isinstance(base, dict) else None
    if (
        not isinstance(base, dict) or base.get("ref") != branch
        or not isinstance(base_repo, dict)
        or str(base_repo.get("full_name", "")).casefold() != client.repo.slug.casefold()
    ):
        raise AgentDeliveryError("Plan PR must be merged into the selected authority branch of this repository.")
    merge_sha = pull.get("merge_commit_sha")
    if not isinstance(merge_sha, str) or not SHA_RE.fullmatch(merge_sha):
        raise AgentDeliveryError("GitHub did not provide a full merge commit SHA for the Plan PR.")
    files = client.pull_files(number)
    if not any(item.get("filename") == task_path and item.get("status") in {"added", "modified"} for item in files):
        raise AgentDeliveryError("The selected merged Plan PR did not add or modify this exact pull task.")
    raw = client.content(task_path, merge_sha)
    task = parse_pull_task(raw, task_path)
    return merge_sha, {"task": task, "task_bytes": raw, "task_sha256": _sha256(raw), "url": url, "merge_sha": merge_sha, "task_path": task_path, "number": number, "authority_branch": branch}


def _load_task_package(client: GitHubClient, *, plan_pr: str, task_path: str, authority_branch: str) -> dict[str, Any]:
    repo, number, url = parse_plan_pr_ref(plan_pr)
    if repo.slug.casefold() != client.repo.slug.casefold():
        raise AgentDeliveryError("Plan PR belongs to a different target repository.")
    branch = _authority_branch(authority_branch)
    _merge_sha, resolved = _validate_merged_plan(client, number, task_path, branch, url)
    context_payload: list[dict[str, Any]] = []
    context_refs: list[dict[str, str]] = []
    total = 0
    for reference in resolved["task"]["context"]:
        raw = client.content(reference["path"], resolved["merge_sha"])
        actual_digest = _sha256(raw)
        if actual_digest != reference["sha256"]:
            raise AgentDeliveryError(f"Fixed context digest mismatch at {reference['path']}; task is blocked.")
        total += len(raw)
        if total > MAX_CONTEXT_BYTES:
            raise AgentDeliveryError("Verified task context exceeds the total size limit.")
        try:
            value = {"path": reference["path"], "sha256": actual_digest, "encoding": "utf-8", "content": raw.decode("utf-8")}
        except UnicodeDecodeError:
            value = {"path": reference["path"], "sha256": actual_digest, "encoding": "base64", "content": base64.b64encode(raw).decode("ascii")}
        context_payload.append(value)
        context_refs.append(reference)
    context_sha256 = _sha256(_json_bytes(context_refs))
    binding = {
        "protocol_version": PULL_PROTOCOL_VERSION,
        "repository": client.repo.slug.casefold(),
        "task_id": resolved["task"]["task_id"],
        "revision": resolved["task"]["revision"],
        "task_sha256": resolved["task_sha256"],
        "source_merge_sha": resolved["merge_sha"],
        "source_plan_pr": resolved["url"],
        "authority_branch": branch,
        "task_path": task_path,
        "context_sha256": context_sha256,
    }
    # Identity is the declared task revision. Content changes under the same id/revision
    # must collide and fail the canonical-binding comparison instead of minting a new key.
    key = execution_key(client.repo.slug, f"agent-pull-v1:{resolved['task']['task_id']}", f"r{resolved['task']['revision']}")
    package = {
        "task": resolved["task"],
        "context": context_payload,
        "task_package_version": {
            "protocol_version": PULL_PROTOCOL_VERSION,
            "repository": client.repo.slug,
            "task_id": resolved["task"]["task_id"],
            "revision": resolved["task"]["revision"],
            "task_sha256": resolved["task_sha256"],
            "source_merge_sha": resolved["merge_sha"],
            "source_plan_pr": resolved["url"],
            "authority_branch": branch,
            "task_path": task_path,
            "context_sha256": context_sha256,
        },
        "execution_key": key,
        "binding": binding,
    }
    return package


class PullExecutionStore(ExecutionStore):
    """Pull-only shared claim/start/receipt namespace, separate from Work Orders."""

    def _ref(self, kind: str, key: str) -> str:
        if kind not in {"claims", "starts", "receipts"} or not re.fullmatch(r"[0-9a-f]{64}", key):
            raise AgentDeliveryError("Invalid pull execution reference.")
        return f"refs/heads/agent-pull-v1-{kind}/{key}"

    def _commit(self, record: dict[str, Any], parents: list[str]) -> str:
        if record.get("kind") != "receipts":
            return super()._commit(record, parents)
        tree = self.client.request("POST", f"{self.api}/trees", {"tree": [{
            "path": "receipt.json", "mode": "100644", "type": "blob",
            "content": json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        }]}, expected_status=201)
        tree_sha = self._confirmed_sha(tree)
        commit = self.client.request("POST", f"{self.api}/commits", {
            "message": f"agent pull receipt: {record['execution_key']}",
            "tree": tree_sha, "parents": parents,
        }, expected_status=201)
        return self._confirmed_sha(commit)

    def _read(self, kind: str, key: str) -> dict[str, Any] | None:
        if kind != "receipts":
            return super()._read(kind, key)
        ref = self._ref(kind, key)
        try:
            result = self.client.request("GET", f"{self.api}/ref/{ref.removeprefix('refs/')}")
        except GitHubAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        try:
            if result["ref"] != ref or result["object"]["type"] != "commit":
                raise ValueError("Unexpected pull receipt ref")
            sha = result["object"]["sha"]
            commit = self.client.request("GET", f"{self.api}/commits/{sha}")
            tree = self.client.request("GET", f"{self.api}/trees/{commit['tree']['sha']}")
            entries = tree["tree"]
            if len(entries) != 1 or entries[0]["path"] != "receipt.json" or entries[0]["type"] != "blob" or entries[0]["mode"] != "100644":
                raise ValueError("Unexpected pull receipt tree")
            blob = self.client.request("GET", f"{self.api}/blobs/{entries[0]['sha']}")
            encoded = blob["content"]
            if (
                blob["encoding"] != "base64" or not isinstance(encoded, str)
                or len(encoded) > ((MAX_RECORD_BYTES + 2) // 3) * 4 + 8192
            ):
                raise ValueError("Invalid pull receipt blob")
            try:
                raw_record = base64.b64decode("".join(encoded.split()), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("Invalid pull receipt encoding") from exc
            if len(raw_record) > MAX_RECORD_BYTES:
                raise ValueError("Pull receipt exceeds the shared record size limit")
            record = json.loads(raw_record.decode("utf-8"), object_pairs_hook=_unique_object)
            if (
                record.get("schema_version") != 1 or record.get("kind") != "receipts"
                or record.get("execution_key") != key or record.get("repository") != self.repository
                or record.get("status") not in REPORT_STATUSES or not isinstance(record.get("binding"), dict)
                or not all(isinstance(record.get(name), str) and record[name] for name in ("claimant_id", "request_id", "grant_id", "start_sha"))
            ):
                raise ValueError("Invalid pull receipt")
            return {**record, "record_sha": sha}
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise AgentDeliveryError("Shared pull receipt is invalid; execution is blocked pending inspection.") from exc

    def inspect_pull(self, key: str) -> dict[str, Any]:
        state = self.inspect(key)
        claim = self._read("claims", key)
        if claim is not None:
            state["binding"] = claim["binding"]
        receipt = self._read("receipts", key)
        if receipt is not None:
            start = self._read("starts", key)
            if start is None:
                raise AgentDeliveryError("Pull receipt exists without a start grant; execution is blocked pending inspection.")
            if (
                receipt["binding"] != start["binding"]
                or receipt["start_sha"] != start["record_sha"]
                or receipt["grant_id"] != state["grant_id"]
                or receipt["claimant_id"] != state["claimant_id"]
            ):
                raise AgentDeliveryError("Pull receipt disagrees with its start grant; execution is blocked.")
            state.update({key: value for key, value in receipt.items() if key not in {"record_sha", "kind", "schema_version", "repository", "execution_key", "binding"}})
            state["receipt_sha"] = receipt["record_sha"]
            state["status"] = receipt["status"]
            state["summary"] = receipt["summary"]
            state["artifacts"] = receipt["artifacts"]
            state["next_actions"] = receipt["next_actions"]
        else:
            state["artifacts"] = []
            if state["status"] == "pending_confirmation":
                state["summary"] = "The start record exists, but no shared receipt confirms what the current Agent did."
                state["next_actions"] = ["Inspect the original execution host and its current Agent session; do not reacquire or rerun automatically."]
            elif state["status"] == "claimed":
                state["summary"] = "A shared claim exists without a confirmed start grant; this invocation has no permission to work."
                state["next_actions"] = ["Inspect the shared claim and original host. Do not automatically retry or transfer ownership."]
            else:
                state["summary"] = "No shared claim, start grant, or receipt is visible for this task revision."
                state["next_actions"] = ["If this is still eligible, check capabilities and acquire from one active harness session."]
            state["can_execute"] = False
        return state

    def write_receipt(self, grant: dict[str, Any], *, status: str, summary: str, artifacts: list[str], next_actions: list[str], executor: dict[str, Any], reported_at: str | None = None) -> dict[str, Any]:
        if grant.get("result") != "new" or grant.get("new_start") is not True:
            raise AgentDeliveryError("A retained local grant issued by a confirmed new acquire is required to report task work.")
        if status not in REPORT_STATUSES:
            raise AgentDeliveryError("Report status must be running, completed, blocked, or failed.")
        if not isinstance(summary, str) or not summary.strip() or len(summary) > 8000:
            raise AgentDeliveryError("Report summary must be a non-empty string up to 8000 characters.")
        if len(artifacts) > 100 or len(next_actions) > 100:
            raise AgentDeliveryError("Report contains too many artifacts or next actions.")
        start = self._read("starts", grant["execution_key"])
        if start is None or start["record_sha"] != grant["record_sha"] or start["grant_id"] != grant["grant_id"] or start["binding"] != grant["binding"]:
            raise AgentDeliveryError("Grant does not match the fixed shared start record; report is blocked.")
        record = {
            "schema_version": 1, "repository": self.repository, "execution_key": grant["execution_key"],
            "kind": "receipts", "status": status, "claimant_id": grant["claimant_id"],
            "request_id": grant["request_id"], "binding": grant["binding"],
            "start_sha": grant["record_sha"], "grant_id": grant["grant_id"],
            "summary": summary.strip(), "artifacts": artifacts, "next_actions": next_actions,
            "executor": executor, "reported_at": reported_at or datetime.now(UTC).isoformat(),
        }
        if len(_json_bytes(record)) > MAX_RECORD_BYTES:
            raise AgentDeliveryError("Pull receipt exceeds the shared size limit; no shared write was attempted.")
        existing = self._read("receipts", grant["execution_key"])
        if existing is None:
            created = self._create("receipts", grant["execution_key"], record)
            if created.get("result") == "unknown":
                raise GitHubWritePendingError("Receipt write outcome is unknown; inspect status before any further report.")
            if created.get("result") == "existing":
                if created.get("grant_id") != grant["grant_id"] or created.get("start_sha") != grant["record_sha"]:
                    raise AgentDeliveryError("A different receipt already occupies this task execution.")
                if created.get("status") in TERMINAL_REPORT_STATUSES:
                    raise AgentDeliveryError("A terminal receipt already exists; inspect status instead of reporting again.")
                existing = created
            else:
                return {"status": status, "receipt_sha": created["record_sha"], "result": "new"}
        if existing["grant_id"] != grant["grant_id"] or existing["start_sha"] != grant["record_sha"] or existing["binding"] != grant["binding"]:
            raise AgentDeliveryError("Existing receipt is bound to a different start grant.")
        if existing["status"] in TERMINAL_REPORT_STATUSES:
            raise AgentDeliveryError("A terminal receipt already exists; inspect status instead of reporting again.")
        commit_sha = self._commit(record, [existing["record_sha"]])
        ref = self._ref("receipts", grant["execution_key"])
        result = self.client.request("PATCH", f"{self.api}/refs/{ref.removeprefix('refs/')}", {"sha": commit_sha, "force": False}, expected_status=200)
        if not isinstance(result, dict) or result.get("object", {}).get("sha") != commit_sha:
            raise GitHubWritePendingError("Receipt update was not confirmed; inspect status before any further report.")
        return {"status": status, "receipt_sha": commit_sha, "result": "updated"}


def _identity_and_client(repo: Repo, *, pat_file: Path | None, login: str | None, require_write_identity: bool = False) -> tuple[GitHubPAT | None, GitHubClient]:
    if (pat_file is None) != (login is None):
        raise AgentDeliveryError("Provide both --github-pat-file and --github-login, or neither.")
    if require_write_identity and pat_file is None:
        raise AgentDeliveryError("Shared writes require an explicit --github-pat-file and --github-login.")
    identity = None
    if pat_file is not None and login is not None:
        identity = GitHubPAT.from_file(path=pat_file, repo_root=Path.cwd(), repository=repo.slug, expected_login=login)
    client = GitHubClient(repo, token=identity.token_for(repo.slug) if identity else None)
    if identity is not None:
        account = client.request("GET", "/user")
        if not isinstance(account, dict) or str(account.get("login", "")).casefold() != identity.expected_login.casefold():
            raise AgentDeliveryError("PAT account does not match the explicitly expected GitHub login.")
    return identity, client


def _task_args(parser: argparse.ArgumentParser, *, writes: bool = False) -> None:
    parser.add_argument("--repo", required=True, help="Target GitHub repository OWNER/REPO")
    parser.add_argument("--plan-pr", required=True, help="Merged Plan PR URL or OWNER/REPO#NUMBER")
    parser.add_argument("--task-path", required=True, help="Pull task path changed by this Plan PR")
    parser.add_argument("--authority-branch", default="main", help="Required target branch of the merged Plan PR (default: main)")
    parser.add_argument("--github-pat-file", type=Path, help="Explicit fine-grained GitHub PAT file; required for acquire/report")
    parser.add_argument("--github-login", help="Expected GitHub login for the explicit PAT")


def _atomic_private_json(path: Path, value: dict[str, Any]) -> None:
    destination = path.expanduser().absolute()
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = parent.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise AgentDeliveryError("Grant directory must be owned by this user and private (0700 or stricter).")
    if destination.exists() or destination.is_symlink():
        raise AgentDeliveryError("Grant file already exists; choose a new private path.")
    temporary = parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, destination, follow_symlinks=False)
        os.chmod(destination, 0o600, follow_symlinks=False)
        temporary.unlink()
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except FileExistsError as exc:
        raise AgentDeliveryError("Grant file already exists; choose a new private path.") from exc
    except OSError as exc:
        raise AgentDeliveryError("Private grant file could not be safely persisted; do not begin task work.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _replace_private_json(path: Path, value: dict[str, Any]) -> None:
    """Atomically update an existing grant and verify the persisted contents."""
    destination = path.expanduser().absolute()
    parent = destination.parent
    try:
        parent_info = parent.stat()
        current = destination.stat(follow_symlinks=False)
        if (
            parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077
            or not stat.S_ISREG(current.st_mode) or current.st_uid != os.getuid()
            or stat.S_IMODE(current.st_mode) != 0o600 or current.st_nlink != 1
        ):
            raise ValueError("Unsafe grant path")
    except OSError as exc:
        raise AgentDeliveryError("Private grant file could not be safely updated.") from exc
    temporary = parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n"
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if _read_private_json(destination) != value:
            raise ValueError("Persisted grant did not match")
    except (OSError, ValueError) as exc:
        raise AgentDeliveryError("Private grant update could not be confirmed; report remains pending confirmation.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _grant_report_lock(path: Path) -> Iterator[None]:
    grant_path = path.expanduser().absolute()
    parent = grant_path.parent
    lock_path = parent / f".{grant_path.name}.report.lock"
    descriptor: int | None = None
    try:
        parent_info = parent.stat()
        if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077:
            raise ValueError("Unsafe grant directory")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        lock_info = os.fstat(descriptor)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid() or stat.S_IMODE(lock_info.st_mode) != 0o600 or lock_info.st_nlink != 1:
            raise ValueError("Unsafe report lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AgentDeliveryError("A report using this local grant is already in progress.") from exc
        yield
    except OSError as exc:
        raise AgentDeliveryError("Could not lock the private grant for a report.") from exc
    finally:
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _read_private_json(path: Path) -> dict[str, Any]:
    candidate = path.expanduser().absolute()
    descriptor = None
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > MAX_RECORD_BYTES:
            raise ValueError("Unsafe grant file")
        raw = os.read(descriptor, MAX_RECORD_BYTES + 1)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError("Grant is not an object")
        grant = value.get("grant")
        if (
            value.get("protocol_version") != PULL_PROTOCOL_VERSION or not isinstance(grant, dict)
            or grant.get("result") != "new" or grant.get("new_start") is not True
            or not isinstance(grant.get("execution_key"), str) or not isinstance(grant.get("grant_id"), str)
            or not isinstance(grant.get("record_sha"), str) or not isinstance(grant.get("binding"), dict)
        ):
            raise ValueError("Invalid grant")
        gate = value.get("report_gate")
        if not isinstance(gate, dict) or gate.get("state") not in {"ready", "pending_confirmation"}:
            raise ValueError("Missing or invalid local report gate")
        if gate["state"] == "pending_confirmation" and (
            not isinstance(gate.get("request_id"), str) or not isinstance(gate.get("request_sha256"), str)
            or not SHA256_RE.fullmatch(gate["request_sha256"]) or gate.get("status") not in REPORT_STATUSES
        ):
            raise ValueError("Invalid pending report intent")
        return value
    except (OSError, UnicodeError, ValueError) as exc:
        raise AgentDeliveryError("Grant file is missing, unsafe, or invalid; no report was written.") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _mechanism_snapshot() -> dict[str, Any]:
    try:
        from .pull_install import load_install_receipt
    except ImportError:
        return {"installation_status": "uninstalled", "install_sha": None, "skill_sha256": None, "protocol_version": PULL_PROTOCOL_VERSION, "package_version": None, "source": "development"}
    value = load_install_receipt()
    if not isinstance(value, dict):
        raise AgentDeliveryError("Installation receipt is invalid; acquire is blocked.")
    allowed = ("installation_status", "install_sha", "skill_sha256", "protocol_version", "package_version", "last_successful_update", "source", "ref")
    snapshot = {key: value.get(key) for key in allowed if key in value}
    if snapshot.get("installation_status") not in {"installed", "uninstalled"}:
        raise AgentDeliveryError("Installation receipt has an invalid installation_status.")
    if snapshot.get("installation_status") == "installed":
        if not isinstance(snapshot.get("install_sha"), str) or not SHA_RE.fullmatch(snapshot["install_sha"]):
            raise AgentDeliveryError("Installed source SHA is missing or invalid; acquire is blocked.")
        if not isinstance(snapshot.get("skill_sha256"), str) or not SHA256_RE.fullmatch(snapshot["skill_sha256"]):
            raise AgentDeliveryError("Installed Skill SHA-256 is missing or invalid; acquire is blocked.")
        if snapshot.get("protocol_version") != PULL_PROTOCOL_VERSION:
            raise AgentDeliveryError("Installed pull protocol does not match this CLI; acquire is blocked.")
    else:
        snapshot.setdefault("install_sha", None)
        snapshot.setdefault("skill_sha256", None)
        snapshot.setdefault("protocol_version", PULL_PROTOCOL_VERSION)
        snapshot.setdefault("source", "development")
    return snapshot


def _declared_capabilities(values: list[str]) -> set[str]:
    result: set[str] = set()
    for value in values:
        for capability in value.split(","):
            capability = capability.strip()
            if not capability or len(capability) > 100 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]*", capability):
                raise AgentDeliveryError("Local capability declarations must be short identifiers.")
            result.add(capability)
    return result


def _capability_mismatch(task: dict[str, Any], declared: set[str]) -> list[str]:
    return sorted(set(task["required_capabilities"]) - declared)


def _discover(client: GitHubClient, *, branch: str, limit: int) -> dict[str, Any]:
    candidates: list[dict[str, Any]] = []
    page = 1
    inspected = 0
    while inspected < limit:
        pulls = client.request("GET", f"/repos/{client.repo.slug}/pulls?state=closed&base={branch}&sort=updated&direction=desc&per_page=100&page={page}")
        if not isinstance(pulls, list):
            raise AgentDeliveryError("GitHub did not return a pull request list.")
        if not pulls:
            break
        for pull in pulls:
            if inspected >= limit:
                break
            if not isinstance(pull, dict) or not pull.get("merged_at"):
                continue
            base = pull.get("base")
            if not isinstance(base, dict) or base.get("ref") != branch:
                continue
            number = pull.get("number")
            if not isinstance(number, int) or number < 1:
                continue
            inspected += 1
            for item in client.pull_files(number):
                filename = item.get("filename")
                if isinstance(filename, str) and TASK_FILE_RE.fullmatch(filename) and item.get("status") in {"added", "modified"}:
                    candidates.append({
                        "plan_pr": f"https://github.com/{client.repo.slug}/pull/{number}",
                        "task_path": filename,
                        "merge_sha": pull.get("merge_commit_sha"),
                        "status": "candidate_unverified",
                        "next_actions": ["Run agent-pull show with this Plan PR and task path to verify its fixed task and context."],
                    })
        if len(pulls) < 100:
            break
        page += 1
        if page > 10:
            break
    return {
        "status": "candidates_found" if candidates else "no_candidates",
        "summary": f"Found {len(candidates)} pull task candidate(s) among {inspected} recent merged Plan PR(s).",
        "repository": client.repo.slug,
        "authority_branch": branch,
        "candidates": candidates,
        "artifacts": [],
        "next_actions": ["Verify a candidate with show; discovery does not grant permission to execute."] if candidates else ["No eligible pull task candidate was found in the inspected PR window."],
    }


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _common_package(args: argparse.Namespace, *, require_write_identity: bool = False) -> tuple[GitHubPAT | None, GitHubClient, dict[str, Any]]:
    repo, _number, _url = parse_plan_pr_ref(args.plan_pr)
    if repo.slug.casefold() != args.repo.casefold():
        raise AgentDeliveryError("--repo does not match the repository in --plan-pr.")
    identity, client = _identity_and_client(repo, pat_file=args.github_pat_file, login=args.github_login, require_write_identity=require_write_identity)
    package = _load_task_package(client, plan_pr=args.plan_pr, task_path=args.task_path, authority_branch=args.authority_branch)
    return identity, client, package


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-pull", description="Discover, verify, acquire, and report shared pull tasks for the current Agent session.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover = subparsers.add_parser("discover", help="List candidate pull tasks authorized by recent merged Plan PRs")
    discover.add_argument("--repo", required=True, help="Target GitHub repository OWNER/REPO")
    discover.add_argument("--authority-branch", default="main")
    discover.add_argument("--limit", type=int, default=30, help="Maximum merged PRs to inspect (1-100)")
    discover.add_argument("--github-pat-file", type=Path)
    discover.add_argument("--github-login")
    show = subparsers.add_parser("show", help="Verify and return the immutable task and context package")
    _task_args(show)
    acquire = subparsers.add_parser("acquire", help="Check local declarations, atomically acquire the one-time start grant, and return the task package")
    _task_args(acquire, writes=True)
    acquire.add_argument("--capability", action="append", default=[], help="Locally available capability (repeat or comma-separate); harness-declared")
    acquire.add_argument("--claimant-id", help="Optional stable local identifier; otherwise a random invocation ID is used")
    acquire.add_argument("--harness", required=True, help="Name of the already-active harness; no Agent process is started")
    acquire.add_argument("--grant-file", required=True, type=Path, help="New private local path for this invocation's grant")
    report = subparsers.add_parser("report", help="Write a shared task receipt using this invocation's private grant")
    _task_args(report, writes=True)
    report.add_argument("--grant-file", required=True, type=Path)
    report.add_argument("--status", required=True, choices=sorted(REPORT_STATUSES))
    report.add_argument("--summary", required=True)
    report.add_argument("--next-action", action="append", default=[])
    report.add_argument("--artifact", action="append", default=[])
    status = subparsers.add_parser("status", help="Read a task's cross-host claim, start, and receipt state")
    _task_args(status)
    args = parser.parse_args(argv)
    try:
        if args.command == "discover":
            branch = _authority_branch(args.authority_branch)
            if not 1 <= args.limit <= 100:
                raise AgentDeliveryError("Discover limit must be between 1 and 100 merged PRs.")
            repo = Repo(*args.repo.split("/")) if re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9_.-]+", args.repo) else None
            if repo is None:
                raise AgentDeliveryError("--repo must be OWNER/REPO.")
            _identity, client = _identity_and_client(repo, pat_file=args.github_pat_file, login=args.github_login)
            result = _discover(client, branch=branch, limit=args.limit)
        elif args.command == "show":
            _identity, _client, package = _common_package(args)
            result = {"status": "eligible_for_capability_check", "summary": "Task authorization, immutable revision, and every context digest were verified.", "can_execute": False, **package, "artifacts": [], "next_actions": ["Check local required capabilities, then call acquire from this active harness session."]}
        elif args.command == "acquire":
            if len(args.harness) > 120 or not args.harness.strip():
                raise AgentDeliveryError("Harness name must be non-empty and no longer than 120 characters.")
            local_capabilities = _declared_capabilities(args.capability)
            _identity, client, package = _common_package(args, require_write_identity=True)
            missing = _capability_mismatch(package["task"], local_capabilities)
            if missing:
                result = {
                    "status": "not_eligible_here", "can_execute": False,
                    "summary": "The local harness did not declare every required capability; no shared claim or start write was attempted.",
                    **package, "missing_capabilities": missing,
                    "capability_check": {"source": "harness_declared", "declared": sorted(local_capabilities), "verified_by_cli": False},
                    "artifacts": [], "next_actions": ["Run on a harness that can truthfully declare the missing capabilities."],
                }
            else:
                # Verify grant target is writable before spending the one-time shared start.
                target = args.grant_file.expanduser().absolute()
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                parent_info = target.parent.stat()
                if parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) & 0o077 or target.exists() or target.is_symlink():
                    raise AgentDeliveryError("Grant target must be absent inside a user-owned private directory (0700 or stricter).")
                mechanism = _mechanism_snapshot()
                claimant_id = args.claimant_id.strip() if args.claimant_id else str(uuid.uuid4())
                if not claimant_id or len(claimant_id) > 200 or "\x00" in claimant_id:
                    raise AgentDeliveryError("Claimant ID must be non-empty and no longer than 200 characters.")
                request_id = str(uuid.uuid4())
                grant = PullExecutionStore(client).acquire(package["execution_key"], claimant_id=claimant_id, request_id=request_id, binding=package["binding"])
                can_execute = grant.get("result") == "new" and grant.get("new_start") is True
                if can_execute:
                    local_grant = {
                        "protocol_version": PULL_PROTOCOL_VERSION,
                        "task_package_version": package["task_package_version"],
                        "grant": grant,
                        "executor": {"harness": args.harness.strip(), "mechanism": mechanism, "capability_source": "harness_declared", "declared_capabilities": sorted(local_capabilities)},
                        "report_gate": {"state": "ready"},
                    }
                    try:
                        _atomic_private_json(args.grant_file, local_grant)
                    except AgentDeliveryError:
                        result = {
                            "status": "pending_confirmation", "can_execute": False,
                            "summary": "The shared start was created, but this invocation could not safely persist its grant; task work must not begin.",
                            **package, "grant_id": grant["grant_id"], "start_sha": grant["record_sha"],
                            "artifacts": [],
                            "next_actions": ["Run status to inspect the start record and grant file path. Do not reacquire or begin task work."],
                        }
                    else:
                        result = {
                            "status": "acquired", "can_execute": True,
                            "summary": "This invocation created the shared start record and privately saved its grant; the current Agent may now perform the authorized task.",
                            **package, "grant_id": grant["grant_id"], "start_sha": grant["record_sha"],
                            "grant_file": str(args.grant_file.expanduser().absolute()),
                            "executor": local_grant["executor"], "artifacts": [],
                            "next_actions": ["The already-active harness executes the task. Then call report using this grant file."],
                        }
                else:
                    result = {
                        "status": "pending_confirmation" if grant.get("result") == "unknown" or grant.get("status") == "pending_confirmation" else "already_claimed_or_started",
                        "can_execute": False,
                        "summary": "No new start permission was returned to this invocation.",
                        **package, "claimant_id": grant.get("claimant_id"), "grant_id": grant.get("grant_id"),
                        "result": grant.get("result"), "artifacts": [],
                        "next_actions": ["Use status to inspect the shared record. Do not rerun or infer permission from an existing grant."],
                    }
        elif args.command == "report":
            stored = _read_private_json(args.grant_file)
            _identity, client, package = _common_package(args, require_write_identity=True)
            if stored.get("task_package_version") != package["task_package_version"]:
                raise AgentDeliveryError("Grant file is for a different fixed task package; no receipt was written.")
            grant = stored["grant"]
            if grant.get("execution_key") != package["execution_key"] or grant.get("binding") != package["binding"]:
                raise AgentDeliveryError("Grant file binding does not match the fixed task package.")
            artifacts = _string_list(args.artifact, label="artifacts")
            next_actions = _string_list(args.next_action, label="next_action")
            if any(len(value) > 2000 or not re.match(r"^(https://|shared://)", value) for value in artifacts):
                raise AgentDeliveryError("Artifacts must be cross-host https:// or shared:// references up to 2000 characters.")
            executor = stored.get("executor")
            if not isinstance(executor, dict):
                raise AgentDeliveryError("Grant file does not contain its fixed execution endpoint metadata.")
            if not args.summary.strip() or len(args.summary) > 8000:
                raise AgentDeliveryError("Report summary must be non-empty and no longer than 8000 characters.")
            report_time = datetime.now(UTC).isoformat()
            report_preview = {
                "schema_version": 1, "repository": client.repo.slug.casefold(),
                "execution_key": grant["execution_key"], "kind": "receipts", "status": args.status,
                "claimant_id": grant["claimant_id"], "request_id": grant["request_id"],
                "binding": grant["binding"], "start_sha": grant["record_sha"],
                "grant_id": grant["grant_id"], "summary": args.summary.strip(),
                "artifacts": artifacts, "next_actions": next_actions,
                "executor": executor, "reported_at": report_time,
            }
            if len(_json_bytes(report_preview)) > MAX_RECORD_BYTES:
                raise AgentDeliveryError("Pull receipt exceeds the shared size limit; no report intent was saved.")
            request_id = str(uuid.uuid4())
            request_sha256 = _sha256(_json_bytes({
                "task_package_version": package["task_package_version"],
                "execution_key": grant["execution_key"], "grant_id": grant["grant_id"],
                "status": args.status, "summary": args.summary.strip(),
                "artifacts": artifacts, "next_actions": next_actions, "executor": executor,
            }))
            store = PullExecutionStore(client)
            with _grant_report_lock(args.grant_file):
                current = _read_private_json(args.grant_file)
                if (
                    current.get("task_package_version") != package["task_package_version"]
                    or current["grant"].get("grant_id") != grant["grant_id"]
                ):
                    raise AgentDeliveryError("The retained grant changed before report; no shared write was attempted.")
                gate = current["report_gate"]
                if gate.get("state") == "pending_confirmation":
                    result = {
                        "status": "pending_confirmation", "summary": "This grant has an unresolved local report intent; the report was not sent again.",
                        "can_execute": False, "task_package_version": package["task_package_version"],
                        "execution_key": grant["execution_key"], "grant_id": grant["grant_id"],
                        "start_sha": grant["record_sha"], "artifacts": [],
                        "pending_report": {key: gate.get(key) for key in ("request_id", "request_sha256", "status", "prepared_at")},
                        "next_actions": ["Run status to inspect the shared receipt. A human must resolve the pending report before this grant can report again."],
                    }
                elif gate.get("last_request_sha256") == request_sha256:
                    result = {
                        "status": "already_reported", "summary": "This exact report request was already confirmed locally; no shared write was repeated.",
                        "can_execute": False, "task_package_version": package["task_package_version"],
                        "execution_key": grant["execution_key"], "grant_id": grant["grant_id"],
                        "start_sha": grant["record_sha"], "receipt_sha": gate.get("last_receipt_sha"),
                        "artifacts": artifacts,
                        "next_actions": ["Run status to inspect the shared receipt. Use a new report only for new progress or a changed outcome."],
                    }
                else:
                    pending = {
                        **current,
                        "report_gate": {
                            "state": "pending_confirmation", "request_id": request_id,
                            "request_sha256": request_sha256, "status": args.status,
                            "prepared_at": datetime.now(UTC).isoformat(),
                        },
                    }
                    try:
                        _replace_private_json(args.grant_file, pending)
                    except AgentDeliveryError:
                        result = {
                            "status": "pending_confirmation", "summary": "The local report intent could not be safely confirmed; the shared report was not sent.",
                            "can_execute": False, "task_package_version": package["task_package_version"],
                            "execution_key": grant["execution_key"], "grant_id": grant["grant_id"],
                            "start_sha": grant["record_sha"], "artifacts": [],
                            "pending_report": {"request_id": request_id, "request_sha256": request_sha256, "status": args.status},
                            "next_actions": ["Inspect the private grant file and shared status manually. Do not repeat this report automatically."],
                        }
                    else:
                        try:
                            receipt = store.write_receipt(
                                grant, status=args.status, summary=args.summary,
                                artifacts=artifacts, next_actions=next_actions,
                                executor=executor, reported_at=report_time,
                            )
                        except Exception:
                            result = {
                                "status": "pending_confirmation", "summary": "The report did not receive a confirmed success; its local intent remains pending and no repeat was attempted.",
                                "can_execute": False, "task_package_version": package["task_package_version"],
                                "execution_key": grant["execution_key"], "grant_id": grant["grant_id"],
                                "start_sha": grant["record_sha"], "artifacts": artifacts,
                                "pending_report": pending["report_gate"],
                                "next_actions": ["Run status to inspect the shared receipt. This grant will not send another report while the local intent is pending."],
                            }
                        else:
                            ready = {
                                **pending,
                                "report_gate": {
                                    "state": "ready", "last_request_id": request_id,
                                    "last_request_sha256": request_sha256,
                                    "last_receipt_sha": receipt["receipt_sha"],
                                    "last_status": args.status,
                                    "confirmed_at": datetime.now(UTC).isoformat(),
                                },
                            }
                            try:
                                _replace_private_json(args.grant_file, ready)
                            except AgentDeliveryError:
                                # Restore the conservative gate when a ready update was not confirmed.
                                try:
                                    _replace_private_json(args.grant_file, pending)
                                except AgentDeliveryError:
                                    pass
                                result = {
                                    "status": "pending_confirmation", "summary": "The shared receipt succeeded, but the local ready state could not be confirmed; this grant remains blocked from another report.",
                                    "can_execute": False, "task_package_version": package["task_package_version"],
                                    "execution_key": grant["execution_key"], "grant_id": grant["grant_id"],
                                    "start_sha": grant["record_sha"], "receipt_sha": receipt["receipt_sha"],
                                    "pending_report": pending["report_gate"],
                                    "artifacts": artifacts,
                                    "next_actions": ["Run status to inspect the shared receipt and manually inspect the local grant. Do not report again."],
                                }
                            else:
                                result = {
                                    "status": receipt["status"], "summary": args.summary.strip(), "can_execute": False,
                                    "task_package_version": package["task_package_version"], "execution_key": package["execution_key"],
                                    "grant_id": grant["grant_id"], "start_sha": grant["record_sha"],
                                    "receipt_sha": receipt["receipt_sha"], "result": receipt["result"],
                                    "artifacts": artifacts, "next_actions": next_actions,
                                }
        else:
            _identity, client, package = _common_package(args)
            result = PullExecutionStore(client).inspect_pull(package["execution_key"])
            if result.get("status") != "absent" and result.get("binding") != package["binding"]:
                result = {
                    "status": "authorization_conflict",
                    "summary": "A shared execution record exists for this task id and revision, but its fixed content or authorization differs.",
                    "can_execute": False, "execution_key": package["execution_key"],
                    "task_package_version": package["task_package_version"],
                    "existing_task_sha256": result.get("binding", {}).get("task_sha256"),
                    "requested_task_sha256": package["binding"]["task_sha256"],
                    "artifacts": [],
                    "next_actions": ["Inspect the original authorization and shared execution record. A content change requires a new task revision; do not reacquire."],
                }
            result.update({
                "task_package_version": package["task_package_version"],
                "summary": result.get("summary") or f"Shared task status is {result['status']}.",
                "can_execute": False,
                "artifacts": result.get("artifacts", []),
                "next_actions": result.get("next_actions", ["No further action is recorded."]),
            })
        _print(result)
        return 0
    except Exception as exc:
        message = str(exc) if isinstance(exc, AgentDeliveryError) else "Unexpected failure; no further write was attempted."
        print(f"agent-pull: {message}", file=sys.stderr)
        return 2
