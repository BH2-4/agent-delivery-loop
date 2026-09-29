"""Git operations for isolated worktrees and App-authenticated publishing."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import AgentDeliveryError
from .github import Repo


def git(cwd: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AgentDeliveryError("A required Git operation could not be started or timed out.") from exc
    if check and result.returncode != 0:
        action = args[0] if args else "operation"
        raise AgentDeliveryError(f"Git {action} failed; inspect the repository state before continuing.")
    return result


def repository_root(path: Path) -> Path:
    result = git(path, "rev-parse", "--show-toplevel")
    return Path(result.stdout.strip()).resolve()


def repository_remote(root: Path) -> Repo:
    result = git(root, "remote", "get-url", "origin")
    remote = result.stdout.strip()
    match = re.fullmatch(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?", remote)
    if match:
        return Repo(match.group(1), match.group(2))
    match = re.fullmatch(r"git@github\.com:([A-Za-z0-9-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?", remote)
    if match:
        return Repo(match.group(1), match.group(2))
    raise AgentDeliveryError("origin must point to this repository on github.com using HTTPS or SSH.")


def ensure_plan_is_on_main(root: Path, repo: Repo, merge_sha: str) -> None:
    ref = "refs/remotes/agent-delivery-loop/main"
    git(root, "fetch", "--no-tags", repo.https_url, f"+refs/heads/main:{ref}")
    git(root, "cat-file", "-e", f"{merge_sha}^{{commit}}")
    result = git(root, "merge-base", "--is-ancestor", merge_sha, ref, check=False)
    if result.returncode != 0:
        raise AgentDeliveryError("The Plan PR merge commit is not an ancestor of the current main branch.")


def create_worktree(root: Path, destination: Path, branch: str, base_sha: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if destination.exists():
        raise AgentDeliveryError("The isolated worktree path already exists; refusing to reuse it.")
    git(root, "worktree", "add", "-b", branch, str(destination), base_sha)


def changed_paths(worktree: Path) -> list[str]:
    tracked = git(worktree, "diff", "--name-only", "-z", "HEAD").stdout
    untracked = git(worktree, "ls-files", "--others", "-z").stdout
    values = set()
    for output in (tracked, untracked):
        values.update(item for item in output.split("\0") if item)
    return sorted(values)


def stage_changes(worktree: Path) -> list[str]:
    git(worktree, "add", "--all", "--", ".")
    paths = git(worktree, "diff", "--cached", "--name-only", "-z", "HEAD").stdout
    staged = sorted(item for item in paths.split("\0") if item)
    if not staged:
        raise AgentDeliveryError(
            "No deliverable changes were staged; ignored or otherwise uncommitted files cannot be reported as a delivery."
        )
    whitespace = git(worktree, "diff", "--cached", "--check", "HEAD", check=False)
    if whitespace.returncode != 0:
        raise AgentDeliveryError("The proposed changes contain whitespace errors; no commit was created.")
    return staged


def ensure_diff_clean(worktree: Path) -> None:
    result = git(worktree, "diff", "--check", "HEAD", check=False)
    if result.returncode != 0:
        raise AgentDeliveryError("The proposed changes contain whitespace errors; no commit was created.")


def commit_changes(worktree: Path, task_identity: str) -> str:
    whitespace = git(worktree, "diff", "--cached", "--check", "HEAD", check=False)
    if whitespace.returncode != 0:
        raise AgentDeliveryError("The proposed changes contain whitespace errors; no commit was created.")
    diff = git(worktree, "diff", "--cached", "--quiet", "HEAD", check=False)
    if diff.returncode == 0:
        raise AgentDeliveryError("Claude Code produced no file changes; no Delivery PR was created.")
    if diff.returncode != 1:
        raise AgentDeliveryError("Could not inspect the staged changes.")
    git(
        worktree,
        "-c",
        "user.name=Agent Delivery Loop",
        "-c",
        "user.email=agent-delivery-loop@invalid",
        "commit",
        "-m",
        f"Delivery {task_identity}",
    )
    return git(worktree, "rev-parse", "HEAD").stdout.strip()


def push_branch_with_app_token(worktree: Path, repo: Repo, branch: str, token: str) -> None:
    askpass_source = """#!/bin/sh
case "$1" in
  *Username*) printf '%s\\n' 'x-access-token' ;;
  *Password*) printf '%s\\n' "$AGENT_GIT_PASSWORD" ;;
  *) exit 1 ;;
esac
"""
    with tempfile.TemporaryDirectory(prefix="agent-delivery-askpass-") as temporary:
        askpass = Path(temporary) / "askpass"
        askpass.write_text(askpass_source, encoding="utf-8")
        askpass.chmod(0o700)
        child_env = os.environ.copy()
        for name in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "GH_ENTERPRISE_TOKEN",
            "GIT_ASKPASS",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "OPENAI_API_KEY",
        ):
            child_env.pop(name, None)
        child_env.update(
            {
                "GIT_ASKPASS": str(askpass),
                "GIT_TERMINAL_PROMPT": "0",
                "AGENT_GIT_PASSWORD": token,
            }
        )
        result = git(
            worktree,
            "-c",
            "credential.helper=",
            "push",
            repo.https_url,
            f"HEAD:refs/heads/{branch}",
            env=child_env,
            check=False,
        )
        if result.returncode != 0:
            raise AgentDeliveryError("GitHub App could not push the Delivery branch.")
