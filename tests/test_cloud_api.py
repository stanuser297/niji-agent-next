import base64
import hashlib
import io
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
import zipfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from niji.cloud_api import _client_ip, _parse_trusted_proxy_cidrs, create_app
from niji.cloud_runtime import RunRequest, RunStatus
from niji.durable_run_store import SQLiteRunStore
from niji.postgres_run_store import StoredArtifact


TOKEN = "test-token-" + "x" * 48


class CloudApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteRunStore(Path(self.temp.name) / "runs.sqlite3")
        self.app = create_app(store=self.store, api_token=TOKEN, tenant_id="tenant-one")
        self.client = TestClient(self.app)
        self.headers = {"Authorization": f"Bearer {TOKEN}"}

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def submit(self, key="request-1", payload=None, **extra):
        body = {"idempotency_key": key, "payload": payload or {"prompt": "summarize"}}
        body.update(extra)
        return self.client.post("/v1/runs", json=body, headers=self.headers)

    @staticmethod
    def _archive_base64():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("src/main.py", "print('hi')\\n")
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def test_health_is_public_but_api_docs_are_disabled(self):
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/docs").status_code, 404)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)

    def test_health_reports_database_readiness_without_leaking_diagnostics(self):
        self.store.healthcheck = lambda: False
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"detail": "Service not ready"})
        self.assertNotIn("database", response.text.lower())

    def test_capabilities_require_auth_and_report_effective_execution_mode(self):
        self.assertEqual(self.client.get("/v1/capabilities").status_code, 401)
        prompt = self.client.get("/v1/capabilities", headers=self.headers)
        self.assertEqual(prompt.status_code, 200)
        prompt_data = prompt.json()
        self.assertEqual(prompt_data["product"], "Niji Agent")
        self.assertEqual(prompt_data["execution_mode"], "prompt")
        self.assertTrue(prompt_data["features"]["prompt_runs"])
        self.assertFalse(prompt_data["features"]["text_file_upload"])
        self.assertEqual(prompt_data["sandbox_tools"], [])
        self.assertEqual([item["name"] for item in prompt_data["models"]], [
            "Nemotron 3.5 Lightning", "Nemotron 3 Super 120B",
        ])
        self.assertNotIn("provider", prompt.text.lower())
        self.assertNotIn("api_key", prompt.text.lower())
        self.assertNotIn("base_url", prompt.text.lower())
        self.assertNotIn("provider_api_key", prompt.text)
        selected = self.submit("model-choice", {"prompt": "hi", "model": "nemotron-3.5-lightning"})
        self.assertEqual(selected.status_code, 202)
        self.assertEqual(selected.json()["status"], "queued")
        unknown = self.submit("unknown-model", {"prompt": "hi", "model": "openai/gpt-5"})
        self.assertEqual(unknown.status_code, 422)

        sandbox_app = create_app(
            store=self.store, api_token=TOKEN, tenant_id="tenant-one", execution_mode="sandbox",
        )
        with TestClient(sandbox_app) as sandbox_client:
            sandbox = sandbox_client.get("/v1/capabilities", headers=self.headers)
        self.assertEqual(sandbox.status_code, 200)
        sandbox_data = sandbox.json()
        self.assertEqual(sandbox_data["execution_mode"], "sandbox")
        self.assertTrue(sandbox_data["features"]["text_file_upload"])
        self.assertEqual(sandbox_data["models"], [])
        self.assertIn("bash", sandbox_data["sandbox_tools"])

    def test_bearer_authentication_is_required_and_constant_shape(self):
        response = self.client.get("/v1/runs")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.assertEqual(self.client.get("/v1/runs", headers={
            "Authorization": "Bearer " + "z" * len(TOKEN),
        }).status_code, 401)

    def test_create_returns_accepted_run_without_echoing_private_request(self):
        response = self.submit()
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.headers["location"], f"/v1/runs/{response.json()['run_id']}")
        self.assertEqual(response.json()["status"], "queued")
        self.assertEqual(response.json()["events"][0]["detail"], "Run accepted")
        self.assertNotIn("payload", response.json())

    def test_idempotency_retry_returns_existing_run_and_conflict_is_safe(self):
        first = self.submit("retry-key")
        retry = self.submit("retry-key")
        conflict = self.submit("retry-key", {"prompt": "different"})
        self.assertEqual(first.status_code, 202)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(first.json()["run_id"], retry.json()["run_id"])
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["detail"], "Idempotency key conflicts with an earlier request")
        self.assertEqual(len(self.client.get("/v1/runs", headers=self.headers).json()["runs"]), 1)

    def test_get_and_list_return_only_server_tenant_records(self):
        created = self.submit().json()
        fetched = self.client.get(f"/v1/runs/{created['run_id']}", headers=self.headers)
        listed = self.client.get("/v1/runs", headers=self.headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["run_id"], created["run_id"])
        self.assertEqual(len(listed.json()["runs"]), 1)
        self.assertEqual(self.client.get("/v1/runs/not-a-run", headers=self.headers).status_code, 404)

    def test_blocking_store_read_does_not_block_the_api_event_loop(self):
        created = self.submit().json()
        entered = threading.Event()
        release = threading.Event()
        original_get = self.store.get

        def blocked_get(*args, **kwargs):
            entered.set()
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release the store read")
            return original_get(*args, **kwargs)

        self.store.get = blocked_get
        with ThreadPoolExecutor(max_workers=2) as executor:
            run_future = executor.submit(
                self.client.get, f"/v1/runs/{created['run_id']}", headers=self.headers
            )
            self.assertTrue(entered.wait(timeout=2), "run detail did not reach the store")
            health_future = executor.submit(self.client.get, "/healthz")
            try:
                health = health_future.result(timeout=1)
                self.assertEqual(health.status_code, 200)
            finally:
                release.set()
            self.assertEqual(run_future.result(timeout=3).status_code, 200)

    def test_list_limit_is_bounded(self):
        for index in range(3):
            self.submit(f"list-{index}")
        response = self.client.get("/v1/runs?limit=2", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["runs"]), 2)
        self.assertEqual(self.client.get("/v1/runs?limit=101", headers=self.headers).status_code, 422)

    def test_run_admission_limits_active_queue_and_hourly_submissions(self):
        store = SQLiteRunStore(Path(self.temp.name) / "limited.sqlite3")
        app = create_app(
            store=store, api_token=TOKEN, tenant_id="tenant-limited",
            max_active_runs=1, max_submissions_per_hour=2,
        )
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {TOKEN}"}
            first = client.post("/v1/runs", json={
                "idempotency_key": "limit-1", "payload": {"prompt": "one"},
            }, headers=headers)
            self.assertEqual(first.status_code, 202)
            retry = client.post("/v1/runs", json={
                "idempotency_key": "limit-1", "payload": {"prompt": "one"},
            }, headers=headers)
            self.assertEqual(retry.status_code, 200)
            blocked_active = client.post("/v1/runs", json={
                "idempotency_key": "limit-2", "payload": {"prompt": "two"},
            }, headers=headers)
            self.assertEqual(blocked_active.status_code, 429)
            self.assertEqual(blocked_active.json()["detail"], "Active run limit reached")
            self.assertGreaterEqual(int(blocked_active.headers["retry-after"]), 1)
            client.post(f"/v1/runs/{first.json()['run_id']}/cancel", headers=headers)
            second = client.post("/v1/runs", json={
                "idempotency_key": "limit-2", "payload": {"prompt": "two"},
            }, headers=headers)
            self.assertEqual(second.status_code, 202)
            # A terminal run no longer occupies the active queue, but still counts
            # toward the hourly admission budget.
            client.post(f"/v1/runs/{second.json()['run_id']}/cancel", headers=headers)
            hourly = client.post("/v1/runs", json={
                "idempotency_key": "limit-3", "payload": {"prompt": "three"},
            }, headers=headers)
            self.assertEqual(hourly.status_code, 429)
            self.assertEqual(hourly.json()["detail"], "Hourly run submission limit reached")
            self.assertGreaterEqual(int(hourly.headers["retry-after"]), 1)

    def test_sandbox_mode_accepts_only_valid_pinned_repository_reference(self):
        app = create_app(
            store=self.store, api_token=TOKEN, tenant_id="tenant-one", execution_mode="sandbox",
        )
        with TestClient(app) as client:
            accepted = client.post("/v1/runs", json={
                "idempotency_key": "public-repo",
                "payload": {"prompt": "review this code", "repository": {
                    "url": "https://github.com/example/demo", "revision": "a" * 40,
                }},
            }, headers=self.headers)
            rejected = client.post("/v1/runs", json={
                "idempotency_key": "unsafe-repo",
                "payload": {"prompt": "review this code", "repository": {
                    "url": "https://127.0.0.1/private", "revision": "main",
                }},
            }, headers=self.headers)
        selected_in_sandbox = client.post("/v1/runs", json={
            "idempotency_key": "model-in-sandbox",
            "payload": {"prompt": "hi", "model": "nemotron-3.5-lightning"},
        }, headers=self.headers)
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(selected_in_sandbox.status_code, 422)

    def test_monthly_token_budget_reserves_and_releases_capacity(self):
        app = create_app(
            store=self.store, api_token=TOKEN, tenant_id="tenant-budget",
            monthly_prompt_token_limit=1000, monthly_completion_token_limit=500,
            run_prompt_token_reservation=600, run_completion_token_reservation=300,
        )
        with TestClient(app) as client:
            first = client.post("/v1/runs", json={
                "idempotency_key": "budget-first", "payload": {"prompt": "one"},
            }, headers=self.headers)
            blocked = client.post("/v1/runs", json={
                "idempotency_key": "budget-second", "payload": {"prompt": "two"},
            }, headers=self.headers)
            self.assertEqual(first.status_code, 202)
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(blocked.json()["detail"], "Monthly token budget reached")
            self.assertGreaterEqual(int(blocked.headers["retry-after"]), 1)
            cancelled = client.post(f"/v1/runs/{first.json()['run_id']}/cancel", headers=self.headers)
            self.assertEqual(cancelled.status_code, 200)
            accepted = client.post("/v1/runs", json={
                "idempotency_key": "budget-second", "payload": {"prompt": "two"},
            }, headers=self.headers)
            self.assertEqual(accepted.status_code, 202)

    def test_idempotent_retries_do_not_reserve_monthly_quota_twice(self):
        app = create_app(
            store=self.store, api_token=TOKEN, tenant_id="tenant-idempotent-budget",
            monthly_prompt_token_limit=1000, monthly_completion_token_limit=500,
            run_prompt_token_reservation=600, run_completion_token_reservation=300,
        )
        with TestClient(app) as client:
            first = client.post("/v1/runs", json={
                "idempotency_key": "same-budgeted-run", "payload": {"prompt": "one"},
            }, headers=self.headers)
            retry = client.post("/v1/runs", json={
                "idempotency_key": "same-budgeted-run", "payload": {"prompt": "one"},
            }, headers=self.headers)
            another = client.post("/v1/runs", json={
                "idempotency_key": "different-budgeted-run", "payload": {"prompt": "two"},
            }, headers=self.headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(first.json()["run_id"], retry.json()["run_id"])
        self.assertEqual(another.status_code, 429)
        self.assertEqual(another.json()["detail"], "Monthly token budget reached")

    def test_spend_budgets_use_usd_bounds_not_token_count_bounds(self):
        with patch.dict(os.environ, {
            "NIJI_CLOUD_MAX_MONTHLY_SPEND_USD": "50000",
            "NIJI_CLOUD_INPUT_PRICE_USD_PER_MILLION_TOKENS": "100000",
            "NIJI_CLOUD_OUTPUT_PRICE_USD_PER_MILLION_TOKENS": "100000",
        }, clear=False):
            app = create_app(
                store=self.store, api_token=TOKEN, tenant_id="tenant-large-spend-cap",
            )
        with TestClient(app) as client:
            response = client.post("/v1/runs", json={
                "idempotency_key": "large-spend-budget", "payload": {"prompt": "work"},
            }, headers=self.headers)
        self.assertEqual(response.status_code, 202)

    def test_partial_monthly_spend_pricing_configuration_is_rejected(self):
        env_names = (
            "NIJI_CLOUD_MAX_MONTHLY_SPEND_USD",
            "NIJI_CLOUD_INPUT_PRICE_USD_PER_MILLION_TOKENS",
            "NIJI_CLOUD_OUTPUT_PRICE_USD_PER_MILLION_TOKENS",
        )
        for partial in (
            {env_names[0]: "1"},
            {env_names[1]: "1"},
            {env_names[0]: "1", env_names[1]: "1"},
        ):
            env = {name: "" for name in env_names}
            env.update(partial)
            with self.subTest(config=partial), patch.dict(os.environ, env, clear=False):
                with self.assertRaisesRegex(ValueError, "requires its limit and both explicit token prices"):
                    create_app(store=self.store, api_token=TOKEN, tenant_id="tenant-pricing")

    def test_explicit_usd_pricing_enables_monthly_spend_circuit_breaker(self):
        with patch.dict(os.environ, {
            "NIJI_CLOUD_MAX_MONTHLY_SPEND_USD": "0.30",
            "NIJI_CLOUD_INPUT_PRICE_USD_PER_MILLION_TOKENS": "1",
            "NIJI_CLOUD_OUTPUT_PRICE_USD_PER_MILLION_TOKENS": "2",
        }, clear=False):
            app = create_app(store=self.store, api_token=TOKEN, tenant_id="tenant-cost")
        with TestClient(app) as client:
            first = client.post("/v1/runs", json={
                "idempotency_key": "spend-first", "payload": {"prompt": "one"},
            }, headers=self.headers)
            second = client.post("/v1/runs", json={
                "idempotency_key": "spend-second", "payload": {"prompt": "two"},
            }, headers=self.headers)
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 429)
        self.assertEqual(second.json()["detail"], "Monthly spend budget reached")

    def test_authenticated_requests_are_limited_per_user(self):
        app = create_app(
            store=self.store, api_token=TOKEN, tenant_id="tenant-one",
            max_requests_per_minute=2, max_requests_per_ip_per_minute=100,
        )
        with TestClient(app) as client:
            self.assertEqual(client.get("/v1/runs", headers=self.headers).status_code, 200)
            self.assertEqual(client.get("/v1/runs", headers=self.headers).status_code, 200)
            limited = client.get("/v1/runs", headers=self.headers)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.json()["detail"], "Request rate limit exceeded")
        self.assertGreaterEqual(int(limited.headers["retry-after"]), 1)

    def test_authenticated_requests_are_limited_per_client_ip(self):
        app = create_app(
            store=self.store, api_token=TOKEN, tenant_id="tenant-one",
            max_requests_per_minute=100, max_requests_per_ip_per_minute=2,
        )
        with TestClient(app, client=("198.51.100.20", 12345)) as client:
            self.assertEqual(client.get("/v1/runs", headers=self.headers).status_code, 200)
            self.assertEqual(client.get("/v1/runs", headers=self.headers).status_code, 200)
            limited = client.get("/v1/runs", headers=self.headers)
        self.assertEqual(limited.status_code, 429)

    def test_forwarded_ip_is_ignored_unless_the_peer_is_a_trusted_proxy(self):
        from starlette.requests import Request

        def request(peer, forwarded):
            return Request({
                "type": "http", "http_version": "1.1", "method": "GET",
                "scheme": "http", "path": "/", "raw_path": b"/",
                "query_string": b"", "headers": [(b"x-forwarded-for", forwarded.encode())],
                "client": (peer, 1234), "server": ("testserver", 80),
            })

        headers = request("198.51.100.2", "203.0.113.9")
        self.assertEqual(_client_ip(headers, ()), "198.51.100.2")
        trusted = _parse_trusted_proxy_cidrs("10.0.0.0/8, 2001:db8::/32")
        proxied = request("10.0.0.4", "203.0.113.9, 10.0.0.3")
        self.assertEqual(_client_ip(proxied, trusted), "203.0.113.9")
        with self.assertRaises(ValueError):
            _parse_trusted_proxy_cidrs("not-a-network")

    def test_cancel_queued_run_and_keep_terminal_runs_unchanged(self):
        created = self.submit().json()
        cancelled = self.client.post(f"/v1/runs/{created['run_id']}/cancel", headers=self.headers)
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["status"], "cancelled")
        self.assertEqual([event["status"] for event in cancelled.json()["events"]], ["queued", "cancelled"])
        repeated = self.client.post(f"/v1/runs/{created['run_id']}/cancel", headers=self.headers)
        self.assertEqual(repeated.json()["status"], "cancelled")

    def test_unknown_or_cross_tenant_run_is_not_disclosed(self):
        other_tenant_snapshot, _ = self.store.create("tenant-two", RunRequest("owned-elsewhere", {}))
        response = self.client.get(f"/v1/runs/{other_tenant_snapshot.run_id}", headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Run not found")
        cancelled = self.client.post(
            f"/v1/runs/{other_tenant_snapshot.run_id}/cancel", headers=self.headers,
        )
        self.assertEqual(cancelled.status_code, 404)

    def test_invalid_json_run_request_is_rejected_without_persistence(self):
        for body in (
            {"idempotency_key": "unsafe/key", "payload": {"prompt": "hello"}},
            {"idempotency_key": "missing-payload"},
            {"idempotency_key": "extra-field", "payload": {}, "tenant_id": "attacker"},
            {"idempotency_key": "boolean-timeout", "payload": {"prompt": "hello"}, "timeout_seconds": True},
            {"idempotency_key": "extra-payload", "payload": {"prompt": "hello", "tools": ["shell"]}},
            {"idempotency_key": "blank-prompt", "payload": {"prompt": "  "}},
        ):
            response = self.client.post("/v1/runs", json=body, headers=self.headers)
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/v1/runs", headers=self.headers).json()["runs"], [])

    def test_rejects_oversized_body_even_when_content_length_is_missing(self):
        body = b'{"idempotency_key":"large","payload":{"data":"' + b"x" * 1_050_000 + b'"}}'
        response = self.client.post(
            "/v1/runs", content=body,
            headers={**self.headers, "content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 413)

    def test_prompt_mode_rejects_project_file_and_archive_uploads(self):
        for payload in (
            {"prompt": "review this file", "files": [{"path": "main.py", "content": "print('hi')"}]},
            {"prompt": "review this file", "archive_base64": self._archive_base64()},
            {"prompt": "review this repository", "repository": {
                "url": "https://github.com/example/demo", "revision": "a" * 40,
            }},
        ):
            response = self.submit(payload=payload)
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/v1/runs", headers=self.headers).json()["runs"], [])

    def test_sandbox_mode_accepts_bounded_text_project_files(self):
        app = create_app(
            store=self.store, api_token=TOKEN, tenant_id="tenant-one", execution_mode="sandbox",
        )
        with TestClient(app) as client:
            response = client.post("/v1/runs", json={
                "idempotency_key": "sandbox-files",
                "payload": {
                    "prompt": "review this file",
                    "files": [{"path": "src/main.py", "content": "print('hi')"}],
                },
            }, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "queued")

    def test_sandbox_mode_accepts_safe_zip_archive_upload(self):
        app = create_app(
            store=self.store, api_token=TOKEN, tenant_id="tenant-one", execution_mode="sandbox",
        )
        with TestClient(app) as client:
            response = client.post("/v1/runs", json={
                "idempotency_key": "sandbox-archive",
                "payload": {
                    "prompt": "review this project",
                    "archive_base64": self._archive_base64(),
                },
            }, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "queued")

    def test_artifact_download_requires_completion_and_is_attachment_only(self):
        created = self.submit().json()
        pending = self.client.get(
            f"/v1/runs/{created['run_id']}/artifacts", headers=self.headers,
        )
        self.assertEqual(pending.status_code, 409)
        self.store.transition("tenant-one", created["run_id"], RunStatus.RUNNING, "Started")
        self.store.transition("tenant-one", created["run_id"], RunStatus.COMPLETED, "Done", result={"ok": True})
        artifact = StoredArtifact(
            "a" * 32, "reports/hello world.txt", "text/plain", "b" * 64, 5, 100.0, b"hello",
        )
        self.store.list_artifacts = lambda _tenant, _run: [artifact]
        self.store.get_artifact = lambda _tenant, _run, _id: artifact

        listed = self.client.get(
            f"/v1/runs/{created['run_id']}/artifacts", headers=self.headers,
        )
        self.assertEqual(listed.status_code, 200)
        entry = listed.json()["artifacts"][0]
        self.assertEqual(entry["path"], "reports/hello world.txt")
        self.assertEqual(entry["size_bytes"], 5)
        downloaded = self.client.get(entry["download_url"], headers=self.headers)
        self.assertEqual(downloaded.content, b"hello")
        self.assertIn("attachment", downloaded.headers["content-disposition"])
        self.assertIn("filename*=UTF-8''hello%20world.txt", downloaded.headers["content-disposition"])
        self.assertEqual(downloaded.headers["x-content-type-options"], "nosniff")

    def test_artifact_endpoints_fail_closed_without_persistent_storage(self):
        created = self.submit().json()
        self.store.transition("tenant-one", created["run_id"], RunStatus.RUNNING, "Started")
        self.store.transition("tenant-one", created["run_id"], RunStatus.COMPLETED, "Done", result={"ok": True})
        response = self.client.get(
            f"/v1/runs/{created['run_id']}/artifacts", headers=self.headers,
        )
        self.assertEqual(response.status_code, 503)

    def test_authenticated_data_deletion_requires_confirmation_and_inactive_runs(self):
        self.assertEqual(self.client.delete(
            "/v1/account/data", headers=self.headers,
        ).status_code, 400)
        unauthenticated = self.client.delete(
            "/v1/account/data", headers={"X-Confirm-Data-Deletion": "delete"},
        )
        self.assertEqual(unauthenticated.status_code, 401)

        created = self.submit("delete-me").json()
        confirm = {**self.headers, "X-Confirm-Data-Deletion": "delete"}
        blocked = self.client.delete("/v1/account/data", headers=confirm)
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("active runs", blocked.json()["detail"])

        cancelled = self.client.post(
            f"/v1/runs/{created['run_id']}/cancel", headers=self.headers,
        )
        self.assertEqual(cancelled.status_code, 200)
        self.store.create("tenant-other", RunRequest("keep-me", {"prompt": "keep"}))

        deleted = self.client.delete("/v1/account/data", headers=confirm)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json(), {"deleted_runs": 1, "deleted": True})
        self.assertEqual(self.client.get("/v1/runs", headers=self.headers).json()["runs"], [])
        self.assertEqual(len(self.store.list_recent("tenant-other")), 1)
        self.assertEqual(self.client.delete("/v1/account/data", headers=confirm).json(),
                         {"deleted_runs": 0, "deleted": True})

        with self.store._connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM monthly_usage WHERE tenant_id='tenant-one'"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM run_usage_reservations WHERE tenant_id='tenant-one'"
            ).fetchone()[0], 0)
            tenant_bucket = hashlib.sha256(b"tenant:tenant-one").hexdigest()
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM rate_limit_buckets WHERE bucket_key=?", (tenant_bucket,)
            ).fetchone()[0], 0)

    def test_database_url_selects_postgres_adapter(self):
        with patch.dict(os.environ, {"NIJI_CLOUD_DATABASE_URL": "postgresql://example/db"}):
            with patch("niji.cloud_api.PostgresRunStore") as postgres_store:
                app = create_app(api_token=TOKEN, tenant_id="tenant-one")
        postgres_store.assert_called_once_with("postgresql://example/db")
        self.assertIs(app.state.run_store, postgres_store.return_value)

    def test_api_requires_strong_secret_and_server_configured_tenant(self):
        for token, tenant in (("short", "tenant"), (TOKEN, "../tenant"), (TOKEN, "")):
            with self.subTest(token=token[:5], tenant=tenant), self.assertRaises(ValueError):
                create_app(store=self.store, api_token=token, tenant_id=tenant)


if __name__ == "__main__":
    unittest.main()
