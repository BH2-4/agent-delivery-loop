"""Command-line interface for the first local delivery-loop version."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .claude_worker import ClaudeConfig, SUPPORTED_EFFORTS
from .errors import AgentDeliveryError
from .git_ops import repository_root
from .runner import execute_plan, watch_once
from .review_cli import MAX_REVIEW_TIMEOUT_SECONDS, MIN_REVIEW_TIMEOUT_SECONDS, run_review_process
from .review_handoff import check_review, prepare_review
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


def _report_error(exc: Exception) -> int:
    message = str(exc) if isinstance(exc, AgentDeliveryError) else "Unexpected failure; sensitive subprocess output was suppressed."
    print(f"agent-delivery: {message}", file=sys.stderr)
    return 2


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
        _print_result(
            execute_plan(
                repo_path=repo_root,
                plan_pr=args.plan_pr,
                work_order_path=args.work_order_path,
                worker_config=worker_config,
                publish=args.publish,
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
    deliver_parser.add_argument("--plan-pr", required=True)
    deliver_parser.add_argument("--work-order-path", required=True)
    deliver_parser.add_argument("--repo-path", type=Path, default=Path.cwd())
    _add_worker_arguments(deliver_parser)
    deliver_parser.add_argument("--install-receipt", required=True, type=Path, help="Install provenance receipt for the running entry")
    deliver_parser.add_argument("--expected-source-sha", required=True, help="Approved full source SHA the installation must match")
    deliver_parser.add_argument("--expected-wheel-sha256", required=True, help="Approved wheel SHA-256 the installation must match")
    deliver_parser.add_argument("--review-model", required=True)
    deliver_parser.add_argument("--review-effort", required=True)
    deliver_parser.add_argument("--review-timeout", type=int, default=900)
    deliver_parser.add_argument("--review-bundle-dir", required=True, type=Path)
    deliver_parser.add_argument("--proxy", default=None)
    deliver_parser.add_argument("--ci-timeout", type=int, default=900)
    deliver_parser.add_argument("--auto-merge", action="store_true", help="Merge the Delivery PR only after every gate passes")
    args = parser.parse_args(argv)
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
    if args.command == "deliver":
        from .orchestrator import deliver

        try:
            result = deliver(
                repo_path=args.repo_path, plan_pr=args.plan_pr, work_order_path=args.work_order_path,
                model=args.model, base_url=args.base_url, effort=args.effort, auth_config=args.auth_config,
                install_receipt=args.install_receipt, expected_source_sha=args.expected_source_sha,
                expected_wheel_sha256=args.expected_wheel_sha256,
                review_model=args.review_model, review_effort=args.review_effort,
                review_timeout_seconds=args.review_timeout, review_bundle_dir=args.review_bundle_dir,
                proxy=args.proxy, ci_timeout_seconds=args.ci_timeout, auto_merge=args.auto_merge,
            )
        except Exception as exc:
            return _report_error(exc)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return 2
