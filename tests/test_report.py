"""Focused offline checks for the `agent-delivery report` subcommand."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agent_delivery_loop.cli import main
from agent_delivery_loop.report import REPORT_FIELDS, ReportError, build_report

COMPLETED_ORCHESTRATION_ID = "46d814b8-b778-41dd-8477-b412b7ed2387"
BLOCKED_ORCHESTRATION_ID = "2eb4742a-448e-41ae-a342-f9473c7516ad"
MISSING_ORCHESTRATION_ID = "00000000-0000-0000-0000-000000000000"
CORRUPT_ORCHESTRATION_ID = "11111111-1111-1111-1111-111111111111"
MISMATCHED_ORCHESTRATION_ID = "22222222-2222-2222-2222-222222222222"
SYMLINK_ORCHESTRATION_ID = "33333333-3333-3333-3333-333333333333"
STRUCTURE_ORCHESTRATION_ID = "44444444-4444-4444-4444-444444444444"
OVERSIZED_ORCHESTRATION_ID = "55555555-5555-5555-5555-555555555555"
MINIMAL_ORCHESTRATION_ID = "66666666-6666-6666-6666-666666666666"

# Expected output is byte-identical to the inline sanitized samples: exactly the
# whitelisted fields, in sample order, with no extra whitespace.
COMPLETED_SUMMARY = (
    '{"orchestration_id":"46d814b8-b778-41dd-8477-b412b7ed2387","task":"WO-B1-DOCS-001",'
    '"revision":1,"stage":"completed","run_id":"c3b99d61-2b36-44e5-8eab-b893ca31cb7e",'
    '"session_id":"f836c5d6-a26c-4a26-b036-145255bfe946",'
    '"delivery_commit":"4f39ce086878fb7897d938ea8de31f131f9e518c","delivery_pr":33}'
)
BLOCKED_SUMMARY = (
    '{"orchestration_id":"2eb4742a-448e-41ae-a342-f9473c7516ad","task":"WO-PAT-TRIAL-001",'
    '"revision":2,"stage":"review_blocked","run_id":"1c8ec4c0-d573-40e8-b6a9-a7e3e85c8e28",'
    '"session_id":"80932a39-9c42-4f46-9025-1a3389da0fa3","delivery_commit":null,"delivery_pr":null}'
)
MINIMAL_SUMMARY = (
    '{"orchestration_id":"66666666-6666-6666-6666-666666666666","task":null,"revision":null,'
    '"stage":"created","run_id":null,"session_id":null,"delivery_commit":null,"delivery_pr":null}'
)

# Synthetic sensitive content that must never reach a report.
LEAK = "/private/operator/claude-settings.json"
FAILURE_TEXT = "raw subprocess failure with /private/operator detail"


def _completed_record() -> dict:
    return {
        "schema_version": 1,
        "orchestration_id": COMPLETED_ORCHESTRATION_ID,
        "mode": "single_shot_delivery",
        "stage": "completed",
        "started_at": "2026-10-05T09:00:00+00:00",
        "finished_at": "2026-10-05T09:40:00+00:00",
        "failure": None,
        "authorization": {
            "plan_pr": "https://github.com/BH2-4/agent-delivery-loop/pull/32",
            "plan_merge_sha": "0" * 40,
            "work_order_path": ".agents/work-orders/WO-B1-DOCS-001-r1.json",
            "work_order_sha256": "1" * 64,
            "task_id": "WO-B1-DOCS-001",
            "revision": 1,
        },
        "worker": {
            "run_id": "c3b99d61-2b36-44e5-8eab-b893ca31cb7e",
            "session_id": "f836c5d6-a26c-4a26-b036-145255bfe946",
            "agent_run_exit_code": 0,
            "delivery_branch": "agent/wo-b1-docs-001-r1-00000000",
            "worker_commit": "4f39ce086878fb7897d938ea8de31f131f9e518c",
            "changed_paths": ["README.md"],
            "record_path": f"/private/state/runs/abcd/c3b99d61-2b36-44e5-8eab-b893ca31cb7e.json",
        },
        "delivery_pr": {
            "number": 33,
            "url": "https://github.com/BH2-4/agent-delivery-loop/pull/33",
            "reused": False,
            "head": "4f39ce086878fb7897d938ea8de31f131f9e518c",
        },
        "parameters": {"auth_config": LEAK, "install_receipt": "/private/receipt.json"},
    }


def _blocked_record() -> dict:
    return {
        "schema_version": 1,
        "orchestration_id": BLOCKED_ORCHESTRATION_ID,
        "mode": "single_shot_delivery",
        "stage": "review_blocked",
        "finished_at": "2026-10-06T08:10:00+00:00",
        "failure": FAILURE_TEXT,
        "authorization": {
            "plan_pr": "https://github.com/BH2-4/agent-delivery-loop/pull/28",
            "plan_merge_sha": "2" * 40,
            "work_order_path": ".agents/work-orders/WO-PAT-TRIAL-001-r2.json",
            "work_order_sha256": "3" * 64,
            "task_id": "WO-PAT-TRIAL-001",
            "revision": 2,
        },
        "worker": {
            "run_id": "1c8ec4c0-d573-40e8-b6a9-a7e3e85c8e28",
            "session_id": "80932a39-9c42-4f46-9025-1a3389da0fa3",
            "agent_run_exit_code": 0,
            "delivery_branch": "agent/wo-pat-trial-001-r2-0000000",
            "worker_commit": "5" * 40,
            "changed_paths": ["docs/pat-delivery-runbook.md"],
            "record_path": f"/private/state/runs/ef12/1c8ec4c0-d573-40e8-b6a9-a7e3e85c8e28.json",
        },
        "candidate": None,
        "delivery_pr": None,
        "parameters": {"auth_config": LEAK},
    }


def _snapshot(root: Path) -> tuple[frozenset[str], dict[str, bytes]]:
    """Every entry name plus every file's bytes, so any write becomes visible."""
    entries = frozenset(str(path.relative_to(root)) for path in root.rglob("*"))
    contents = {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    return entries, contents


class ReportTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)

    def _write(self, record: dict, identifier: str | None = None) -> Path:
        directory = self.state / "orchestrations"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{identifier or record['orchestration_id']}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return path

    def _run_cli(self, identifier: str) -> tuple[int, str, str]:
        stdout, stderr = StringIO(), StringIO()
        with patch.dict(os.environ, {"AGENT_STATE_DIR": str(self.state)}):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["report", "--orchestration-id", identifier])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_completed_and_blocked_records_exit_zero_with_sample_output(self) -> None:
        self._write(_completed_record())
        self._write(_blocked_record())
        for identifier, expected in (
            (COMPLETED_ORCHESTRATION_ID, COMPLETED_SUMMARY),
            (BLOCKED_ORCHESTRATION_ID, BLOCKED_SUMMARY),
        ):
            code, stdout, stderr = self._run_cli(identifier)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(stdout.strip(), expected)
            self.assertEqual(stderr, "")

    def test_summary_is_exactly_the_inline_whitelist(self) -> None:
        self._write(_completed_record())
        self._write(_blocked_record())
        completed = build_report(COMPLETED_ORCHESTRATION_ID, state_dir=self.state)
        blocked = build_report(BLOCKED_ORCHESTRATION_ID, state_dir=self.state)
        self.assertEqual(tuple(completed), REPORT_FIELDS)
        self.assertEqual(tuple(blocked), REPORT_FIELDS)
        self.assertEqual(completed, json.loads(COMPLETED_SUMMARY))
        self.assertEqual(blocked, json.loads(BLOCKED_SUMMARY))
        for serialized in (json.dumps(completed), json.dumps(blocked)):
            self.assertNotIn(LEAK, serialized)
            self.assertNotIn(FAILURE_TEXT, serialized)
            self.assertNotIn("record_path", serialized)
            self.assertNotIn("parameters", serialized)

    def test_missing_optional_sections_map_to_null(self) -> None:
        self._write({"schema_version": 1, "orchestration_id": MINIMAL_ORCHESTRATION_ID, "stage": "created"})
        code, stdout, _ = self._run_cli(MINIMAL_ORCHESTRATION_ID)
        self.assertEqual(code, 0)
        self.assertEqual(stdout.strip(), MINIMAL_SUMMARY)

    def test_report_reads_without_writing_state(self) -> None:
        self._write(_completed_record())
        before = _snapshot(self.state)
        build_report(COMPLETED_ORCHESTRATION_ID, state_dir=self.state)
        self.assertEqual(_snapshot(self.state), before)
        # No side-effectful initialization (runs/locks directories) may appear.
        self.assertEqual({path.name for path in self.state.iterdir()}, {"orchestrations"})

    def test_invalid_missing_and_corrupt_inputs_fail_closed(self) -> None:
        self._write(_completed_record())
        self._write(_completed_record(), identifier=MISMATCHED_ORCHESTRATION_ID)  # ID inside mismatches
        directory = self.state / "orchestrations"
        (directory / f"{CORRUPT_ORCHESTRATION_ID}.json").write_text("{not json", encoding="utf-8")
        target = directory / "target.json"
        target.write_text(json.dumps(_completed_record()), encoding="utf-8")
        (directory / f"{SYMLINK_ORCHESTRATION_ID}.json").symlink_to(target)
        for identifier in (
            "not-a-uuid",
            "46D814B8-B778-41DD-8477-B412B7ED2387",  # non-canonical case
            "46d814b8b77841dd8477b412b7ed2387",  # non-canonical form
            "../../etc/passwd",
            MISSING_ORCHESTRATION_ID,
            CORRUPT_ORCHESTRATION_ID,
            MISMATCHED_ORCHESTRATION_ID,
            SYMLINK_ORCHESTRATION_ID,
        ):
            with self.assertRaises(ReportError):
                build_report(identifier, state_dir=self.state)
        code, _, stderr = self._run_cli("not-a-uuid")
        self.assertEqual(code, 2)
        self.assertTrue(stderr.startswith("agent-delivery: "))
        self.assertNotIn(LEAK, stderr)
        self.assertNotIn(str(self.state), stderr)

    def test_structurally_invalid_whitelist_fields_fail_closed(self) -> None:
        for mutation in (
            {"stage": None},
            {"authorization": "not-a-mapping"},
            {"authorization": {"task_id": "WO-X-001", "revision": "1"}},
            {"authorization": {"task_id": "WO-X-001", "revision": True}},
            {"worker": {"run_id": "not-a-uuid", "session_id": None}},
            {"worker": {"run_id": None, "session_id": "also not uuid"}},
            {"delivery_pr": {"number": 33, "head": "shortsha"}},
            {"delivery_pr": {"number": "33", "head": "4" * 40}},
        ):
            record = _completed_record()
            record["orchestration_id"] = STRUCTURE_ORCHESTRATION_ID
            record.update(mutation)
            self._write(record)
            with self.assertRaises(ReportError, msg=json.dumps(mutation)):
                build_report(STRUCTURE_ORCHESTRATION_ID, state_dir=self.state)
            (self.state / "orchestrations" / f"{STRUCTURE_ORCHESTRATION_ID}.json").unlink()

    def test_oversized_record_is_rejected(self) -> None:
        record = _completed_record()
        record["orchestration_id"] = OVERSIZED_ORCHESTRATION_ID
        record["parameters"]["blob"] = "x" * (300 * 1024)
        self._write(record)
        with self.assertRaises(ReportError):
            build_report(OVERSIZED_ORCHESTRATION_ID, state_dir=self.state)


if __name__ == "__main__":
    unittest.main()
