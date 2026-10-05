import concurrent.futures
import hashlib
import os
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from niji.cloud_api import create_app
from niji.cloud_runtime import (ActiveRunsPreventDeletion, IdempotencyConflict, RunError,
                                RunLimitExceeded, RunRequest, RunStatus)
from niji.cloud_worker import CloudWorker
from niji.postgres_run_store import PostgresRunStore


_DSN = os.environ.get("NIJI_TEST_DATABASE_URL")


@unittest.skipUnless(_DSN, "PostgreSQL integration URL is not configured")
class PostgresRunStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = PostgresRunStore(_DSN)

    def setUp(self):
        self.tenant = "test-" + uuid.uuid4().hex

    def tearDown(self):
        # The integration database is dedicated to tests. Remove rows created under
        # this unique prefix so global multi-tenant claim tests stay deterministic.
        with self.store._connect() as connection:
            connection.execute("DELETE FROM niji_runs WHERE tenant_id LIKE %s", (self.tenant + "%",))
            connection.execute("DELETE FROM niji_monthly_usage WHERE tenant_id LIKE %s", (self.tenant + "%",))

    def test_tenant_data_deletion_is_atomic_scoped_and_rejects_active_runs(self):
        request = RunRequest("delete-me", {"prompt": "private"})
        snapshot, created = self.store.create_limited(
            self.tenant, request, monthly_prompt_token_limit=100,
            monthly_completion_token_limit=100, reserved_prompt_tokens=10,
            reserved_completion_tokens=5,
        )
        self.assertTrue(created)
        other_tenant = self.tenant + "-other"
        other, _ = self.store.create(other_tenant, RunRequest("keep-me", {"prompt": "keep"}))
        tenant_bucket = hashlib.sha256(("tenant:" + self.tenant).encode()).hexdigest()
        self.store.consume_rate_limit(tenant_bucket, limit=10)

        with self.assertRaises(ActiveRunsPreventDeletion):
            self.store.delete_tenant_data(self.tenant)
        self.store.transition(self.tenant, snapshot.run_id, RunStatus.CANCELLED, "Cancelled")

        self.assertEqual(self.store.delete_tenant_data(self.tenant), 1)
        self.assertEqual(self.store.delete_tenant_data(self.tenant), 0)
        self.assertIsNone(self.store.get(self.tenant, snapshot.run_id))
        self.assertIsNotNone(self.store.get(other_tenant, other.run_id))
        with self.store._connect() as connection:
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM niji_monthly_usage WHERE tenant_id=%s", (self.tenant,)
            ).fetchone())
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM niji_run_usage_reservations WHERE tenant_id=%s", (self.tenant,)
            ).fetchone())
            self.assertIsNone(connection.execute(
                "SELECT 1 FROM niji_rate_limit_buckets WHERE bucket_key=%s", (tenant_bucket,)
            ).fetchone())

    def test_healthcheck_queries_the_database(self):
        self.assertTrue(self.store.healthcheck())

    def test_existing_v1_schema_is_upgraded_additively(self):
        with self.store._connect() as connection:
            connection.execute("DROP TABLE IF EXISTS niji_cloud_artifacts")
            connection.execute("DROP TABLE IF EXISTS niji_run_usage_reservations")
            connection.execute("DROP TABLE IF EXISTS niji_monthly_usage")
            connection.execute("UPDATE niji_run_schema SET version=1 WHERE singleton=TRUE")
        upgraded = PostgresRunStore(_DSN)
        with upgraded._connect() as connection:
            row = connection.execute(
                "SELECT version FROM niji_run_schema WHERE singleton=TRUE"
            ).fetchone()
            table = connection.execute(
                "SELECT to_regclass('niji_cloud_artifacts') AS table_name"
            ).fetchone()
            cleanup_index = connection.execute(
                "SELECT to_regclass('niji_cloud_artifacts_created') AS index_name"
            ).fetchone()
            rate_table = connection.execute(
                "SELECT to_regclass('niji_rate_limit_buckets') AS table_name"
            ).fetchone()
            usage_table = connection.execute(
                "SELECT to_regclass('niji_monthly_usage') AS table_name"
            ).fetchone()
            reservation_table = connection.execute(
                "SELECT to_regclass('niji_run_usage_reservations') AS table_name"
            ).fetchone()
        self.assertEqual(row["version"], 7)
        self.assertEqual(table["table_name"], "niji_cloud_artifacts")
        self.assertEqual(cleanup_index["index_name"], "niji_cloud_artifacts_created")
        self.assertEqual(rate_table["table_name"], "niji_rate_limit_buckets")
        self.assertEqual(usage_table["table_name"], "niji_monthly_usage")
        self.assertEqual(reservation_table["table_name"], "niji_run_usage_reservations")
        with upgraded._connect() as connection:
            global_claim_index = connection.execute(
                "SELECT to_regclass('niji_runs_status_created') AS index_name"
            ).fetchone()
        self.assertEqual(global_claim_index["index_name"], "niji_runs_status_created")
        with upgraded._connect() as connection:
            self.assertEqual(
                connection.execute("SELECT to_regclass('niji_runs_tenant_created') AS name").fetchone()["name"],
                "niji_runs_tenant_created",
            )

    def test_idempotency_and_tenant_scoping(self):
        request = RunRequest("same-key", {"goal": "summarize"})
        first, created = self.store.create(self.tenant, request)
        again, created_again = self.store.create(self.tenant, request)
        other, other_created = self.store.create(self.tenant + "-other", request)
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertTrue(other_created)
        self.assertEqual(first.run_id, again.run_id)
        self.assertNotEqual(first.run_id, other.run_id)
        self.assertIsNone(self.store.get(self.tenant + "-other", first.run_id))
        with self.assertRaises(IdempotencyConflict):
            self.store.create(self.tenant, RunRequest("same-key", {"goal": "different"}))

    def test_shared_rate_limit_is_atomic_across_connections(self):
        bucket = hashlib.sha256(self.tenant.encode()).hexdigest()
        def consume(_):
            return PostgresRunStore(_DSN).consume_rate_limit(bucket, limit=5, window_seconds=60)
        with patch("niji.postgres_run_store.time.time", return_value=1200):
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                results = list(pool.map(consume, range(20)))
        self.assertEqual(sum(result is None for result in results), 5)
        self.assertTrue(all(isinstance(result, int) and result >= 1
                            for result in results if result is not None))
        with self.store._connect() as connection:
            connection.execute("DELETE FROM niji_rate_limit_buckets WHERE bucket_key=%s", (bucket,))

    def test_atomic_claim_allows_only_one_worker_to_own_one_run(self):
        self.store.create(self.tenant, RunRequest("claim", {"goal": "work"}))
        def claim(_):
            return PostgresRunStore(_DSN).claim_next(self.tenant)
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(claim, range(8)))
        winners = [item for item in outcomes if item is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0].attempt, 1)
        self.assertEqual(winners[0].request.payload, {"goal": "work"})

    def test_global_worker_claims_keep_each_user_partition(self):
        tenant_a = self.tenant + "-a"
        tenant_b = self.tenant + "-b"
        created_a, _ = self.store.create(tenant_a, RunRequest("global-a", {}))
        created_b, _ = self.store.create(tenant_b, RunRequest("global-b", {}))
        claims = [self.store.claim_next(None), self.store.claim_next(None)]
        self.assertEqual({claim.tenant_id for claim in claims}, {tenant_a, tenant_b})
        for claim in claims:
            result = self.store.finish_claim(
                claim.tenant_id, claim.run_id, claim.lease_token,
                RunStatus.COMPLETED, "done", result={"owner": claim.tenant_id},
            )
            self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(self.store.get(tenant_a, created_a.run_id).result, {"owner": tenant_a})
        self.assertEqual(self.store.get(tenant_b, created_b.run_id).result, {"owner": tenant_b})

    def test_concurrent_submissions_cannot_exceed_tenant_active_limit(self):
        def submit(index):
            try:
                return self.store.create_limited(
                    self.tenant,
                    RunRequest(f"limited-{index}", {"goal": "work"}),
                    max_active_runs=2,
                    max_submissions_per_hour=100,
                )[1]
            except RunLimitExceeded:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))
        self.assertEqual(sum(results), 2)
        self.assertEqual(len(self.store.list_recent(self.tenant)), 2)

    def test_monthly_usage_reservations_are_atomic_and_settle_on_completion(self):
        kwargs = {
            "max_active_runs": 10, "max_submissions_per_hour": 10,
            "monthly_prompt_token_limit": 1000,
            "monthly_completion_token_limit": 500,
            "reserved_prompt_tokens": 600, "reserved_completion_tokens": 300,
            "monthly_cost_limit_micros": 80_000_000_000,
            "reserved_cost_micros": 60_000_000_000,
        }
        created, _ = self.store.create_limited(
            self.tenant, RunRequest("monthly-a", {"prompt": "work"}), **kwargs,
        )
        with self.assertRaisesRegex(RunLimitExceeded, "Monthly token budget"):
            self.store.create_limited(
                self.tenant, RunRequest("monthly-b", {"prompt": "work"}), **kwargs,
            )
        claim = self.store.claim_next(self.tenant)
        self.store.finish_claim(
            self.tenant, created.run_id, claim.lease_token,
            RunStatus.COMPLETED, "done", result={"ok": True},
            usage={"prompt_tokens": 100, "completion_tokens": 20,
                   "usage_reported": True, "usage_complete": True,
                   "cost_micros": 40_000_000_000},
        )
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM niji_monthly_usage WHERE tenant_id=%s", (self.tenant,),
            ).fetchone()
        self.assertEqual(row["prompt_tokens_used"], 100)
        self.assertEqual(row["completion_tokens_used"], 20)
        self.assertEqual(row["prompt_tokens_reserved"], 0)
        self.assertEqual(row["cost_micros_used"], 40_000_000_000)
        self.assertEqual(row["cost_micros_reserved"], 0)

    def test_usage_above_reservation_is_charged_only_at_reserved_ceiling(self):
        created, _ = self.store.create_limited(
            self.tenant, RunRequest("overrun", {"prompt": "work"}),
            monthly_prompt_token_limit=100, monthly_completion_token_limit=50,
            reserved_prompt_tokens=80, reserved_completion_tokens=40,
            monthly_cost_limit_micros=1000, reserved_cost_micros=700,
        )
        claim = self.store.claim_next(self.tenant)
        self.store.finish_claim(
            self.tenant, created.run_id, claim.lease_token,
            RunStatus.COMPLETED, "done", result={"text": "ok"},
            usage={"prompt_tokens": 81, "completion_tokens": 10,
                   "usage_reported": True, "usage_complete": True,
                   "cost_micros": 10},
        )
        with self.store._connect() as connection:
            row = connection.execute(
                "SELECT * FROM niji_monthly_usage WHERE tenant_id=%s", (self.tenant,),
            ).fetchone()
        self.assertEqual(row["prompt_tokens_used"], 80)
        self.assertEqual(row["completion_tokens_used"], 40)
        self.assertEqual(row["cost_micros_used"], 700)
        self.assertEqual(row["prompt_tokens_reserved"], 0)
        self.assertEqual(row["cost_micros_reserved"], 0)

    def test_concurrent_submissions_cannot_overreserve_monthly_tokens(self):
        def submit(index):
            store = PostgresRunStore(_DSN)
            try:
                store.create_limited(
                    self.tenant, RunRequest(f"quota-race-{index}", {"prompt": "work"}),
                    max_active_runs=20, max_submissions_per_hour=20,
                    monthly_prompt_token_limit=1000, monthly_completion_token_limit=1000,
                    reserved_prompt_tokens=600, reserved_completion_tokens=600,
                )
                return True
            except RunLimitExceeded:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(submit, range(8)))
        self.assertEqual(sum(outcomes), 1)

    def test_expired_lease_recovery_rejects_stale_worker_result(self):
        created, _ = self.store.create(self.tenant, RunRequest("recover", {}))
        with patch("niji.postgres_run_store.time.time", return_value=1000):
            first = self.store.claim_next(self.tenant, lease_seconds=1)
        with patch("niji.postgres_run_store.time.time", return_value=1002):
            second = self.store.claim_next(self.tenant, lease_seconds=10)
            self.assertEqual(second.attempt, 2)
            with self.assertRaises(RunError):
                self.store.finish_claim(self.tenant, created.run_id, first.lease_token,
                                        RunStatus.COMPLETED, "stale", result={"bad": True})
            done = self.store.finish_claim(self.tenant, created.run_id, second.lease_token,
                                           RunStatus.COMPLETED, "done", result={"ok": True})
        self.assertEqual(done.status, RunStatus.COMPLETED)
        self.assertEqual(done.result, {"ok": True})

    def test_live_cancellation_retains_lease_until_worker_exits(self):
        created, _ = self.store.create(self.tenant, RunRequest("cancel-lease", {}))
        claim = self.store.claim_next(self.tenant, lease_seconds=10)
        self.store.transition(self.tenant, created.run_id, RunStatus.CANCELLING,
                              "Cancellation requested")
        self.assertTrue(self.store.renew_lease(
            self.tenant, created.run_id, claim.lease_token, lease_seconds=10,
        ))
        self.assertIsNone(self.store.claim_next(self.tenant))
        cancelled = self.store.finish_claim(
            self.tenant, created.run_id, claim.lease_token,
            RunStatus.CANCELLED, "Run cancelled",
        )
        self.assertEqual(cancelled.status, RunStatus.CANCELLED)

    def test_cancellation_wins_over_late_worker_completion(self):
        created, _ = self.store.create(self.tenant, RunRequest("cancel", {}))
        claim = self.store.claim_next(self.tenant)
        self.store.transition(self.tenant, created.run_id, RunStatus.CANCELLING,
                              "Cancellation requested")
        done = self.store.finish_claim(self.tenant, created.run_id, claim.lease_token,
                                       RunStatus.COMPLETED, "late completion", result={"ok": True})
        self.assertEqual(done.status, RunStatus.CANCELLED)
        self.assertIsNone(done.result)

    def test_progress_events_are_safe_bounded_and_lease_owned(self):
        created, _ = self.store.create(self.tenant, RunRequest("progress", {"prompt": "hello"}))
        claim = self.store.claim_next(self.tenant)
        updated = self.store.record_progress(
            self.tenant, created.run_id, claim.lease_token, "Agent is thinking"
        )
        self.assertEqual(updated.events[-1].detail, "Agent is thinking")
        with self.assertRaises(ValueError):
            self.store.record_progress(
                self.tenant, created.run_id, claim.lease_token, "private prompt text"
            )
        with self.assertRaises(RunError):
            self.store.record_progress(
                self.tenant, created.run_id, "wrong-worker", "Agent is working"
            )

    def test_artifact_storage_is_lease_owned_bounded_tenant_scoped_and_private_until_complete(self):
        created, _ = self.store.create(self.tenant, RunRequest("artifact", {"prompt": "make a file"}))
        claim = self.store.claim_next(self.tenant)
        with self.assertRaises(RunError):
            self.store.add_artifact(self.tenant, created.run_id, "wrong-worker", "out.txt", b"x", "text/plain")
        with self.assertRaises(ValueError):
            self.store.add_artifact(self.tenant, created.run_id, claim.lease_token, "../secret.txt", b"x", "text/plain")
        with self.assertRaises(ValueError):
            self.store.add_artifact(self.tenant, created.run_id, claim.lease_token, ".env", b"x", "text/plain")
        with self.assertRaises(ValueError):
            self.store.add_artifact(self.tenant, created.run_id, claim.lease_token, "large.bin", b"x" * 1_000_001, "application/octet-stream")
        self.assertEqual(self.store.list_artifacts(self.tenant, created.run_id), [])
        artifact = self.store.add_artifact(
            self.tenant, created.run_id, claim.lease_token, "reports/result.txt", b"hello", "text/plain",
        )
        self.assertIsNone(self.store.get_artifact(self.tenant, created.run_id, artifact.artifact_id))
        with self.assertRaises(RunError):
            self.store.add_artifact(self.tenant + "-other", created.run_id, claim.lease_token, "other.txt", b"x", "text/plain")
        self.store.finish_claim(
            self.tenant, created.run_id, claim.lease_token,
            RunStatus.COMPLETED, "done", result={"text": "complete"},
        )
        listed = self.store.list_artifacts(self.tenant, created.run_id)
        self.assertEqual([item.artifact_id for item in listed], [artifact.artifact_id])
        self.assertEqual(self.store.list_artifacts(self.tenant + "-other", created.run_id), [])
        downloaded = self.store.get_artifact(self.tenant, created.run_id, artifact.artifact_id)
        self.assertEqual(downloaded.data, b"hello")
        self.assertIsNone(self.store.get_artifact(self.tenant, created.run_id, "not-an-id"))

    def test_artifacts_are_deleted_when_run_does_not_complete(self):
        created, _ = self.store.create(self.tenant, RunRequest("artifact-cancel", {"prompt": "work"}))
        claim = self.store.claim_next(self.tenant)
        artifact = self.store.add_artifact(
            self.tenant, created.run_id, claim.lease_token, "out.txt", b"partial", "text/plain",
        )
        self.store.finish_claim(
            self.tenant, created.run_id, claim.lease_token,
            RunStatus.CANCELLED, "cancelled",
        )
        self.assertIsNone(self.store.get_artifact(self.tenant, created.run_id, artifact.artifact_id))
        self.assertEqual(self.store.list_artifacts(self.tenant, created.run_id), [])

    def test_artifact_retention_deletes_only_expired_files(self):
        with patch("niji.postgres_run_store.time.time", return_value=1000):
            created, _ = self.store.create(
                self.tenant, RunRequest("artifact-retention", {"prompt": "work"})
            )
            claim = self.store.claim_next(self.tenant)
            artifact = self.store.add_artifact(
                self.tenant, created.run_id, claim.lease_token,
                "report.txt", b"old", "text/plain",
            )
            self.store.finish_claim(
                self.tenant, created.run_id, claim.lease_token,
                RunStatus.COMPLETED, "done", result={"ok": True},
            )
        with patch("niji.postgres_run_store.time.time", return_value=1000 + 86_401):
            self.assertEqual(self.store.cleanup_expired_artifacts(retention_days=1), 1)
        self.assertIsNone(self.store.get_artifact(self.tenant, created.run_id, artifact.artifact_id))
        with self.assertRaises(ValueError):
            self.store.cleanup_expired_artifacts(retention_days=0)
        with self.assertRaises(ValueError):
            self.store.cleanup_expired_artifacts(retention_days=True)

    def test_artifact_retention_preserves_files_for_active_runs(self):
        with patch("niji.postgres_run_store.time.time", return_value=1000):
            created, _ = self.store.create(
                self.tenant, RunRequest("artifact-retention-active", {"prompt": "work"})
            )
            claim = self.store.claim_next(self.tenant)
            self.store.add_artifact(
                self.tenant, created.run_id, claim.lease_token,
                "report.txt", b"still working", "text/plain",
            )
        with patch("niji.postgres_run_store.time.time", return_value=1000 + 86_401):
            self.assertEqual(self.store.cleanup_expired_artifacts(retention_days=1), 0)

    def test_result_limits_and_recent_list_limits(self):
        first, _ = self.store.create(self.tenant, RunRequest("result-a", {}))
        self.store.create(self.tenant, RunRequest("result-b", {}))
        claim = self.store.claim_next(self.tenant)
        with self.assertRaises(ValueError):
            self.store.finish_claim(self.tenant, first.run_id, claim.lease_token,
                                    RunStatus.COMPLETED, "too large", result="x" * 250_001)
        with self.assertRaises(ValueError):
            self.store.list_recent(self.tenant, limit=101)
        self.assertEqual(len(self.store.list_recent(self.tenant, limit=1)), 1)


class PostgresRunStoreUnitTests(unittest.TestCase):
    def test_existing_schema_validation_rejects_missing_core_columns(self):
        class Connection:
            def execute(self, _query, _params):
                return SimpleNamespace(fetchall=lambda: [
                    {"table_name": "niji_runs", "column_name": "run_id"},
                    {"table_name": "niji_run_events", "column_name": "run_id"},
                ])

        with self.assertRaisesRegex(RunError, "niji_runs.fingerprint"):
            PostgresRunStore._validate_existing_schema(Connection(), 1)

    def test_artifact_cleanup_uses_a_bounded_instance_method_and_retention_cutoff(self):
        class Connection:
            query = None
            params = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query, params):
                self.query = query
                self.params = params
                return SimpleNamespace(rowcount=2)

        connection = Connection()
        store = object.__new__(PostgresRunStore)
        store._connect = lambda: connection
        with patch("niji.postgres_run_store.time.time", return_value=1000):
            self.assertEqual(store.cleanup_expired_artifacts(retention_days=2), 2)
        self.assertIn("DELETE FROM niji_cloud_artifacts", connection.query)
        self.assertIn("r.status=%s", connection.query)
        self.assertIn("LIMIT 1000", connection.query)
        self.assertEqual(connection.params, (1000 - 2 * 86_400, RunStatus.COMPLETED.value))
        with self.assertRaises(ValueError):
            store.cleanup_expired_artifacts(retention_days=True)


if __name__ == "__main__":
    unittest.main()
