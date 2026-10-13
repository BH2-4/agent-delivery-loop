"""Product launch paths and authoritative GitHub ref semantics; no model calls."""
from __future__ import annotations

import base64
import json
import http.client
import io
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_delivery_loop import runner, rework
from agent_delivery_loop.claude_worker import ClaudeConfig, WorkerOutcome
from agent_delivery_loop.errors import AgentDeliveryError
from agent_delivery_loop.execution import ExecutionStore, execution_key
from agent_delivery_loop.github import GitHubAPIError, GitHubClient, GitHubWritePendingError, Repo
from agent_delivery_loop.github_identity import GitHubPAT
from agent_delivery_loop.store import RunStore
from agent_delivery_loop.work_order import parse_work_order


class MemoryGitHub:
    """One shared atomic ref authority, including distinct HTTP failures."""
    repo = Repo("owner", "repo")

    def __init__(self):
        self.refs = {}
        self.commits = {}
        self.trees = {}
        self.lock = threading.Lock()
        self.calls = []
        self.lose_start_response = False
        self.reject_create = None

    def request(self, method, path, payload=None, *, expected_status=None):
        with self.lock:
            self.calls.append((method, path, payload))
            route = path.split("/git/")[1]
            if method == "POST" and route == "trees":
                sha = f"{len(self.trees) + 1:040x}"
                self.trees[sha] = payload
                return {"sha": sha}
            if method == "POST" and route == "commits":
                sha = f"{len(self.commits) + 100:040x}"
                self.commits[sha] = payload
                return {"sha": sha}
            if method == "POST" and route == "refs":
                ref = payload["ref"]
                if self.reject_create:
                    raise GitHubAPIError(self.reject_create)
                if ref in self.refs:
                    raise GitHubAPIError(422)
                self.refs[ref] = payload["sha"]
                if "delivery-starts" in ref and self.lose_start_response:
                    raise GitHubWritePendingError("lost response after durable write")
                return {"ref": ref, "object": {"type": "commit", "sha": payload["sha"]}}
            if method == "GET" and route.startswith("ref/"):
                ref = "refs/" + route.removeprefix("ref/")
                if ref not in self.refs:
                    raise GitHubAPIError(404)
                return {"ref": ref, "object": {"type": "commit", "sha": self.refs[ref]}}
            if method == "GET" and route.startswith("commits/"):
                commit = self.commits[route.split("/")[1]]
                return {"tree": {"sha": commit["tree"]}, "parents": [{"sha": s} for s in commit["parents"]]}
            if method == "GET" and route.startswith("trees/"):
                tree = self.trees[route.split("/")[1]]
                return {"tree": [{"path": "execution.json", "mode": "100644", "type": "blob", "sha": tree["tree"][0]["content"]}]}
            if method == "GET" and route.startswith("blobs/"):
                content = route.removeprefix("blobs/")
                return {"encoding": "base64", "content": base64.b64encode(content.encode()).decode()}
            if method == "PATCH" and route.startswith("refs/"):
                ref = "refs/" + route.removeprefix("refs/")
                new = payload["sha"]
                if self.commits[new]["parents"] != [self.refs[ref]] or payload["force"]:
                    raise GitHubAPIError(422)
                self.refs[ref] = new
                return {"ref": ref, "object": {"type": "commit", "sha": new}}
            raise AssertionError((method, route))


class ExecutionProtocolTests(unittest.TestCase):
    def setUp(self):
        self.api = MemoryGitHub()
        self.key = execution_key("owner/repo", "WO-TEST-001", "r1")
        self.store = ExecutionStore(self.api)

    def acquire(self, **overrides):
        return self.store.acquire(self.key, claimant_id="claimant-A", request_id="request-A", binding={"order": "fixed"}, **overrides)

    def test_t1_t3_sequential_completed_and_restart_replay(self):
        grant = self.acquire()
        self.assertEqual(grant["result"], "new")
        self.assertTrue(grant["new_start"])
        self.assertFalse(self.acquire()["new_start"])
        self.store.complete(grant, session_id="receipt-1")
        replay = ExecutionStore(self.api).acquire(self.key, claimant_id="claimant-B", request_id="old-request", binding={"order": "fixed"})
        self.assertFalse(replay["new_start"])
        self.assertEqual(replay["status"], "completed")
        self.assertEqual(self.store.inspect(self.key)["session_id"], "receipt-1")

    def test_t2_t4_concurrent_reentry_and_different_claimants(self):
        for same_claimant in (True, False):
            api = MemoryGitHub()
            barrier = threading.Barrier(2)
            class RacingStore(ExecutionStore):
                def _create(self, kind, key, record):
                    if kind == ("starts" if same_claimant else "claims"):
                        barrier.wait(timeout=5)
                    return super()._create(kind, key, record)
            def call(n):
                return RacingStore(api).acquire(self.key, claimant_id="A" if same_claimant else str(n), request_id="same", binding={})
            with ThreadPoolExecutor(2) as pool:
                outcomes = list(pool.map(call, range(2)))
            self.assertEqual(sum(x["new_start"] for x in outcomes), 1)
            self.assertEqual(len([ref for ref in api.refs if "delivery-starts" in ref]), 1)
            competed = "starts" if same_claimant else "claims"
            self.assertEqual(len([x for x in api.calls if x[0] == "POST" and x[1].endswith("/refs") and f"delivery-{competed}/" in x[2]["ref"]]), 2)

    def test_t5_new_revision_is_independent(self):
        self.assertTrue(self.acquire()["new_start"])
        other = execution_key("OWNER/REPO", "WO-TEST-001", "r2")
        self.assertNotEqual(self.key, other)
        self.assertTrue(self.store.acquire(other, claimant_id="B", request_id="next", binding={})["new_start"])
        self.assertFalse(self.acquire()["new_start"])

    def test_t6_durable_start_with_unknown_response_never_grants_on_readback(self):
        self.api.lose_start_response = True
        outcome = self.acquire()
        self.assertEqual(outcome["result"], "unknown")
        self.assertFalse(outcome["new_start"])
        self.assertEqual(self.store.inspect(self.key)["status"], "pending_confirmation")
        self.api.lose_start_response = False
        self.assertFalse(self.acquire()["new_start"])

    def test_errors_are_not_claim_conflicts(self):
        for code in (401, 403, 404, 409, 422, 429):
            with self.subTest(code=code):
                self.api.reject_create = code
                with self.assertRaises(GitHubAPIError) as caught:
                    self.acquire()
                self.assertEqual(caught.exception.status_code, code)
                self.assertFalse(self.api.refs)

    def test_existing_binding_drift_is_rejected(self):
        self.acquire()
        with self.assertRaises(AgentDeliveryError):
            self.store.acquire(self.key, claimant_id="claimant-A", request_id="x", binding={"order": "changed"})

    def test_unknown_claim_response_can_recover_only_first_start(self):
        actual = self.api.request
        def lose_claim(method, path, payload=None, **kwargs):
            result = actual(method,path,payload,**kwargs)
            if method == "POST" and path.endswith("/refs") and "delivery-claims/" in payload["ref"]:
                raise GitHubWritePendingError("claim response lost")
            return result
        self.api.request = lose_claim
        self.assertEqual(self.acquire()["result"], "unknown")
        self.assertIsNone(self.store.inspect(self.key)["start_sha"])
        self.api.request = actual
        self.assertTrue(self.acquire()["new_start"])
        self.assertFalse(self.acquire()["new_start"])

    def test_completion_response_loss_does_not_regrant(self):
        grant = self.acquire()
        actual = self.api.request
        def lose_completion(method,path,payload=None,**kwargs):
            result = actual(method,path,payload,**kwargs)
            if method == "PATCH":
                raise GitHubWritePendingError("completion response lost")
            return result
        self.api.request = lose_completion
        with self.assertRaises(GitHubWritePendingError):
            self.store.complete(grant,session_id="receipt")
        self.assertEqual(self.store.inspect(self.key)["status"], "completed")
        self.assertFalse(self.acquire()["new_start"])

    def test_http_write_truncation_bad_json_and_non_created_status_are_unknown(self):
        client = GitHubClient(self.api.repo,token="synthetic")
        for body, status in ((b"invalid",201),(b'{}',200),(b'\xff',201)):
            response = io.BytesIO(body)
            response.status = status
            with patch("agent_delivery_loop.github.urllib.request.urlopen",return_value=response):
                with self.assertRaises(GitHubWritePendingError):
                    client.request("POST","/synthetic",{},expected_status=201)
        with patch("agent_delivery_loop.github.urllib.request.urlopen",side_effect=http.client.IncompleteRead(b"partial")) as request:
            with self.assertRaises(GitHubWritePendingError):
                client.request("POST","/synthetic",{},expected_status=201)
            self.assertEqual(request.call_count,1)

    def test_malformed_create_object_is_unknown(self):
        with patch.object(self.api,"request",return_value={}):
            self.assertEqual(self.store._create("starts",self.key,{})["result"],"unknown")


class ProductLaunchTests(unittest.TestCase):
    def test_t1_t3_t5_t6_t7_t8_model_boundary(self):
        for window, expected in (("normal", 1), ("new_revision", 2), ("unknown", 0), ("before_spawn", 0), ("no_receipt", 1), ("legacy_local", 0)):
            with self.subTest(window=window), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                api = MemoryGitHub()
                api.lose_start_response = window == "unknown"
                order_path = ".agents/work-orders/WO-2026-001-r1.json"
                raw = (Path(__file__).parents[1] / "examples/work-orders/WO-2026-001.json").read_bytes()
                skill = b"test skill\n"
                client = Mock()
                client.authorized_plan.return_value = SimpleNamespace(merge_sha="a"*40, order_bytes=raw, order_path=order_path)
                client.content.return_value = skill
                config = ClaudeConfig(executable="unused", model="m", base_url="https://example.invalid", provider_host="example.invalid", auth_name="ANTHROPIC_AUTH_TOKEN", auth_value="fake", effort="max", target_repo_root=root)
                identity = GitHubPAT(repository="owner/repo", expected_login="owner", _token="github_pat_test_only")
                starts = root / "starts.txt"
                preflights = []
                def preflight(*_args):
                    preflights.append(True)
                    return (2, 1, 300)
                def create(_root, dest, _branch, _sha):
                    p = dest / ".agents/policies/delivery-skill/SKILL.md"
                    p.parent.mkdir(parents=True)
                    p.write_bytes(skill)
                def model(*args, **kwargs):
                    if window == "before_spawn":
                        raise SystemExit(73)
                    # Real lightweight subprocess: a process event, not a real model.
                    subprocess.run([sys.executable, "-c", "import pathlib,sys; p=pathlib.Path(sys.argv[1]); p.open('a').write('start\\n')", str(starts)], check=True)
                    if window == "no_receipt":
                        raise SystemExit(74)
                    kwargs["lifecycle"].status = "stopped"
                    return WorkerOutcome("receipt", "complete", [])
                for attempt in range(4 if window == "new_revision" else 3):
                    if window == "new_revision" and attempt == 3:
                        revised = json.loads(raw)
                        revised["revision"] = 2
                        order_path = ".agents/work-orders/WO-2026-001-r2.json"
                        client.authorized_plan.return_value = SimpleNamespace(merge_sha="a"*40,order_bytes=json.dumps(revised).encode(),order_path=order_path)
                    if window == "legacy_local":
                        local = RunStore(root/f"host-{attempt}")
                        from agent_delivery_loop.store import task_key
                        local.write(task_key("owner/repo","WO-2026-001",1),"legacy",{"schema_version":1,"run_id":"legacy","status":"local_ready","worker_status":"stopped"})
                    # A fresh local directory models a restart/another host; authority survives.
                    with patch.multiple(runner, repository_root=Mock(return_value=root), repository_remote=Mock(return_value=api.repo), GitHubClient=Mock(return_value=client), ExecutionStore=Mock(side_effect=lambda _client: ExecutionStore(api)), default_state_dir=Mock(return_value=root / f"host-{attempt}"), preflight=Mock(side_effect=preflight), ensure_plan_is_on_main=Mock(), create_worktree=Mock(side_effect=create), run_claude=Mock(side_effect=model), _safe_changed_paths=Mock(), ensure_diff_clean=Mock(), stage_changes=Mock(return_value=SimpleNamespace(paths=[])), _validate_paths=Mock(return_value=[]), commit_changes=Mock(return_value=SimpleNamespace(commit_sha="b"*40, paths=[]))):
                        try:
                            result = runner.execute_plan(repo_path=root, plan_pr="owner/repo#1", work_order_path=order_path, worker_config=config, pat_identity=identity)
                        except SystemExit:
                            self.assertEqual(attempt, 0)
                self.assertEqual(len(starts.read_text().splitlines()) if starts.exists() else 0, expected)
                self.assertEqual(len(preflights), 0 if window in {"unknown","legacy_local"} else 2 if window == "new_revision" else 1)
                key = execution_key("owner/repo", "WO-2026-001", "r1")
                self.assertEqual(ExecutionStore(api).inspect(key)["status"], "completed" if window in {"normal","new_revision"} else "absent" if window == "legacy_local" else "pending_confirmation")

    def test_watcher_skips_existing_shared_start_without_launching(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = MemoryGitHub()
            raw = (Path(__file__).parents[1]/"examples/work-orders/WO-2026-001.json").read_bytes()
            client = Mock(repo=api.repo)
            client.request.side_effect = api.request
            client.merged_plan_candidates.return_value = [(1,".agents/work-orders/WO-2026-001-r1.json","owner/repo#1")]
            client.authorized_plan.return_value = SimpleNamespace(order_bytes=raw,order_path=".agents/work-orders/WO-2026-001-r1.json")
            ExecutionStore(api).acquire(execution_key("owner/repo","WO-2026-001","r1"),claimant_id="A",request_id="previous",binding={})
            config = Mock(target_repo_root=root)
            identity = GitHubPAT(repository="owner/repo",expected_login="owner",_token="github_pat_test_only")
            with patch.multiple(runner,repository_root=Mock(return_value=root),repository_remote=Mock(return_value=api.repo),GitHubClient=Mock(return_value=client),default_state_dir=Mock(return_value=root/"state")), patch.object(runner,"execute_plan") as execute:
                self.assertIsNone(runner.watch_once(repo_path=root,worker_config=config,pat_identity=identity))
                execute.assert_not_called()

    def test_rework_round_has_its_own_retained_shared_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = MemoryGitHub()
            store = RunStore(root/"state")
            raw = (Path(__file__).parents[1]/"examples/work-orders/WO-2026-001.json").read_bytes()
            order = parse_work_order(raw)
            skill = root/order.skill_ref
            skill.parent.mkdir(parents=True)
            skill.write_text("skill")
            identity = GitHubPAT(repository="owner/repo",expected_login="owner",_token="github_pat_test_only")
            def model(*_args,**kwargs):
                kwargs["lifecycle"].status = "stopped"
                return WorkerOutcome("receipt","complete",[])
            with patch.multiple(rework,GitHubClient=Mock(return_value=api),run_claude=Mock(side_effect=model),_safe_changed_paths=Mock(),ensure_diff_clean=Mock(),stage_changes=Mock(return_value=SimpleNamespace(paths=[])),_validate_paths=Mock(return_value=[]),commit_changes=Mock(return_value=SimpleNamespace(commit_sha="b"*40,paths=[]))):
                args = dict(store=store,repo_slug="owner/repo",worktree=root,order=order,worker_config=Mock(),original_run_id="original",delivery_branch="branch",parent_sha="a"*40,rework_index=1,directive="fixed",rework_count_so_far=0,pat_identity=identity)
                rework.run_bounded_rework(**args)
                with self.assertRaisesRegex(AgentDeliveryError,"launch withheld"):
                    rework.run_bounded_rework(**args)
                self.assertEqual(rework.run_claude.call_count,1)
            self.assertEqual(ExecutionStore(api).inspect(execution_key("owner/repo",order.task_id,"r1/rework-1"))["status"],"completed")


if __name__ == "__main__":
    unittest.main()
