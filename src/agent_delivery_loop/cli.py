"""Command-line interface for the first local delivery-loop version."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .errors import AgentDeliveryError
from .runner import execute_plan, watch_once
from .work_order import parse_work_order


def _print_result(result: dict[str, object]) -> None:
    fields = (
        "status",
        "run_id",
        "task_id",
        "revision",
        "session_id",
        "plan_merge_sha",
        "work_order_sha256",
        "skill_sha256",
        "claude_code_version",
        "requested_model",
        "delivery_branch",
        "delivery_commit",
        "delivery_pr",
        "changed_paths",
        "worktree_id",
    )
    print(json.dumps({key: result.get(key) for key in fields}, ensure_ascii=False, indent=2, sort_keys=True))


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
    args = parser.parse_args(argv)
    try:
        _print_result(
            execute_plan(
                repo_path=args.repo_path,
                plan_pr=args.plan_pr,
                work_order_path=args.work_order_path,
                publish=args.publish,
            )
        )
        return 0
    except Exception as exc:
        return _report_error(exc)


def agent_watch_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-watch",
        description="Scan up to 30 recent merged PRs and discover at most one unprocessed Work Order.",
    )
    parser.add_argument("--once", action="store_true", required=True, help="Perform one discovery pass and exit")
    parser.add_argument("--repo-path", type=Path, default=Path.cwd(), help="Local clone of the target repository")
    parser.add_argument("--publish", action="store_true", help="Require a verified GitHub App and open a Delivery PR")
    args = parser.parse_args(argv)
    try:
        result = watch_once(repo_path=args.repo_path, publish=args.publish)
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
    args = parser.parse_args(argv)
    if args.command == "validate":
        try:
            order = parse_work_order(args.path.read_bytes())
        except (OSError, AgentDeliveryError) as exc:
            return _report_error(exc)
        print(f"Valid Work Order {order.identity}; canonical SHA-256 {order.sha256}")
        return 0
    return 2
