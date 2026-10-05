import base64
import io
import os
import tempfile
import threading
import time
import zipfile
import unittest
from pathlib import Path

from niji.cloud_runtime import (
    IdempotencyConflict,
    LocalFakeRunner,
    RunCancelled,
    RunError,
    RunRequest,
    RunStatus,
    Runner,
    validate_cloud_payload,
)


class CloudRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "workspaces"

    def tearDown(self):
        self.temp.cleanup()

    def wait_for(self, runner, run_id, *statuses, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = runner.get(run_id)
            if snapshot and snapshot.status in statuses:
                return snapshot
            time.sleep(0.005)
        self.fail(f"run {run_id} did not reach {[s.value for s in statuses]}")

    def test_local_fake_adapter_implements_provider_neutral_contract(self):
        runner = LocalFakeRunner(self.root, lambda request, context: {"ok": True})
        try:
            self.assertIsInstance(runner, Runner)
            accepted = runner.submit(RunRequest("contract-test", {"goal": "test"}))
            completed = self.wait_for(runner, accepted.run_id, RunStatus.COMPLETED)
            self.assertEqual(completed.result, {"ok": True})
        finally:
            runner.close()

    def test_each_run_gets_a_private_distinct_workspace(self):
        barrier = threading.Barrier(2)
        def handler(request, context):
            marker = context.workspace / "marker.txt"
            marker.write_text(request.payload["name"], encoding="utf-8")
            barrier.wait(timeout=1)
            return {"path": marker.name, "content": marker.read_text(encoding="utf-8")}

        runner = LocalFakeRunner(self.root, handler, max_workers=2)
        try:
            first = runner.submit(RunRequest("workspace-a", {"name": "alpha"}))
            second = runner.submit(RunRequest("workspace-b", {"name": "beta"}))
            first = self.wait_for(runner, first.run_id, RunStatus.COMPLETED)
            second = self.wait_for(runner, second.run_id, RunStatus.COMPLETED)
            first_dir, second_dir = runner.workspace_for(first.run_id), runner.workspace_for(second.run_id)
            self.assertNotEqual(first_dir, second_dir)
            self.assertEqual(first_dir.parent, second_dir.parent)
            self.assertEqual(first.result["content"], "alpha")
            self.assertEqual(second.result["content"], "beta")
            if os.name == "posix":
                self.assertEqual(first_dir.stat().st_mode & 0o777, 0o700)
                self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        finally:
            runner.close()

    def test_lifecycle_records_queued_running_and_terminal_transitions(self):
        runner = LocalFakeRunner(self.root, lambda request, context: "done")
        try:
            accepted = runner.submit(RunRequest("lifecycle", {}))
            complete = self.wait_for(runner, accepted.run_id, RunStatus.COMPLETED)
            self.assertEqual([event.status for event in complete.events], [
                RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.COMPLETED,
            ])
            self.assertLessEqual(complete.created_at, complete.updated_at)
            self.assertEqual(complete.result, "done")
            self.assertIsNone(runner.get("missing"))
        finally:
            runner.close()

    def test_cancel_is_cooperative_and_terminal_runs_stay_terminal(self):
        entered, cancelled = threading.Event(), threading.Event()
        def handler(request, context):
            entered.set()
            while not context.cancellation_requested:
                time.sleep(0.005)
            try:
                context.check_cancelled()
            except RunCancelled:
                cancelled.set()
                raise

        runner = LocalFakeRunner(self.root, handler)
        try:
            accepted = runner.submit(RunRequest("cancel-me", {}))
            self.assertTrue(entered.wait(1))
            requested = runner.cancel(accepted.run_id)
            self.assertIn(requested.status, (RunStatus.CANCELLING, RunStatus.CANCELLED))
            complete = self.wait_for(runner, accepted.run_id, RunStatus.CANCELLED)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(runner.cancel(accepted.run_id).status, RunStatus.CANCELLED)
            self.assertIn("Run cancelled", [event.detail for event in complete.events])
        finally:
            runner.close()

    def test_queued_run_can_be_cancelled_without_starting(self):
        entered = threading.Event()
        release = threading.Event()
        def handler(request, context):
            entered.set()
            release.wait(1)
            context.check_cancelled()
            return "first"

        runner = LocalFakeRunner(self.root, handler, max_workers=1)
        try:
            first = runner.submit(RunRequest("first-queued", {}))
            self.assertTrue(entered.wait(1))
            second = runner.submit(RunRequest("second-queued", {}))
            cancelled = runner.cancel(second.run_id)
            release.set()
            self.wait_for(runner, first.run_id, RunStatus.CANCELLED, RunStatus.COMPLETED)
            final = self.wait_for(runner, second.run_id, RunStatus.CANCELLED)
            self.assertEqual(final.status, RunStatus.CANCELLED)
            self.assertNotIn(RunStatus.RUNNING, [event.status for event in final.events])
        finally:
            release.set()
            runner.close()

    def test_deadline_marks_run_timed_out(self):
        runner = LocalFakeRunner(self.root, lambda request, context: time.sleep(0.03) or "late")
        try:
            accepted = runner.submit(RunRequest("timeout", {}, timeout_seconds=0.01))
            final = self.wait_for(runner, accepted.run_id, RunStatus.TIMED_OUT)
            self.assertEqual(final.error, "Run deadline reached")
            self.assertIsNone(final.result)
        finally:
            runner.close()

    def test_duplicate_requests_are_idempotent_but_key_reuse_conflicts(self):
        gate = threading.Event()
        runner = LocalFakeRunner(self.root, lambda request, context: gate.wait(0.5) or request.payload)
        try:
            original = RunRequest("idem-key", {"a": 1, "b": 2})
            first = runner.submit(original)
            same = runner.submit(RunRequest("idem-key", {"b": 2, "a": 1}))
            self.assertEqual(first.run_id, same.run_id)
            self.assertEqual(len(list(self.root.iterdir())), 1)
            with self.assertRaises(IdempotencyConflict):
                runner.submit(RunRequest("idem-key", {"a": 9}))
            gate.set()
            self.wait_for(runner, first.run_id, RunStatus.COMPLETED)
        finally:
            gate.set()
            runner.close()

    def test_request_payload_is_copied_and_input_validation_is_bounded(self):
        source = {"nested": {"value": 1}}
        request = RunRequest("payload", source)
        source["nested"]["value"] = 2
        copy = request.payload
        copy["nested"]["value"] = 3
        self.assertEqual(request.payload, {"nested": {"value": 1}})
        with self.assertRaises(ValueError):
            RunRequest("../bad", {})
        with self.assertRaises(ValueError):
            RunRequest("nan-timeout", {}, timeout_seconds=float("nan"))
        with self.assertRaises(ValueError):
            RunRequest("too-big", {"body": "x" * 1_000_001})
        with self.assertRaises(ValueError):
            RunRequest("non-json", {"bad": object()})

    def test_failure_text_does_not_leak_handler_secrets(self):
        runner = LocalFakeRunner(self.root, lambda request, context: (_ for _ in ()).throw(ValueError("token=private")))
        try:
            accepted = runner.submit(RunRequest("failure", {}))
            failed = self.wait_for(runner, accepted.run_id, RunStatus.FAILED)
            self.assertEqual(failed.error, "Execution failed (ValueError)")
            self.assertNotIn("private", str(failed))
        finally:
            runner.close()

    def test_results_must_be_json_safe_and_bounded(self):
        runner = LocalFakeRunner(self.root, lambda request, context: object())
        try:
            accepted = runner.submit(RunRequest("invalid-result", {}))
            failed = self.wait_for(runner, accepted.run_id, RunStatus.FAILED)
            self.assertIn("Execution failed (TypeError)", failed.error)
        finally:
            runner.close()

    def test_active_workspace_cannot_be_purged_and_completed_workspace_can(self):
        entered, release = threading.Event(), threading.Event()
        def handler(request, context):
            entered.set()
            release.wait(1)
            return "ok"
        runner = LocalFakeRunner(self.root, handler)
        try:
            accepted = runner.submit(RunRequest("purge", {}))
            self.assertTrue(entered.wait(1))
            with self.assertRaises(RunError):
                runner.purge(accepted.run_id)
            release.set()
            self.wait_for(runner, accepted.run_id, RunStatus.COMPLETED)
            workspace = runner.workspace_for(accepted.run_id)
            nested = workspace / "nested"
            nested.mkdir()
            (nested / "artifact.txt").write_text("safe", encoding="utf-8")
            self.assertTrue(runner.purge(accepted.run_id))
            self.assertFalse(workspace.exists())
        finally:
            release.set()
            runner.close()

    def test_oversized_result_is_rejected_without_persisting_payload(self):
        runner = LocalFakeRunner(self.root, lambda request, context: "x" * 250_001)
        try:
            accepted = runner.submit(RunRequest("large-result", {}))
            failed = self.wait_for(runner, accepted.run_id, RunStatus.FAILED)
            self.assertIn("Execution failed (ValueError)", failed.error)
            self.assertIsNone(failed.result)
        finally:
            runner.close()

    def test_symlink_workspace_root_is_rejected(self):
        if os.name != "posix":
            self.skipTest("symlink permission semantics are platform-specific")
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = Path(self.temp.name) / "linked-root"
        link.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            LocalFakeRunner(link, lambda request, context: None)

    def test_closed_runner_rejects_new_submissions(self):
        runner = LocalFakeRunner(self.root, lambda request, context: None)
        runner.close()
        with self.assertRaises(RunError):
            runner.submit(RunRequest("closed", {}))


    def test_cloud_project_payload_rejects_paths_secrets_and_size_abuse(self):
        prompt, files = validate_cloud_payload({
            "prompt": "inspect project",
            "files": [{"path": "src/main.py", "content": "print('ok')"}],
        })
        self.assertEqual(prompt, "inspect project")
        self.assertEqual(files, [{"path": "src/main.py", "content": "print('ok')"}])
        prompt, files = validate_cloud_payload({
            "prompt": "review imported source",
            "repository": {"url": "https://github.com/example/demo", "revision": "a" * 40},
        })
        self.assertEqual(prompt, "review imported source")
        self.assertEqual(files, [])

        unsafe_paths = (
            "../escape.txt", "/etc/passwd", "C:/secrets.txt",
            "src" + chr(92) + "main.py", "nul" + chr(0) + "file",
            ".env", "keys/id_rsa", "node_modules/pkg/file.py",
        )
        for path in unsafe_paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_cloud_payload({"prompt": "review", "files": [{"path": path, "content": "x"}]})

        with self.assertRaises(ValueError):
            validate_cloud_payload({"prompt": "review", "files": [{"path": "x.py", "content": "x" * 64_001}]})
        with self.assertRaises(ValueError):
            validate_cloud_payload({"prompt": "review", "files": [{"path": f"{i}.txt", "content": ""} for i in range(101)]})
        with self.assertRaises(ValueError):
            validate_cloud_payload({"prompt": "review", "files": [{"path": f"{i}.txt", "content": "x" * 60_000} for i in range(9)]})
        with self.assertRaises(ValueError):
            validate_cloud_payload({"prompt": "review", "files": [
                {"path": "A.txt", "content": "x"}, {"path": "a.TXT", "content": "y"},
            ]})

    @staticmethod
    def _zip_payload(entries):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries:
                archive.writestr(name, content)
        return base64.b64encode(buffer.getvalue()).decode("ascii")

    def test_cloud_zip_archive_becomes_bounded_text_files(self):
        encoded = self._zip_payload([
            ("project/src/main.py", "print('hi')\n"),
            ("project/README.md", "# Demo\n"),
        ])
        prompt, files = validate_cloud_payload({
            "prompt": "review this project", "archive_base64": encoded,
        })
        self.assertEqual(prompt, "review this project")
        self.assertEqual(files, [
            {"path": "project/src/main.py", "content": "print('hi')\n"},
            {"path": "project/README.md", "content": "# Demo\n"},
        ])

    def test_cloud_zip_archive_rejects_traversal_secrets_and_symlinks(self):
        import stat

        for unsafe in ("../escape.txt", "/etc/passwd", "C:/secret.txt",
                       "src\\\\main.py", ".env", "node_modules/pkg/file.py"):
            with self.subTest(path=unsafe), self.assertRaises(ValueError):
                validate_cloud_payload({
                    "prompt": "review", "archive_base64": self._zip_payload([(unsafe, "x")]),
                })

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("project/link.txt")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../../outside")
        with self.assertRaisesRegex(ValueError, "symlinks"):
            validate_cloud_payload({
                "prompt": "review", "archive_base64": base64.b64encode(buffer.getvalue()).decode(),
            })

    def test_cloud_zip_archive_rejects_binary_oversized_invalid_and_duplicate_entries(self):
        invalid_archives = (
            self._zip_payload([("image.bin", b"\\x00\\xff")]),
            self._zip_payload([("large.txt", "x" * 64_001)]),
            self._zip_payload([("A.txt", "one"), ("a.TXT", "two")]),
            self._zip_payload([("ok.txt", "ok"), (".env", "secret")]),
        )
        for encoded in invalid_archives:
            with self.subTest(encoded=encoded[:20]), self.assertRaises(ValueError):
                validate_cloud_payload({"prompt": "review", "archive_base64": encoded})
        for encoded in ("not-base64", base64.b64encode(b"not a zip").decode()):
            with self.subTest(encoded=encoded[:20]), self.assertRaises(ValueError):
                validate_cloud_payload({"prompt": "review", "archive_base64": encoded})

    def test_cloud_zip_archive_rejects_excessive_entry_count_and_empty_archives(self):
        too_many = self._zip_payload([(f"{index}.txt", "x") for index in range(101)])
        empty = self._zip_payload([])
        for encoded in (too_many, empty):
            with self.subTest(encoded=encoded[:20]), self.assertRaises(ValueError):
                validate_cloud_payload({"prompt": "review", "archive_base64": encoded})


if __name__ == "__main__":
    unittest.main()
