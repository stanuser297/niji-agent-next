import json
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from niji.cloud_runtime import RunRequest, RunStatus, RunTimedOut
from niji.durable_run_store import SQLiteRunStore
from niji.cloud_worker import CloudRunContext, CloudWorker, NijiPromptExecutor, WorkerSettings, create_worker
from niji.agent import Agent


class FakeExecutor:
    def __init__(self, result=None):
        self.result = result if result is not None else {"text": "ok"}
        self.callback = None
        self.artifact_callback = None

    def set_activity_callback(self, callback):
        self.callback = callback

    def set_artifact_callback(self, callback):
        self.artifact_callback = callback

    def __call__(self, request, context):
        context.check_cancelled()
        if self.callback:
            self.callback({"level": "THINKING", "message": "private request content"})
            self.callback({"level": "TOOL", "message": "private filesystem path"})
        if self.artifact_callback:
            self.artifact_callback("out.txt", b"output", "text/plain")
        return self.result


class CloudWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SQLiteRunStore(os.path.join(self.temp.name, "runs.sqlite3"))
        self.tenant = "worker-test"

    def tearDown(self):
        self.temp.cleanup()

    def test_worker_executes_and_persists_success_with_sanitized_progress(self):
        created, _ = self.store.create(self.tenant, RunRequest("success", {"prompt": "hello"}))
        worker = CloudWorker(self.store, self.tenant, FakeExecutor(), lease_seconds=10)
        self.assertTrue(worker.run_once())
        result = self.store.get(self.tenant, created.run_id)
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertEqual(result.result, {"text": "ok"})
        details = [event.detail for event in result.events]
        self.assertIn("Agent is thinking", details)
        self.assertIn("Agent is working", details)
        self.assertNotIn("private request content", " ".join(details))
        self.assertNotIn("private filesystem path", " ".join(details))

    def test_worker_wires_artifact_sink_with_run_and_lease_scope(self):
        created, _ = self.store.create(self.tenant, RunRequest("artifact-wire", {"prompt": "hello"}))
        saved = []
        self.store.add_artifact = lambda *args: saved.append(args)
        worker = CloudWorker(self.store, self.tenant, FakeExecutor(), lease_seconds=10)
        self.assertTrue(worker.run_once())
        self.assertEqual(len(saved), 1)
        tenant, run_id, lease_token, path, content, content_type = saved[0]
        self.assertEqual((tenant, run_id, path, content, content_type),
                         (self.tenant, created.run_id, "out.txt", b"output", "text/plain"))
        self.assertTrue(lease_token)

    def test_worker_cancels_execution_when_lease_renewal_fails(self):
        created, _ = self.store.create(self.tenant, RunRequest("lease-db-failure", {"prompt": "hello"}))
        observed_cancellation = []

        def fail_renewal(*_args, **_kwargs):
            raise RuntimeError("database connection string must not be logged")

        def wait_for_cancellation(_request, context):
            while not context.cancellation_requested:
                time.sleep(0.01)
            observed_cancellation.append(True)
            context.check_cancelled()

        self.store.renew_lease = fail_renewal
        worker = CloudWorker(self.store, self.tenant, wait_for_cancellation, lease_seconds=3)
        self.assertTrue(worker.run_once())
        result = self.store.get(self.tenant, created.run_id)
        self.assertEqual(result.status, RunStatus.CANCELLED)
        self.assertEqual(observed_cancellation, [True])
        self.assertNotIn("connection string", " ".join(event.detail for event in result.events))

    def test_timeout_keeps_lease_until_blocked_executor_exits(self):
        created, _ = self.store.create(
            self.tenant, RunRequest("timeout-inflight", {"prompt": "hello"}, timeout_seconds=0.1),
        )
        entered, release = threading.Event(), threading.Event()

        def slow_executor(_request, context):
            entered.set()
            release.wait(timeout=5)
            context.check_cancelled()
            return {"text": "unexpected"}

        worker = CloudWorker(self.store, self.tenant, slow_executor, lease_seconds=2)
        thread = threading.Thread(target=worker.run_once)
        thread.start()
        self.assertTrue(entered.wait(timeout=1))
        time.sleep(2.3)  # Longer than the original lease; heartbeat must retain ownership.
        competing_worker = CloudWorker(self.store, self.tenant, FakeExecutor(), lease_seconds=2)
        self.assertFalse(competing_worker.run_once())
        release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.store.get(self.tenant, created.run_id).status, RunStatus.TIMED_OUT)

    def test_api_cancellation_keeps_lease_until_blocked_executor_exits(self):
        created, _ = self.store.create(
            self.tenant, RunRequest("cancel-inflight", {"prompt": "hello"}),
        )
        entered, release = threading.Event(), threading.Event()

        def slow_executor(_request, context):
            entered.set()
            release.wait(timeout=5)
            context.check_cancelled()
            return {"text": "unexpected"}

        worker = CloudWorker(self.store, self.tenant, slow_executor, lease_seconds=2)
        thread = threading.Thread(target=worker.run_once)
        thread.start()
        self.assertTrue(entered.wait(timeout=1))
        self.store.transition(
            self.tenant, created.run_id, RunStatus.CANCELLING, "Cancellation requested",
        )
        time.sleep(2.3)  # Longer than the original lease; cancellation must not permit a duplicate.
        competing_worker = CloudWorker(self.store, self.tenant, FakeExecutor(), lease_seconds=2)
        self.assertFalse(competing_worker.run_once())
        release.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.store.get(self.tenant, created.run_id).status, RunStatus.CANCELLED)

    def test_large_text_result_is_truncated_before_durable_finalize(self):
        created, _ = self.store.create(
            self.tenant, RunRequest("large-result", {"prompt": "hello"}),
        )
        worker = CloudWorker(
            self.store, self.tenant, FakeExecutor({"text": "x" * 300_000}), lease_seconds=10,
        )
        self.assertTrue(worker.run_once())
        result = self.store.get(self.tenant, created.run_id)
        self.assertEqual(result.status, RunStatus.COMPLETED)
        self.assertIn("response truncated", result.result["text"])
        self.assertLessEqual(len(json.dumps(result.result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")), 250_000)

    def test_worker_failure_does_not_publish_exception_text(self):
        created, _ = self.store.create(self.tenant, RunRequest("failure", {"prompt": "hello"}))
        def fail(_request, _context):
            raise RuntimeError("secret prompt or API token")
        worker = CloudWorker(self.store, self.tenant, fail, lease_seconds=10)
        self.assertTrue(worker.run_once())
        result = self.store.get(self.tenant, created.run_id)
        self.assertEqual(result.status, RunStatus.FAILED)
        self.assertEqual(result.error, "Execution failed (RuntimeError)")
        self.assertNotIn("secret", result.error)

    def test_multi_tenant_worker_executes_and_finishes_under_claim_owner(self):
        a, _ = self.store.create("user-a", RunRequest("multi-a", {"prompt": "A"}))
        b, _ = self.store.create("user-b", RunRequest("multi-b", {"prompt": "B"}))
        worker = CloudWorker(self.store, None, FakeExecutor(), lease_seconds=10)
        self.assertTrue(worker.run_once())
        self.assertTrue(worker.run_once())
        self.assertEqual(self.store.get("user-a", a.run_id).status, RunStatus.COMPLETED)
        self.assertEqual(self.store.get("user-b", b.run_id).status, RunStatus.COMPLETED)

    def test_no_queued_run_returns_false(self):
        worker = CloudWorker(self.store, self.tenant, FakeExecutor(), poll_seconds=0.2)
        self.assertFalse(worker.run_once())

    def test_cloud_agent_has_no_tools_and_ignores_host_memory_or_sessions(self):
        with tempfile.TemporaryDirectory() as workspace:
            with patch("niji.agent.OpenAI"), \
                    patch("niji.agent.load_config") as load_config, \
                    patch("niji.agent.load_plan") as load_plan, \
                    patch("niji.agent.save_plan") as save_plan, \
                    patch("niji.agent.load_project_guidance", return_value=""), \
                    patch("niji.agent.discover_skills", return_value=[]):
                agent = Agent(
                    {"provider": "openai", "base_url": "https://example.test/v1",
                     "model": "test-model", "api_key": "private"},
                    allowed_tools=[], workspace=workspace, cloud_mode=True, verbose=False,
                )
                self.assertEqual(agent.tool_schemas, [])
                self.assertEqual(agent.todos["items"], [])
                agent._save_session()
                load_config.assert_not_called()
                load_plan.assert_not_called()
                save_plan.assert_not_called()

    def test_prompt_executor_routes_only_allowlisted_selected_model(self):
        selected_choice = "nemotron-3-super-120b"
        selected_api_model = "nvidia/nemotron-3-super-120b-a12b"
        received = []

        class StubAgent:
            usage = {"prompt_tokens": 1, "completion_tokens": 1}
            usage_reported = False
            usage_complete = False

            def __init__(self, config, **_kwargs):
                received.append(config)
                self.activity_callback = None

            def cancel(self):
                pass

            def chat(self, _prompt):
                return "OK"

        settings = WorkerSettings(
            "postgresql://unused", "tenant", "nvidia", "https://example.test/v1",
            "nvidia/nemotron-3.5-lightning-30b-a3b", "private",
        )
        executor = NijiPromptExecutor(settings)
        request = RunRequest("selected-model", {"prompt": "hi", "model": selected_choice})
        with patch("niji.agent.Agent", StubAgent):
            self.assertEqual(executor(request, CloudRunContext(time.monotonic() + 60)), {"text": "OK"})
        self.assertEqual(received[0]["model"], selected_api_model)
        invalid = RunRequest("invalid-provider-model", {"prompt": "hi", "model": "nemotron-3.5-lightning"})
        with self.assertRaisesRegex(ValueError, "not available"):
            NijiPromptExecutor(WorkerSettings(
                "postgresql://unused", "tenant", "openai", "https://example.test/v1", "gpt-5", "private",
            ))(invalid, CloudRunContext(time.monotonic() + 60))

    def test_worker_settings_require_private_provider_secret_and_postgres(self):
        env = {
            "NIJI_CLOUD_DATABASE_URL": "postgresql://example",
            "NIJI_CLOUD_TENANT_ID": "primary",
            "NIJI_CLOUD_PROVIDER_API_KEY": "secret",
        }
        with patch.dict(os.environ, env, clear=True):
            settings = WorkerSettings.from_env()
            self.assertEqual(settings.provider, "openai")
            self.assertEqual(settings.model, "gpt-5")
            self.assertEqual(settings.artifact_retention_days, 7)
            with patch.dict(os.environ, env | {"NIJI_CLOUD_WORKER_TENANT_MODE": "all"}, clear=True):
                self.assertIsNone(WorkerSettings.from_env().tenant_id)
            with patch.dict(os.environ, {"NIJI_CLOUD_ARTIFACT_RETENTION_DAYS": "12"}):
                self.assertEqual(WorkerSettings.from_env().artifact_retention_days, 12)
            with patch.dict(os.environ, {"NIJI_CLOUD_ARTIFACT_RETENTION_DAYS": "0"}):
                with self.assertRaisesRegex(ValueError, "ARTIFACT_RETENTION_DAYS"):
                    WorkerSettings.from_env()
            with patch.dict(os.environ, {"NIJI_CLOUD_PROVIDER_API_KEY": ""}):
                with self.assertRaisesRegex(ValueError, "private worker secret"):
                    WorkerSettings.from_env()
        with patch.dict(os.environ, {"NIJI_CLOUD_TENANT_ID": "primary"}, clear=True):
            with self.assertRaisesRegex(ValueError, "DATABASE_URL"):
                WorkerSettings.from_env()
        with patch.dict(os.environ, env | {"NIJI_CLOUD_EXECUTION_MODE": "sandbox",
                                           "E2B_API_KEY": "sandbox-secret"}, clear=True):
            settings = WorkerSettings.from_env()
            self.assertEqual(settings.execution_mode, "sandbox")
        with patch.dict(os.environ, env | {"NIJI_CLOUD_EXECUTION_MODE": "sandbox"}, clear=True):
            with self.assertRaisesRegex(ValueError, "E2B_API_KEY"):
                WorkerSettings.from_env()
        with patch.dict(os.environ, env | {"NIJI_CLOUD_EXECUTION_MODE": "unknown"}, clear=True):
            with self.assertRaisesRegex(ValueError, "prompt.*sandbox"):
                WorkerSettings.from_env()

    def test_create_worker_passes_artifact_retention_setting(self):
        settings = WorkerSettings(
            "postgresql://unused", "tenant", "openai", "https://example.test/v1",
            "test-model", "secret", artifact_retention_days=12,
        )
        with patch("niji.cloud_worker.PostgresRunStore"):
            worker = create_worker(settings)
        self.assertEqual(worker.artifact_retention_days, 12)

    def test_context_times_out_and_cancels_agent_callback(self):
        called = []
        context = CloudRunContext(time.monotonic() - 1)
        context.set_cancel_callback(lambda: called.append(True))
        with self.assertRaises(RunTimedOut):
            context.check_cancelled()
        self.assertTrue(context.cancellation_requested)
        self.assertTrue(called)


if __name__ == "__main__":
    unittest.main()
