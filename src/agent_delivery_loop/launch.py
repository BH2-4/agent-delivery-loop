"""Assemble the existing deliver command from one private local configuration."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from .errors import AgentDeliveryError
from .git_ops import repository_root


REQUIRED_FIELDS = {
    "repo_path", "model", "base_url", "effort", "auth_config",
    "github_pat_file", "github_login", "install_receipt",
    "expected_source_sha", "expected_wheel_sha256", "review_model",
    "review_effort", "review_bundle_root",
}
OPTIONAL_FIELDS = {"review_timeout", "ci_timeout", "proxy"}


def launch_arguments(config_path: Path, *, plan_pr: str, work_order_path: str) -> list[str]:
    path = config_path.expanduser().resolve()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise AgentDeliveryError("Launch configuration must be a readable UTF-8 JSON file.") from None
    if not isinstance(config, dict) or set(config) - REQUIRED_FIELDS - OPTIONAL_FIELDS:
        raise AgentDeliveryError("Launch configuration contains unsupported fields.")
    if REQUIRED_FIELDS - set(config):
        raise AgentDeliveryError("Launch configuration is missing required fields.")
    for name, value in config.items():
        if name in {"review_timeout", "ci_timeout"}:
            valid = isinstance(value, int) and not isinstance(value, bool)
        else:
            valid = isinstance(value, str) and bool(value.strip())
        if not valid:
            raise AgentDeliveryError("Launch configuration contains an invalid value.")
    root = repository_root(Path(config["repo_path"]).expanduser().resolve())
    if path.is_relative_to(root):
        raise AgentDeliveryError("Launch configuration must be outside the repository.")
    bundle = Path(config.pop("review_bundle_root")).expanduser() / str(uuid.uuid4())
    arguments = ["deliver", "--plan-pr", plan_pr, "--work-order-path", work_order_path]
    for name, value in config.items():
        arguments.extend(["--" + name.replace("_", "-"), str(value)])
    arguments.extend(["--review-bundle-dir", str(bundle)])
    return arguments
