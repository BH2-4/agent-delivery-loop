"""Small GitHub REST client for public authorization reads and PR publishing."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .errors import AgentDeliveryError

API_ROOT = "https://api.github.com"
WORK_ORDER_FILE_RE = re.compile(r"^\.agents/work-orders/WO-[A-Z0-9-]+-r[1-9][0-9]*\.json$")
MERGED_PR_PAGE_SIZE = 100
MAX_CLOSED_PR_PAGES = 10
PLAN_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>[1-9][0-9]*)/?$"
)
# Read-only reliability bounds: at most two bounded retries per request.
READ_RETRY_DELAYS_SECONDS = (2.0, 8.0)
READ_ATTEMPT_TIMEOUT_SECONDS = 30
READ_TOTAL_BUDGET_SECONDS = 120
_CONTENT_CACHE: dict[tuple[str, str, str], tuple[str, bytes]] = {}
_CONTENT_CACHE_LIMIT = 32


class GitHubReadError(AgentDeliveryError):
    """A read-only query stayed failed after bounded, classified retries."""


class GitHubWritePendingError(AgentDeliveryError):
    """A write request's outcome is unknown; it must never be blindly replayed."""


def _rate_reset_remaining(headers: Any) -> int | None:
    if headers is None or not hasattr(headers, "get"):
        return None
    try:
        if str(headers.get("X-RateLimit-Remaining")) == "0":
            return int(headers.get("X-RateLimit-Reset", "0"))
    except (TypeError, ValueError, AttributeError):
        return None
    return None


@dataclass(frozen=True, slots=True)
class Repo:
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def https_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.name}.git"


@dataclass(frozen=True, slots=True)
class PlanAuthorization:
    repo: Repo
    number: int
    url: str
    merge_sha: str
    order_path: str
    order_bytes: bytes


def parse_plan_pr_ref(value: str) -> tuple[Repo, int, str]:
    match = PLAN_URL_RE.fullmatch(value.strip())
    if match:
        repo = Repo(match.group("owner"), match.group("repo"))
        return repo, int(match.group("number")), value.strip().rstrip("/")
    issue_match = re.fullmatch(
        r"(?P<owner>[A-Za-z0-9-]+)/(?P<repo>[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]*)",
        value.strip(),
    )
    if issue_match:
        repo = Repo(issue_match.group("owner"), issue_match.group("repo"))
        number = int(issue_match.group("number"))
        return repo, number, f"https://github.com/{repo.slug}/pull/{number}"
    raise AgentDeliveryError("Plan PR must be a github.com pull URL or OWNER/REPO#NUMBER.")


class GitHubClient:
    def __init__(self, repo: Repo, token: str | None = None) -> None:
        self.repo = repo
        self._token = token

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if not path.startswith("/") or ".." in path.split("/"):
            raise AgentDeliveryError("Invalid GitHub API path.")
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            API_ROOT + path,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "agent-delivery-loop/0.1",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": f"Bearer {self._token}"} if self._token else {}),
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        deadline = time.monotonic() + READ_TOTAL_BUDGET_SECONDS
        last_failure = "unknown"
        waited_for_rate = False
        is_read = method.upper() == "GET"
        retries_left = len(READ_RETRY_DELAYS_SECONDS) if is_read else 0
        for attempt in range(retries_left + 1):
            remaining = deadline - time.monotonic()
            if remaining < 1.0:
                # Exhausted (or sub-second) budget never starts a request, and a
                # per-attempt timeout never exceeds the remaining time. Nothing was
                # sent, so a write here is a plain failure, not an unknown outcome.
                raise AgentDeliveryError(
                    "GitHub API request was not started: the read budget was already exhausted."
                ) from None
            attempt_timeout = min(READ_ATTEMPT_TIMEOUT_SECONDS, remaining)
            try:
                with urllib.request.urlopen(request, timeout=attempt_timeout) as response:
                    body = response.read()
                break
            except urllib.error.HTTPError as exc:
                try:
                    raw_body = exc.read()
                except OSError:
                    raw_body = b""
                body_text = raw_body[:2000].decode("utf-8", "replace")
                rate_reset = _rate_reset_remaining(exc.headers)
                if exc.code in (401, 403) and rate_reset is not None:
                    wait = rate_reset - int(time.time()) + 1
                    if is_read and not waited_for_rate and 0 < wait <= deadline - time.monotonic() and attempt < retries_left:
                        waited_for_rate = True
                        time.sleep(wait)
                        last_failure = f"rate limited (resets in ~{wait}s); waited once"
                        continue
                    raise GitHubReadError(f"GitHub rate limit persists beyond the bounded wait (HTTP {exc.code}).") from None
                if is_read and (exc.code in (429,) or (exc.code == 403 and "rate limit" in body_text.lower())):
                    if attempt < retries_left and time.monotonic() < deadline:
                        time.sleep(READ_RETRY_DELAYS_SECONDS[attempt])
                        last_failure = f"rate limited (HTTP {exc.code})"
                        continue
                    raise GitHubReadError(f"GitHub rate limiting persisted after bounded retries (HTTP {exc.code}).") from None
                if is_read and exc.code >= 500 and attempt < retries_left and time.monotonic() < deadline:
                    time.sleep(READ_RETRY_DELAYS_SECONDS[attempt])
                    last_failure = f"server error (HTTP {exc.code})"
                    continue
                if not is_read and exc.code >= 500:
                    # The write may have been applied before the server error; outcome unknown.
                    raise GitHubWritePendingError(
                        f"GitHub write (HTTP {exc.code}) returned a server error; the remote result must be verified read-only."
                    ) from None
                # 404, 401/403 without rate-limit markers, 4xx: real errors, never retried.
                raise GitHubReadError(f"GitHub API request failed with HTTP {exc.code}.") from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_failure = f"network ({type(exc).__name__})"
                if attempt < retries_left and time.monotonic() < deadline:
                    time.sleep(READ_RETRY_DELAYS_SECONDS[attempt])
                    continue
                if not is_read:
                    raise GitHubWritePendingError(
                        "GitHub write lost its response; the remote result must be verified read-only before any repeat."
                    ) from exc
                raise GitHubReadError(
                    f"GitHub API could not be reached after bounded retries; last failure: {last_failure}."
                ) from exc
        else:  # pragma: no cover - loop always breaks or raises
            raise GitHubReadError("GitHub API read did not complete.")
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise AgentDeliveryError("GitHub returned an unreadable API response.") from exc

    def get_pull(self, number: int) -> dict[str, Any]:
        result = self.request("GET", f"/repos/{self.repo.slug}/pulls/{number}")
        if not isinstance(result, dict):
            raise AgentDeliveryError("GitHub did not return a pull request.")
        return result

    def pull_files(self, number: int) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for page in range(1, 11):
            current = self.request(
                "GET",
                f"/repos/{self.repo.slug}/pulls/{number}/files?per_page=100&page={page}",
            )
            if not isinstance(current, list):
                raise AgentDeliveryError("GitHub did not return the pull request file list.")
            result.extend(item for item in current if isinstance(item, dict))
            if len(current) < 100:
                return result
        raise AgentDeliveryError("Plan PR changes more files than this first version can inspect.")

    def content(self, path: str, ref: str) -> bytes:
        encoded_path = urllib.parse.quote(path, safe="/")
        encoded_ref = urllib.parse.quote(ref, safe="")
        # Fixed-ref file content is immutable; a verified (path, ref) result may be reused.
        cache_key = (self.repo.slug.casefold(), path, ref)
        cached = _CONTENT_CACHE.get(cache_key)
        if cached is not None:
            blob_sha, decoded = cached
        else:
            result = self.request(
                "GET",
                f"/repos/{self.repo.slug}/contents/{encoded_path}?ref={encoded_ref}",
            )
            if not isinstance(result, dict) or result.get("type") != "file" or result.get("encoding") != "base64":
                raise AgentDeliveryError(f"Authorized file is missing or is not a regular file: {path}.")
            content = result.get("content")
            blob_sha = result.get("sha")
            if not isinstance(content, str) or not isinstance(blob_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", blob_sha):
                raise AgentDeliveryError("GitHub file response did not contain file content.")
            try:
                decoded = base64.b64decode("".join(content.split()), validate=True)
            except (ValueError, binascii.Error) as exc:
                raise AgentDeliveryError("GitHub file content could not be decoded.") from exc
            git_blob = hashlib.sha1(f"blob {len(decoded)}\0".encode() + decoded).hexdigest()
            if git_blob != blob_sha:
                raise AgentDeliveryError("GitHub file content did not match the blob SHA returned by GitHub.")
            if len(_CONTENT_CACHE) >= _CONTENT_CACHE_LIMIT:
                _CONTENT_CACHE.clear()
            _CONTENT_CACHE[cache_key] = (blob_sha, decoded)
        return decoded

    def authorized_plan(self, number: int, order_path: str, url: str) -> PlanAuthorization:
        if not WORK_ORDER_FILE_RE.fullmatch(order_path):
            raise AgentDeliveryError("Work Order path must include its task ID and revision under .agents/work-orders/.")
        pull = self.get_pull(number)
        if pull.get("state") != "closed" or not pull.get("merged_at"):
            raise AgentDeliveryError("Plan PR has not been merged; execution is not authorized.")
        base = pull.get("base")
        base_repo = base.get("repo") if isinstance(base, dict) else None
        if not isinstance(base, dict) or base.get("ref") != "main":
            raise AgentDeliveryError("Plan PR must target main.")
        if not isinstance(base_repo, dict) or base_repo.get("full_name", "").casefold() != self.repo.slug.casefold():
            raise AgentDeliveryError("Plan PR belongs to a different repository.")
        merge_sha = pull.get("merge_commit_sha")
        if not isinstance(merge_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", merge_sha):
            raise AgentDeliveryError("GitHub did not provide a merged commit SHA for the Plan PR.")
        files = self.pull_files(number)
        if not any(item.get("filename") == order_path and item.get("status") in {"added", "modified"} for item in files):
            raise AgentDeliveryError("The requested Work Order was not added or modified by this Plan PR.")
        order_bytes = self.content(order_path, merge_sha)
        return PlanAuthorization(
            repo=self.repo,
            number=number,
            url=url,
            merge_sha=merge_sha,
            order_path=order_path,
            order_bytes=order_bytes,
        )

    def merged_plan_candidates(self, limit: int = 30) -> list[tuple[int, str, str]]:
        candidates: list[tuple[int, str, str]] = []
        inspected = 0
        seen: set[int] = set()
        for page in range(1, MAX_CLOSED_PR_PAGES + 1):
            pulls = self.request(
                "GET",
                f"/repos/{self.repo.slug}/pulls?state=closed&base=main&sort=updated&direction=desc&per_page={MERGED_PR_PAGE_SIZE}&page={page}",
            )
            if not isinstance(pulls, list):
                raise AgentDeliveryError("GitHub did not return the recent pull request list.")
            for pull in pulls:
                if not isinstance(pull, dict) or not pull.get("merged_at"):
                    continue
                number = pull.get("number")
                base = pull.get("base")
                if (
                    not isinstance(number, int)
                    or number in seen
                    or not isinstance(base, dict)
                    or base.get("ref") != "main"
                ):
                    continue
                seen.add(number)
                inspected += 1
                for item in self.pull_files(number):
                    path = item.get("filename")
                    if (
                        isinstance(path, str)
                        and WORK_ORDER_FILE_RE.fullmatch(path)
                        and item.get("status") in {"added", "modified"}
                    ):
                        candidates.append((number, path, f"https://github.com/{self.repo.slug}/pull/{number}"))
                if inspected >= limit:
                    return candidates
            if len(pulls) < MERGED_PR_PAGE_SIZE:
                return candidates
        raise AgentDeliveryError(
            f"Discovery reached the {MAX_CLOSED_PR_PAGES * MERGED_PR_PAGE_SIZE}-PR safety limit before checking "
            f"{limit} merged PRs; rerun after reviewing repository activity. No-task status was not assumed."
        )

    def create_pull_request(self, *, head: str, title: str, body: str) -> str:
        result = self.request(
            "POST",
            f"/repos/{self.repo.slug}/pulls",
            {"title": title, "head": head, "base": "main", "body": body, "draft": False},
        )
        if not isinstance(result, dict) or not isinstance(result.get("html_url"), str):
            raise AgentDeliveryError("GitHub did not confirm Delivery PR creation.")
        return result["html_url"]
