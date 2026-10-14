import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "arc_test", Path(__file__).resolve().parents[1] / "scripts/arc_test.py"
)
arc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(arc)


def environment():
    ident = "abc123def456"
    return {
        "id": ident,
        "repo": arc.REPO,
        "cluster": "arc-test-" + ident,
        "context": "kind-arc-test-" + ident,
        "scale_set": "arc-test-" + ident,
        "cluster_uid": "owned-uid",
        "runs": [],
    }


class HarnessTests(unittest.TestCase):
    def test_no_false_success_from_empty_partial_or_other_runners(self):
        good = {
            "status": "completed",
            "conclusion": "success",
            "runner_name": "arc-test-abc123def456-runner-x",
        }
        self.assertFalse(arc.success_ok([], 3, "arc-test-abc123def456"))
        self.assertFalse(arc.success_ok([good], 3, "arc-test-abc123def456"))
        self.assertFalse(
            arc.success_ok([{**good, "runner_name": "other-runner"}], 1, "arc-test-abc123def456")
        )
        self.assertTrue(arc.success_ok([good] * 3, 3, "arc-test-abc123def456"))

    def test_queued_jobs_must_not_have_an_assigned_runner(self):
        queued = {"status": "queued", "runner_id": 0, "runner_name": ""}
        self.assertFalse(arc.queue_ok([], 3))
        self.assertTrue(arc.queue_ok([queued] * 3, 3))
        self.assertFalse(arc.queue_ok([{**queued, "runner_id": 42}], 1))
        self.assertFalse(arc.queue_ok([{**queued, "status": "in_progress"}], 1))

    def test_cluster_and_repository_ownership(self):
        arc.validate_state(environment())
        for key, value in [("repo", "other/repo"), ("context", "kind-production"), ("id", "../x")]:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                arc.validate_state({**environment(), key: value})
        with patch.object(arc, "kube", return_value='{"metadata":{"uid":"another-uid"}}'):
            with self.assertRaisesRegex(RuntimeError, "UID mismatch"):
                arc.guard(environment())

    def test_original_job_ids_cannot_be_replaced(self):
        entry = {"run_id": 10, "count": 1, "job_ids": [11]}
        with patch.object(arc, "api", return_value={"total_count": 1, "jobs": [{"id": 12}]}):
            with self.assertRaisesRegex(RuntimeError, "IDs changed"):
                arc.jobs(environment(), entry)

    def test_incomplete_api_snapshot_fails_closed(self):
        with patch.object(arc, "api", return_value={"total_count": 3, "jobs": [{"id": 12}]}):
            with self.assertRaisesRegex(RuntimeError, "Incomplete"):
                arc.jobs(environment(), {"run_id": 10, "count": 3})

    def test_cancel_refuses_unowned_runs(self):
        s = environment()
        s["runs"] = [{"batch": "owned", "run_id": 10}]
        with patch.object(
            arc,
            "api",
            return_value={"display_title": "another-run", "path": ".github/workflows/probe.yml"},
        ) as api:
            with self.assertRaisesRegex(RuntimeError, "ownership mismatch"):
                arc.cancel(s)
            self.assertEqual(api.call_count, 1)

    def test_redacts_tokens_bearer_credentials_and_queue_urls(self):
        text = "ghp_abcdefgh github_pat_ABC_123 Bearer secretvalue https://broker.example/queue/session?sig=secret"
        cleaned = arc.redact(text)
        for secret in (
            "ghp_abcdefgh",
            "github_pat_ABC_123",
            "secretvalue",
            "/queue/session",
            "sig=secret",
        ):
            self.assertNotIn(secret, cleaned)
        self.assertIn("broker.example", cleaned)
        self.assertEqual(
            arc.scrub({"env": [{"name": "GITHUB_TOKEN", "value": "opaque"}]})["env"][0]["value"],
            "[REDACTED]",
        )

    def test_down_stops_before_deleting_cluster_if_arc_uninstall_fails(self):
        with (
            patch.object(arc, "guard"),
            patch.object(arc, "cancel"),
            patch.object(arc, "helm", side_effect=RuntimeError("cannot clean GitHub resources")),
            patch.object(arc, "run") as command,
        ):
            with self.assertRaises(RuntimeError):
                arc.down(environment())
            command.assert_not_called()

    def test_wakeup_does_not_turn_original_failure_into_success(self):
        s = {
            **environment(),
            "versions": {"ARC_VERSION": "0.15.0", "RUNNER_IMAGE": "official"},
            "capacity": 3,
        }
        held = {
            "run_id": 20,
            "url": "https://github.com/kuring/arc-test/actions/runs/20",
            "count": 3,
        }
        s["runs"] = [held]
        # Warmup succeeds, original recovery times out, diagnostic recovery succeeds.
        completions = [[], TimeoutError("original queue stuck"), []]
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(arc, "STATE", Path(folder)),
            patch.object(arc, "workflow_ready"),
            patch.object(arc, "guard"),
            patch.object(arc, "capacity", return_value={}),
            patch.object(arc, "listener_state", return_value={}),
            patch.object(arc, "submit", side_effect=[{"count": 1}, held, {"run_id": 21}]),
            patch.object(arc, "complete", side_effect=completions),
            patch.object(arc, "hold"),
            patch.object(arc, "get", return_value=[]),
            patch.object(arc, "collect"),
            patch.object(arc, "cancel") as cancel,
            patch.object(arc, "jobs", return_value=[{"status": "queued"}] * 3),
            patch.object(arc, "observe", side_effect=lambda *_: arc.contextlib.nullcontext()),
        ):
            with self.assertRaisesRegex(RuntimeError, "Test failed"):
                arc.test(s, "startup", 1, 1)
            import json

            result = json.loads(next(Path(folder).glob("results/*/results.json")).read_text())
            self.assertEqual(result["cases"][0]["status"], "failed")
            self.assertTrue(result["cases"][0]["wakeup"]["old_jobs_completed"])
            self.assertFalse(result["cleanup_errors"])
            self.assertGreaterEqual(cancel.call_count, 1)

    def test_interrupt_records_failure_and_attempts_recovery(self):
        s = {**environment(), "versions": {"ARC_VERSION": "0.15.0", "RUNNER_IMAGE": "official"}}
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(arc, "STATE", Path(folder)),
            patch.object(arc, "workflow_ready"),
            patch.object(arc, "guard"),
            patch.object(arc, "capacity", return_value={}) as capacity,
            patch.object(arc, "submit", return_value={}),
            patch.object(arc, "complete", side_effect=KeyboardInterrupt),
            patch.object(arc, "collect"),
            patch.object(arc, "cancel") as cancel,
            patch.object(arc, "observe", side_effect=lambda *_: arc.contextlib.nullcontext()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                arc.test(s, "smoke", 1, 1)
            import json

            result = json.loads(next(Path(folder).glob("results/*/results.json")).read_text())
            self.assertEqual(result["cases"][0]["status"], "failed")
            self.assertEqual(result["control_error"], "Interrupted")
            self.assertGreaterEqual(cancel.call_count, 1)
            self.assertEqual(capacity.call_args.args[1], 3)


if __name__ == "__main__":
    unittest.main()
