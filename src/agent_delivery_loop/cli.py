"""Command-line interface for the first local delivery-loop version."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from .claude_worker import ClaudeConfig, SUPPORTED_EFFORTS
from .errors import AgentDeliveryError
from .git_ops import repository_remote, repository_root
from .github import parse_plan_pr_ref
from .github_identity import GitHubPAT
from .runner import execute_plan, watch_once
from .review_cli import (
    MAX_REVIEW_TIMEOUT_SECONDS,
    MIN_REVIEW_TIMEOUT_SECONDS,
    load_verified_review_record,
    run_review_process,
)
from .review_handoff import check_evidence, check_review, prepare_review
from .work_order import parse_work_order


def _print_result(result: dict[str, object]) -> None:
    fields = (
        "status",
        "run_id",
        "task_id",
        "revision",
        "session_id",
        "completion_status",
        "incomplete_items",
        "plan_merge_sha",
        "work_order_sha256",
        "skill_sha256",
        "claude_code_version",
        "requested_model",
        "requested_effort",
        "auth_source",
        "auth_env_name",
        "delivery_branch",
        "delivery_commit",
        "delivery_pr",
        "changed_paths",
        "worktree_id",
    )
    print(json.dumps({key: result.get(key) for key in fields}, ensure_ascii=False, indent=2, sort_keys=True))


def _add_worker_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", required=True, help="Explicit Claude model identifier")
    parser.add_argument("--base-url", required=True, help="Explicit Claude API base URL")
    parser.add_argument("--effort", required=True, choices=SUPPORTED_EFFORTS, help="Explicit Claude effort level")
    parser.add_argument("--auth-config", required=True, type=Path, help="Claude settings JSON containing env credentials")


def _worker_config(args: argparse.Namespace) -> tuple[Path, ClaudeConfig]:
    root = repository_root(args.repo_path.expanduser().resolve())
    config = ClaudeConfig.from_explicit(
        model=args.model,
        base_url=args.base_url,
        effort=args.effort,
        auth_config=args.auth_config,
        repo_root=root,
    )
    return root, config


def _add_github_identity_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--github-pat-file", required=True, type=Path, help="Private fine-grained PAT file outside Git/Worker state")
    parser.add_argument("--github-login", required=True, help="Expected GitHub account; never inferred from gh login")


def _report_error(exc: Exception) -> int:
    message = str(exc) if isinstance(exc, AgentDeliveryError) else "Unexpected failure; sensitive subprocess output was suppressed."
    print(f"agent-delivery: {message}", file=sys.stderr)
    return 2


def _take_orchestrator_pat(plan_pr: str) -> GitHubPAT | None:
    """Consume the orchestrator's one-shot credential channel for the trusted runner.

    The channel is read once and closed immediately: the variables leave os.environ before
    any model subprocess can exist, and an invalid value fails closed rather than falling
    back to anonymous reads. Without the channel the legacy anonymous entry is preserved.
    """
    token = os.environ.pop("AGENT_DELIVERY_PAT", None)
    login = os.environ.pop("AGENT_DELIVERY_PAT_LOGIN", None)
    if token is None and login is None:
        return None
    if token is None or login is None:
        raise AgentDeliveryError("The orchestrator PAT channel was half-present; refusing anonymous fallback.")
    repository = parse_plan_pr_ref(plan_pr)[0].slug
    return GitHubPAT.from_value(repository=repository, expected_login=login, token=token)


def _take_spawn_gate_name(pat_present: bool) -> str | None:
    """Consume the one-shot spawn-gate name (not a credential) for the trusted runner."""
    gate = os.environ.pop("AGENT_DELIVERY_SPAWN_GATE", None)
    if gate is not None and not pat_present:
        raise AgentDeliveryError("The orchestrator spawn gate was set without a PAT channel; refusing to run.")
    return gate


def agent_run_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-run", description="Run one Work Order authorized by a merged Plan PR.")
    parser.add_argument("--plan-pr", required=True, help="Merged Plan PR URL or OWNER/REPO#NUMBER")
    parser.add_argument("--work-order-path", required=True, help="Work Order path changed by that Plan PR")
    parser.add_argument("--repo-path", type=Path, default=Path.cwd(), help="Local clone of the target repository")
    parser.add_argument("--publish", action="store_true", help="Use a verified GitHub App to push and open a Delivery PR")
    _add_worker_arguments(parser)
    args = parser.parse_args(argv)
    try:
        repo_root, worker_config = _worker_config(args)
        pat_identity = _take_orchestrator_pat(args.plan_pr)
        spawn_gate_name = _take_spawn_gate_name(pat_present=pat_identity is not None)
        _print_result(
            execute_plan(
                repo_path=repo_root,
                plan_pr=args.plan_pr,
                work_order_path=args.work_order_path,
                worker_config=worker_config,
                publish=args.publish,
                pat_identity=pat_identity,
                spawn_gate_name=spawn_gate_name,
            )
        )
        return 0
    except Exception as exc:
        return _report_error(exc)


def agent_watch_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-watch",
        description=(
            "Scan closed PRs by update time (up to 1000) and inspect up to 30 merged PRs; "
            "this may not cover the 30 most recently merged PRs. Discover at most one unprocessed Work Order."
        ),
    )
    parser.add_argument("--once", action="store_true", required=True, help="Perform one discovery pass and exit")
    parser.add_argument("--repo-path", type=Path, default=Path.cwd(), help="Local clone of the target repository")
    parser.add_argument("--publish", action="store_true", help="Require a verified GitHub App and open a Delivery PR")
    _add_worker_arguments(parser)
    args = parser.parse_args(argv)
    try:
        repo_root, worker_config = _worker_config(args)
        result = watch_once(repo_path=repo_root, worker_config=worker_config, publish=args.publish)
        if result is None:
            print("No eligible merged Work Order was found.")
        else:
            _print_result(result)
        return 0
    except Exception as exc:
        return _report_error(exc)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-delivery", description="Utilities for Agent Delivery Loop.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="Parse and validate a version 1 Work Order JSON file")
    validate_parser.add_argument("path", type=Path)
    for command in ("prepare-review", "check-review", "run-review"):
        review_parser = subparsers.add_parser(command, help="Prepare, check, or run a bounded review-only candidate handoff")
        review_parser.add_argument("--plan-pr", required=True)
        review_parser.add_argument("--work-order-path", required=True)
        review_parser.add_argument("--run-record", required=True, type=Path)
        review_parser.add_argument("--head-sha", required=True, help="Full candidate SHA at the local Delivery branch head")
        review_parser.add_argument("--repo-path", type=Path, default=Path.cwd())
        if command == "prepare-review":
            review_parser.add_argument("--output-dir", required=True, type=Path, help="New private directory outside repo and Worker state")
        elif command == "run-review":
            review_parser.add_argument("--output-dir", required=True, type=Path, help="New private review bundle directory outside repo and Worker state")
            review_parser.add_argument("--review-model", required=True, help="Explicit review CLI model")
            review_parser.add_argument("--review-effort", required=True, help="Explicit review CLI reasoning effort")
            review_parser.add_argument(
                "--review-timeout", type=int, default=900,
                help=f"Review process timeout in seconds ({MIN_REVIEW_TIMEOUT_SECONDS}-{MAX_REVIEW_TIMEOUT_SECONDS})"
            )
            review_parser.add_argument("--proxy", default=None, help="Explicit HTTP(S) proxy for the review process only")
            review_parser.add_argument("--codex-binary", default="codex")
        else:
            review_parser.add_argument("--bundle-dir", required=True, type=Path)
            review_parser.add_argument("--result", required=True, type=Path)
            review_parser.add_argument("--review-exit-code", required=True, type=int, help="Operator-attested CLI exit code; not independently proven")
    deliver_parser = subparsers.add_parser(
        "deliver",
        help="Single-shot deterministic orchestration: verify install and authorization, run the Worker, review, publish, wait for CI, and optionally merge"
    )
    resume_parser = subparsers.add_parser(
        "resume",
        help="Safely continue a delivery orchestration from its last confirmed stage; never re-runs the Worker"
    )
    resume_parser.add_argument("--orchestration-id", required=True)
    for pipeline_parser in (deliver_parser, resume_parser):
        pipeline_parser.add_argument("--plan-pr", required=True)
        pipeline_parser.add_argument("--work-order-path", required=True)
        pipeline_parser.add_argument("--repo-path", type=Path, default=Path.cwd())
        _add_worker_arguments(pipeline_parser)
        _add_github_identity_arguments(pipeline_parser)
        pipeline_parser.add_argument("--install-receipt", required=True, type=Path, help="Install provenance receipt for the running entry")
        pipeline_parser.add_argument("--expected-source-sha", required=True, help="Approved full source SHA the installation must match")
        pipeline_parser.add_argument("--expected-wheel-sha256", required=True, help="Approved wheel SHA-256 the installation must match")
        pipeline_parser.add_argument("--review-model", required=True)
        pipeline_parser.add_argument("--review-effort", required=True)
        pipeline_parser.add_argument("--review-timeout", type=int, default=900)
        pipeline_parser.add_argument("--review-bundle-dir", required=True, type=Path)
        pipeline_parser.add_argument("--proxy", default=None)
        pipeline_parser.add_argument("--ci-timeout", type=int, default=900)
        pipeline_parser.add_argument("--auto-merge", action="store_true", help="Merge the Delivery PR only after every gate passes")
    auth_parser = subparsers.add_parser(
        "check-github-auth", help="Read-only PAT account, repository, PR and CI probe; never starts a Worker or writes to GitHub"
    )
    auth_parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    auth_parser.add_argument("--pr", required=True, type=int, help="Existing PR number used only for a read/CI probe")
    auth_parser.add_argument("--proxy", default=None)
    _add_github_identity_arguments(auth_parser)
    evidence_parser = subparsers.add_parser(
        "check-evidence",
        help="Verify a Work Order's pinned review evidence resolves before any Worker runs"
    )
    evidence_parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    evidence_parser.add_argument("--work-order-path", required=True)
    evidence_parser.add_argument("--source-ref", required=True, help="Full commit SHA containing the Work Order file")
    verify_parser = subparsers.add_parser(
        "verify-review",
        help="Re-verify an already captured review receipt without re-running the review model"
    )
    verify_parser.add_argument("--plan-pr", required=True)
    verify_parser.add_argument("--work-order-path", required=True)
    verify_parser.add_argument("--run-record", required=True, type=Path)
    verify_parser.add_argument("--head-sha", required=True)
    verify_parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    verify_parser.add_argument("--bundle-dir", required=True, type=Path)
    verify_parser.add_argument("--review-record", required=True, type=Path, help="Harness review process record with the captured exit code")
    report_parser = subparsers.add_parser(
        "report",
        help="Print one short sanitized offline JSON summary of a stored orchestration record"
    )
    report_parser.add_argument("--orchestration-id", required=True, help="Orchestration UUID of the record to summarize")
    args = parser.parse_args(argv)
    if args.command == "check-github-auth":
        from .publish import probe_github_access

        try:
            if args.pr < 1:
                raise AgentDeliveryError("The read-only probe requires a positive existing PR number.")
            root = repository_root(args.repo_path.expanduser().resolve())
            identity = GitHubPAT.from_file(
                path=args.github_pat_file, repo_root=root, repository=repository_remote(root).slug,
                expected_login=args.github_login,
            )
            result = probe_github_access(identity, pr_number=args.pr, proxy=args.proxy)
        except Exception as exc:
            return _report_error(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "validate":
        try:
            order = parse_work_order(args.path.read_bytes())
        except (OSError, AgentDeliveryError) as exc:
            return _report_error(exc)
        print(f"Valid Work Order {order.identity}; canonical SHA-256 {order.sha256}")
        return 0
    if args.command in {"prepare-review", "check-review", "run-review"}:
        inputs = {
            "repo_path": args.repo_path,
            "plan_pr": args.plan_pr,
            "work_order_path": args.work_order_path,
            "run_record": args.run_record,
            "head_sha": args.head_sha,
        }
        try:
            if args.command == "prepare-review":
                result = prepare_review(output_dir=args.output_dir, **inputs)
            elif args.command == "run-review":
                prepare_review(output_dir=args.output_dir, **inputs)
                review_run = run_review_process(
                    bundle_dir=args.output_dir, timeout_seconds=args.review_timeout,
                    review_model=args.review_model, review_effort=args.review_effort,
                    codex_binary=args.codex_binary, proxy=args.proxy,
                )
                result = check_review(
                    bundle_dir=args.output_dir, result_path=Path(review_run["result_path"]),
                    review_exit_code=review_run["exit_code"],
                    exit_code_source="captured_by_orchestrator", **inputs,
                )
                result["review_id"] = review_run["review_id"]
                result["codex_version"] = review_run["codex_version"]
                result["review_stop_status"] = review_run["stop_status"]
            else:
                result = check_review(
                    bundle_dir=args.bundle_dir.expanduser(), result_path=args.result.expanduser(),
                    review_exit_code=args.review_exit_code, **inputs,
                )
        except Exception as exc:
            return _report_error(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command in {"deliver", "resume"}:
        from .orchestrator import deliver, resume

        try:
            common = dict(
                repo_path=args.repo_path, plan_pr=args.plan_pr, work_order_path=args.work_order_path,
                model=args.model, base_url=args.base_url, effort=args.effort, auth_config=args.auth_config,
                install_receipt=args.install_receipt, expected_source_sha=args.expected_source_sha,
                expected_wheel_sha256=args.expected_wheel_sha256,
                review_model=args.review_model, review_effort=args.review_effort,
                review_timeout_seconds=args.review_timeout, review_bundle_dir=args.review_bundle_dir,
                proxy=args.proxy, ci_timeout_seconds=args.ci_timeout, auto_merge=args.auto_merge,
                github_pat_file=args.github_pat_file, github_login=args.github_login,
            )
            if args.command == "deliver":
                result = deliver(**common)
            else:
                result = resume(orchestration_id=args.orchestration_id, **common)
        except Exception as exc:
            return _report_error(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "check-evidence":
        try:
            result = check_evidence(args.repo_path, args.source_ref, args.work_order_path)
        except Exception as exc:
            return _report_error(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "verify-review":
        try:
            captured = load_verified_review_record(args.review_record)
            result = check_review(
                bundle_dir=args.bundle_dir.expanduser(),
                result_path=captured["result_path"],
                review_exit_code=captured["exit_code"],
                repo_path=args.repo_path,
                plan_pr=args.plan_pr,
                work_order_path=args.work_order_path,
                run_record=args.run_record,
                head_sha=args.head_sha,
                exit_code_source="captured_by_orchestrator",
            )
            result["reused_review_id"] = captured["record"]["review_id"]
        except Exception as exc:
            return _report_error(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if args.command == "report":
        from .report import build_report

        try:
            summary = build_report(args.orchestration_id)
        except Exception as exc:
            return _report_error(exc)
        print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
        return 0
    return 2
