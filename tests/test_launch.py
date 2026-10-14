import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from agent_delivery_loop.cli import main


class LaunchTests(unittest.TestCase):
    def test_launch_reuses_delivery_gates_and_creates_a_fresh_review_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "repo"
            repo.mkdir()
            config = {
                "repo_path": str(repo), "model": "configured-model",
                "base_url": "https://example.invalid/anthropic", "effort": "high",
                "auth_config": str(base / "claude.json"),
                "github_pat_file": str(base / "pat"), "github_login": "owner",
                "install_receipt": str(base / "receipt.json"),
                "expected_source_sha": "a" * 40, "expected_wheel_sha256": "b" * 64,
                "review_model": "review-model", "review_effort": "high",
                "review_bundle_root": str(base / "reviews"),
            }
            path = base / "launch.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            command = ["launch", "--config", str(path), "--plan-pr", "https://github.com/o/r/pull/7",
                       "--work-order-path", ".agents/work-orders/WO-EXAMPLE-r1.json"]
            with patch("agent_delivery_loop.launch.repository_root", return_value=repo), patch(
                "agent_delivery_loop.orchestrator.deliver", return_value={"stage": "created"}
            ) as deliver, redirect_stdout(io.StringIO()):
                self.assertEqual(main(command), 0)
                self.assertEqual(main(command), 0)
                first, second = [call.kwargs for call in deliver.call_args_list]
                self.assertEqual(first["plan_pr"], command[4])
                self.assertEqual(first["work_order_path"], command[6])
                self.assertEqual(first["expected_source_sha"], "a" * 40)
                self.assertEqual(first["github_pat_file"], base / "pat")
                self.assertFalse(first["auto_merge"])
                self.assertNotEqual(first["review_bundle_dir"], second["review_bundle_dir"])
                self.assertEqual(first["review_bundle_dir"].parent, base / "reviews")
                for field in ("auto_merge", "plan_pr", "github_pat"):
                    path.write_text(json.dumps({**config, field: "PRIVATE_SENTINEL"}), encoding="utf-8")
                    with redirect_stderr(io.StringIO()) as stderr:
                        self.assertEqual(main(command), 2)
                    self.assertNotIn("PRIVATE_SENTINEL", stderr.getvalue())
                self.assertEqual(deliver.call_count, 2)
