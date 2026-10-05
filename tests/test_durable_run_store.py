import concurrent.futures
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from niji.cloud_runtime import (
    IdempotencyConflict,
    RunError,
    RunLimitExceeded,
    RunRequest,
    RunStatus,
)
from niji.durable_run_store import SQLiteRunStore


class SQLiteRunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "private" / "runs.sqlite3"
        self.store = SQLiteRunStore(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_and_transition_survive_store_restart(self):
        request = RunRequest("task-1", {"goal": "summarize", "count": 2})
        queued, created = self.store.create("tenant-a", request)
        self.assertTrue(created)
        self.assertEqual(queued.status, RunStatus.QUEUED)
        self.assertEqual([event.status for event in queued.events], [RunStatus.QUEUED])

        running = self.store.transition("tenant-a", queued.run_id, RunStatus.RUNNING, "Worker started")
        completed = self.store.transition(
            "tenant-a", queued.run_id, RunStatus.COMPLETED, "Run completed",
            result={"summary": "done"},
        )
        restarted = SQLiteRunStore(self.database)
        recovered = restarted.get("tenant-a", queued.run_id)
        self.assertEqual(recovered.status, RunStatus.COMPLETED)
        self.assertEqual(recovered.result, {"summary": "done"})
        self.assertEqual(recovered.created_at, queued.created_at)
        self.assertEqual([event.status for event in recovered.events], [
            RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.COMPLETED,
        ])
        self.assertGreaterEqual(running.updated_at, queued.updated_at)
        self.assertEqual(completed, recovered)
        self.assertEqual(restarted.get_request("tenant-a", queued.run_id).payload,
                         {"goal": "summarize", "count": 2})

    def test_idempotency_is_scoped_per_tenant_and_rejects_changed_input(self):
        request = RunRequest("same-key", {"goal": "one"})
        first, created = self.store.create("tenant-a", request)
        retry, created_again = self.store.create("tenant-a", request)
        other_tenant, other_created = self.store.create("tenant-b", request)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertTrue(other_created)
        self.assertEqual(first.run_id, retry.run_id)
        self.assertNotEqual(first.run_id, other_tenant.run_id)
        with self.assertRaises(IdempotencyConflict):
            self.store.create("tenant-a", RunRequest("same-key", {"goal": "changed"}))

    def test_tenant_cannot_read_or_mutate_another_tenants_run(self):
        created, _ = self.store.create("tenant-a", RunRequest("owned", {}))
        self.assertIsNone(self.store.get("tenant-b", created.run_id))
        with self.assertRaises(KeyError):
            self.store.transition("tenant-b", created.run_id, RunStatus.RUNNING, "start")
        self.assertEqual(self.store.get("tenant-a", created.run_id).status, RunStatus.QUEUED)

    def test_recent_list_is_tenant_scoped_and_bounded(self):
        for index in range(3):
            self.store.create("tenant-a", RunRequest(f"a-{index}", {"n": index}))
        self.store.create("tenant-b", RunRequest("b-1", {}))
        self.assertEqual(len(self.store.list_recent("tenant-a", limit=2)), 2)
        self.assertEqual(len(self.store.list_recent("tenant-b")), 1)
        for invalid in (0, 101, True, "2"):
            with self.subTest(limit=invalid), self.assertRaises(ValueError):
                self.store.list_recent("tenant-a", limit=invalid)

    def test_invalid_transitions_leave_record_and_event_trail_unchanged(self):
        queued, _ = self.store.create("tenant-a", RunRequest("transition", {}))
        with self.assertRaises(RunError):
            self.store.transition("tenant-a", queued.run_id, RunStatus.COMPLETED, "skip worker")
        current = self.store.get("tenant-a", queued.run_id)
        self.assertEqual(current.status, RunStatus.QUEUED)
        self.assertEqual(len(current.events), 1)

    def test_failures_store_only_bounded_error_without_result(self):
        queued, _ = self.store.create("tenant-a", RunRequest("failure", {}))
        self.store.transition("tenant-a", queued.run_id, RunStatus.RUNNING, "Worker started")
        failed = self.store.transition(
            "tenant-a", queued.run_id, RunStatus.FAILED, "Run failed", error="x" * 5_000,
        )
        self.assertIsNone(failed.result)
        self.assertEqual(len(failed.error), 1_000)
        self.assertEqual(failed.status, RunStatus.FAILED)

    def test_result_validation_and_size_limit(self):
        queued, _ = self.store.create("tenant-a", RunRequest("result", {}))
        self.store.transition("tenant-a", queued.run_id, RunStatus.RUNNING, "start")
        with self.assertRaises(ValueError):
            self.store.transition("tenant-a", queued.run_id, RunStatus.COMPLETED,
                                  "done", result={"not_json": object()})
        with self.assertRaises(ValueError):
            self.store.transition("tenant-a", queued.run_id, RunStatus.COMPLETED,
                                  "done", result="x" * 250_001)
        self.assertEqual(self.store.get("tenant-a", queued.run_id).status, RunStatus.RUNNING)

    def test_concurrent_equivalent_submissions_create_exactly_one_run(self):
        request = RunRequest("parallel", {"goal": "one"})
        def submit(_):
            return SQLiteRunStore(self.database).create("tenant-a", request)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(submit, range(16)))
        self.assertEqual(sum(created for _, created in outcomes), 1)
        self.assertEqual(len({snapshot.run_id for snapshot, _ in outcomes}), 1)

    def test_database_permissions_and_symlink_rejection(self):
        if os.name == "posix":
            self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)
        if os.name == "posix":
            outside = Path(self.temp.name) / "outside.sqlite3"
            outside.write_text("not a db", encoding="utf-8")
            link = Path(self.temp.name) / "linked.sqlite3"
            link.symlink_to(outside)
            with self.assertRaises(OSError):
                SQLiteRunStore(link)

    def test_claims_are_atomic_and_only_one_worker_owns_a_queued_run(self):
        self.store.create("tenant-a", RunRequest("claim-one", {"goal": "work"}))
        def claim(_):
            return SQLiteRunStore(self.database).claim_next("tenant-a")
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(claim, range(8)))
        winners = [item for item in claims if item is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].attempt, 1)
        self.assertEqual(winners[0].request.payload, {"goal": "work"})
        self.assertIsNone(self.store.claim_next("tenant-a"))

    def test_global_worker_claims_return_the_verified_owner_partition(self):
        created_a, _ = self.store.create("tenant-a", RunRequest("global-a", {}))
        created_b, _ = self.store.create("tenant-b", RunRequest("global-b", {}))
        claims = [self.store.claim_next(None), self.store.claim_next(None)]
        self.assertEqual({claim.tenant_id for claim in claims}, {"tenant-a", "tenant-b"})
        for claim in claims:
            completed = self.store.finish_claim(
                claim.tenant_id, claim.run_id, claim.lease_token,
                RunStatus.COMPLETED, "done", result={"owner": claim.tenant_id},
            )
            self.assertEqual(completed.status, RunStatus.COMPLETED)
        self.assertEqual(self.store.get("tenant-a", created_a.run_id).result, {"owner": "tenant-a"})
        self.assertEqual(self.store.get("tenant-b", created_b.run_id).result, {"owner": "tenant-b"})

    def test_lease_renewal_and_finish_require_current_worker_token(self):
        created, _ = self.store.create("tenant-a", RunRequest("leased", {"goal": "work"}))
        with patch("niji.durable_run_store.time.time", return_value=1000):
            first = self.store.claim_next("tenant-a", lease_seconds=1)
        with patch("niji.durable_run_store.time.time", return_value=1000.5):
            self.assertTrue(self.store.renew_lease(
                "tenant-a", created.run_id, first.lease_token, lease_seconds=1,
            ))
        with patch("niji.durable_run_store.time.time", return_value=1002):
            second = self.store.claim_next("tenant-a", lease_seconds=10)
            self.assertEqual(second.attempt, 2)
            self.assertFalse(self.store.renew_lease("tenant-a", created.run_id, first.lease_token))
            with self.assertRaises(RunError):
                self.store.finish_claim(
                    "tenant-a", created.run_id, first.lease_token,
                    RunStatus.COMPLETED, "stale completion", result={"wrong": True},
                )
            completed = self.store.finish_claim(
                "tenant-a", created.run_id, second.lease_token,
                RunStatus.COMPLETED, "done", result={"ok": True},
            )
        self.assertEqual(completed.status, RunStatus.COMPLETED)
        self.assertEqual(completed.result, {"ok": True})

    def test_live_cancellation_retains_lease_until_worker_exits(self):
        created, _ = self.store.create("tenant-a", RunRequest("cancel-lease", {}))
        claim = self.store.claim_next("tenant-a", lease_seconds=10)
        self.store.transition("tenant-a", created.run_id, RunStatus.CANCELLING,
                              "Cancellation requested")
        self.assertTrue(self.store.renew_lease(
            "tenant-a", created.run_id, claim.lease_token, lease_seconds=10,
        ))
        self.assertIsNone(self.store.claim_next("tenant-a"))
        cancelled = self.store.finish_claim(
            "tenant-a", created.run_id, claim.lease_token,
            RunStatus.CANCELLED, "Run cancelled",
        )
        self.assertEqual(cancelled.status, RunStatus.CANCELLED)

    def test_api_cancellation_can_mark_live_claim_and_worker_cannot_complete_it(self):
        created, _ = self.store.create("tenant-a", RunRequest("cancel-live", {}))
        claim = self.store.claim_next("tenant-a")
        cancelling = self.store.transition(
            "tenant-a", created.run_id, RunStatus.CANCELLING, "Cancellation requested",
        )
        self.assertEqual(cancelling.status, RunStatus.CANCELLING)
        cancelled = self.store.finish_claim(
            "tenant-a", created.run_id, claim.lease_token,
            RunStatus.COMPLETED, "Worker returned after cancel", result={"ok": True},
        )
        self.assertEqual(cancelled.status, RunStatus.CANCELLED)
        self.assertIsNone(cancelled.result)
        self.assertEqual(cancelled.events[-1].detail, "Run cancelled")

    def test_expired_attempt_limit_marks_run_failed(self):
        created, _ = self.store.create("tenant-a", RunRequest("exhausted", {}))
        with patch("niji.durable_run_store.time.time", return_value=1000):
            first = self.store.claim_next("tenant-a", lease_seconds=1, max_attempts=1)
        self.assertEqual(first.attempt, 1)
        with patch("niji.durable_run_store.time.time", return_value=1002):
            self.assertIsNone(self.store.claim_next("tenant-a", max_attempts=1))
        snapshot = self.store.get("tenant-a", created.run_id)
        self.assertEqual(snapshot.status, RunStatus.FAILED)
        self.assertEqual(snapshot.error, "Worker lease expired too many times")
        self.assertFalse(self.store.renew_lease("tenant-a", created.run_id, first.lease_token))

    def test_v1_database_migrates_to_lease_schema(self):
        path = Path(self.temp.name) / "old.sqlite3"
        SQLiteRunStore(path)
        with sqlite3.connect(path) as connection:
            connection.execute("ALTER TABLE runs DROP COLUMN attempt_count")
            connection.execute("ALTER TABLE runs DROP COLUMN lease_expires_at")
            connection.execute("ALTER TABLE runs DROP COLUMN lease_token_hash")
            connection.execute("PRAGMA user_version=1")
        migrated = SQLiteRunStore(path)
        with sqlite3.connect(path) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        self.assertEqual(version, 7)
        self.assertTrue({"lease_token_hash", "lease_expires_at", "attempt_count"} <= columns)
        self.assertIsNone(migrated.claim_next("tenant-a"))

    def test_v3_database_migrates_global_claim_index(self):
        path = Path(self.temp.name) / "v3.sqlite3"
        SQLiteRunStore(path)
        with sqlite3.connect(path) as connection:
            connection.execute("DROP INDEX runs_by_status_created")
            connection.execute("PRAGMA user_version=3")
        SQLiteRunStore(path)
        with sqlite3.connect(path) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(runs)")}
        self.assertEqual(version, 7)
        self.assertIn("runs_by_status_created", indexes)

    def test_v4_database_migrates_to_current_schema(self):
        path = Path(self.temp.name) / "v4.sqlite3"
        SQLiteRunStore(path)
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version=4")
            connection.execute("DROP INDEX runs_by_tenant_created")
            connection.execute("DROP TABLE rate_limit_buckets")
        SQLiteRunStore(path)
        with sqlite3.connect(path) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            indexes = {row[1] for row in connection.execute("PRAGMA index_list(runs)")}
            rate_buckets = {row[1] for row in connection.execute("PRAGMA table_info(rate_limit_buckets)")}
        self.assertEqual(version, 7)
        self.assertEqual(rate_buckets, {"bucket_key", "window_start", "window_end", "request_count"})
        self.assertIn("runs_by_tenant_created", indexes)
        self.assertIn("runs_by_tenant_status", indexes)
        self.assertIn("runs_by_status_created", indexes)

    def test_v5_database_migrates_rate_limit_table(self):
        path = Path(self.temp.name) / "v5.sqlite3"
        SQLiteRunStore(path)
        with sqlite3.connect(path) as connection:
            connection.execute("PRAGMA user_version=5")
            connection.execute("DROP TABLE rate_limit_buckets")
        SQLiteRunStore(path)
        with sqlite3.connect(path) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            names = {row[1] for row in connection.execute("PRAGMA table_info(rate_limit_buckets)")}
        self.assertEqual(version, 7)
        self.assertEqual(names, {"bucket_key", "window_start", "window_end", "request_count"})

    def test_v6_database_migrates_to_usage_budget_schema(self):
        path = Path(self.temp.name) / "v6.sqlite3"
        SQLiteRunStore(path)
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE run_usage_reservations")
            connection.execute("DROP TABLE monthly_usage")
            connection.execute("PRAGMA user_version=6")
        SQLiteRunStore(path)
        with sqlite3.connect(path) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(version, 7)
        self.assertIn("monthly_usage", tables)
        self.assertIn("run_usage_reservations", tables)

    def test_rate_limit_buckets_are_atomic_and_bounded(self):
        key = "a" * 64
        with patch("niji.durable_run_store.time.time", return_value=1200):
            self.assertIsNone(self.store.consume_rate_limit(key, limit=5, window_seconds=60))
            def consume(_):
                return self.store.consume_rate_limit(key, limit=5, window_seconds=60)
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                retries = list(pool.map(consume, range(16)))
        self.assertEqual(sum(retry is None for retry in retries), 4)
        self.assertTrue(all(retry == 60 for retry in retries if retry is not None))
        with self.assertRaises(ValueError):
            self.store.consume_rate_limit("not-a-digest", limit=5)
        for invalid in (0, True, 100001):
            with self.assertRaises(ValueError):
                self.store.consume_rate_limit(key, limit=invalid)

    def test_rate_limit_bucket_expires_with_its_window(self):
        key = "b" * 64
        with patch("niji.durable_run_store.time.time", return_value=1200):
            self.assertIsNone(self.store.consume_rate_limit(key, limit=1, window_seconds=60))
            self.assertEqual(self.store.consume_rate_limit(key, limit=1, window_seconds=60), 60)
        with patch("niji.durable_run_store.time.time", return_value=1260):
            self.assertIsNone(self.store.consume_rate_limit(key, limit=1, window_seconds=60))

    def test_monthly_usage_reserves_atomically_and_settles_reported_tokens(self):
        kwargs = {
            "monthly_prompt_token_limit": 1000,
            "monthly_completion_token_limit": 500,
            "reserved_prompt_tokens": 600,
            "reserved_completion_tokens": 300,
            "monthly_cost_limit_micros": 80_000_000_000,
            "reserved_cost_micros": 60_000_000_000,
        }
        created, _ = self.store.create_limited(
            "tenant-budget", RunRequest("budget-a", {"prompt": "work"}), **kwargs,
        )
        with self.assertRaisesRegex(RunLimitExceeded, "Monthly token budget"):
            self.store.create_limited(
                "tenant-budget", RunRequest("budget-b", {"prompt": "work"}), **kwargs,
            )
        claim = self.store.claim_next("tenant-budget")
        done = self.store.finish_claim(
            "tenant-budget", created.run_id, claim.lease_token,
            RunStatus.COMPLETED, "done", result={"text": "ok"},
            usage={"prompt_tokens": 120, "completion_tokens": 30,
                   "usage_reported": True, "usage_complete": True,
                   "cost_micros": 40_000_000_000},
        )
        self.assertEqual(done.status, RunStatus.COMPLETED)
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM monthly_usage WHERE tenant_id=?", ("tenant-budget",),
            ).fetchone()
        self.assertEqual(row["prompt_tokens_used"], 120)
        self.assertEqual(row["completion_tokens_used"], 30)
        self.assertEqual(row["prompt_tokens_reserved"], 0)
        self.assertEqual(row["completion_tokens_reserved"], 0)
        self.assertEqual(row["cost_micros_used"], 40_000_000_000)
        self.assertEqual(row["cost_micros_reserved"], 0)

    def test_usage_above_reservation_is_charged_only_at_reserved_ceiling(self):
        created, _ = self.store.create_limited(
            "tenant-overrun", RunRequest("overrun", {"prompt": "work"}),
            monthly_prompt_token_limit=100, monthly_completion_token_limit=50,
            reserved_prompt_tokens=80, reserved_completion_tokens=40,
            monthly_cost_limit_micros=1000, reserved_cost_micros=700,
        )
        claim = self.store.claim_next("tenant-overrun")
        self.store.finish_claim(
            "tenant-overrun", created.run_id, claim.lease_token,
            RunStatus.COMPLETED, "done", result={"text": "ok"},
            usage={"prompt_tokens": 81, "completion_tokens": 10,
                   "usage_reported": True, "usage_complete": True,
                   "cost_micros": 10},
        )
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM monthly_usage WHERE tenant_id=?", ("tenant-overrun",),
            ).fetchone()
        self.assertEqual(row["prompt_tokens_used"], 80)
        self.assertEqual(row["completion_tokens_used"], 40)
        self.assertEqual(row["cost_micros_used"], 700)
        self.assertEqual(row["prompt_tokens_reserved"], 0)
        self.assertEqual(row["cost_micros_reserved"], 0)

    def test_uncertain_provider_usage_charges_the_full_reservation(self):
        created, _ = self.store.create_limited(
            "tenant-fail-closed", RunRequest("uncertain", {"prompt": "work"}),
            monthly_prompt_token_limit=1000, monthly_completion_token_limit=500,
            reserved_prompt_tokens=400, reserved_completion_tokens=200,
            monthly_cost_limit_micros=10_000, reserved_cost_micros=900,
        )
        claim = self.store.claim_next("tenant-fail-closed")
        self.store.finish_claim(
            "tenant-fail-closed", created.run_id, claim.lease_token,
            RunStatus.FAILED, "provider usage incomplete", error="incomplete",
            usage={"prompt_tokens": 7, "completion_tokens": 2,
                   "usage_reported": True, "usage_complete": False},
        )
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM monthly_usage WHERE tenant_id=?", ("tenant-fail-closed",),
            ).fetchone()
        self.assertEqual(row["prompt_tokens_used"], 400)
        self.assertEqual(row["completion_tokens_used"], 200)
        self.assertEqual(row["cost_micros_used"], 900)
        self.assertEqual(row["prompt_tokens_reserved"], 0)
        self.assertEqual(row["cost_micros_reserved"], 0)

    def test_queued_cancellation_releases_usage_reservation(self):
        created, _ = self.store.create_limited(
            "tenant-cancel-budget", RunRequest("cancel-reservation", {"prompt": "work"}),
            monthly_prompt_token_limit=1000, monthly_completion_token_limit=500,
            reserved_prompt_tokens=400, reserved_completion_tokens=200,
        )
        self.store.transition("tenant-cancel-budget", created.run_id,
                              RunStatus.CANCELLED, "Cancelled before execution")
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM monthly_usage WHERE tenant_id=?", ("tenant-cancel-budget",),
            ).fetchone()
        self.assertEqual(row["prompt_tokens_reserved"], 0)
        self.assertEqual(row["completion_tokens_reserved"], 0)
        self.assertEqual(row["prompt_tokens_used"], 0)

    def test_concurrent_runs_cannot_over_reserve_monthly_tokens(self):
        def submit(index):
            try:
                self.store.create_limited(
                    "tenant-race", RunRequest(f"budget-race-{index}", {"prompt": "work"}),
                    max_active_runs=20, max_submissions_per_hour=20,
                    monthly_prompt_token_limit=1000, monthly_completion_token_limit=1000,
                    reserved_prompt_tokens=600, reserved_completion_tokens=600,
                )
                return True
            except Exception as exc:
                if "Monthly token budget" in str(exc):
                    return False
                raise
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            accepted = list(pool.map(submit, range(8)))
        self.assertEqual(sum(accepted), 1)

    def test_validates_tenant_and_run_identifiers(self):
        with self.assertRaises(ValueError):
            self.store.create("../tenant", RunRequest("key", {}))
        for method in (self.store.get, self.store.get_request):
            with self.assertRaises(ValueError):
                method("tenant-a", "../bad")


if __name__ == "__main__":
    unittest.main()
