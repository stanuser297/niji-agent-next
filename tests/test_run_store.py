import json
import math
import os
import tempfile
import threading
import unittest
from pathlib import Path

from niji.run_store import MAX_JOB_EVENTS, RunStore, _MAX_RECORD_BYTES


class RunStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "runs"
        self.store = RunStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def job(self, run_id="run-1", status="completed", **extra):
        value = {"id": run_id, "status": status, "created": 100.0,
                 "updated": 125.0, "response": "safe result", "events": []}
        value.update(extra)
        return value

    def test_atomic_save_load_and_private_permissions(self):
        saved = self.store.save(self.job())
        loaded = self.store.load_recent()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["response"], "safe result")
        self.assertEqual(loaded[0]["updated"], saved["updated"])
        if os.name == "posix":
            self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((self.root / "run-1.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(self.root.glob(".run-*.tmp")), [])

    def test_existing_world_readable_record_is_hardened_before_loading(self):
        path = self.root / "legacy.json"
        path.write_text(json.dumps(self.job("legacy", response="private")), encoding="utf-8")
        if os.name == "posix":
            path.chmod(0o644)
            records = self.store.load_recent()
            self.assertEqual(records[0]["response"], "private")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        else:
            self.assertEqual(self.store.load_recent()[0]["response"], "private")

    def test_active_jobs_become_interrupted_without_replay(self):
        for index, status in enumerate(("running", "pause_requested", "paused")):
            self.store.save(self.job(f"active-{index}", status=status,
                                     cancel_requested=True, original_message="write a file"))
        recovered = self.store.load_recent()
        self.assertEqual({item["status"] for item in recovered}, {"interrupted"})
        for item in recovered:
            self.assertFalse(item["cancel_requested"])
            self.assertIn("No action was replayed", item["progress_detail"])
            self.assertEqual(item["original_message"], "write a file")
            self.assertIn("INTERRUPTED", [event["level"] for event in item["events"]])

    def test_terminal_statuses_and_updated_timestamps_are_preserved(self):
        saved_timestamps = {}
        for index, status in enumerate(("completed", "cancelled", "error")):
            saved = self.store.save(self.job(f"terminal-{index}", status=status, updated=125.5))
            saved_timestamps[saved["id"]] = saved["updated"]
        recovered = self.store.load_recent()
        by_id = {item["id"]: item for item in recovered}
        for index, status in enumerate(("completed", "cancelled", "error")):
            run_id = f"terminal-{index}"
            self.assertEqual(by_id[run_id]["status"], status)
            self.assertEqual(by_id[run_id]["updated"], saved_timestamps[run_id])

    def test_rejects_invalid_identifiers_and_symlinks(self):
        with self.assertRaises(ValueError):
            self.store.save(self.job("../escape"))
        if os.name == "posix":
            outside = Path(self.temp.name) / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            link = self.root / "linked.json"
            link.symlink_to(outside)
            with self.assertRaises(OSError):
                self.store.save(self.job("linked"))
            self.assertEqual(outside.read_text(encoding="utf-8"), "{}")
            other = Path(self.temp.name) / "linked-dir"
            other.symlink_to(self.root, target_is_directory=True)
            with self.assertRaises(OSError):
                RunStore(other)

    def test_ignores_corrupt_oversized_and_mismatched_records(self):
        (self.root / "bad-json.json").write_text("{", encoding="utf-8")
        (self.root / "too-large.json").write_bytes(b"x" * (_MAX_RECORD_BYTES + 1))
        (self.root / "mismatch.json").write_text(json.dumps({"id": "different", "status": "completed"}), encoding="utf-8")
        self.store.save(self.job("good"))
        self.assertEqual([item["id"] for item in self.store.load_recent()], ["good"])

    def test_prunes_oldest_records_to_configured_bound(self):
        bounded = RunStore(self.root, max_records=2)
        for index in range(4):
            bounded.save(self.job(f"r{index}", updated=100 + index))
        self.assertEqual(len(list(self.root.glob("*.json"))), 2)
        self.assertEqual({item["id"] for item in bounded.load_recent()}, {"r2", "r3"})

    def test_keeps_a_bounded_but_complete_recent_action_timeline(self):
        events = [{"level": "TOOL", "message": str(index)}
                  for index in range(MAX_JOB_EVENTS + 17)]
        saved = self.store.save(self.job(events=events))
        self.assertEqual(len(saved["events"]), MAX_JOB_EVENTS)
        self.assertEqual(saved["events"][0]["message"], "17")
        self.assertEqual(saved["events"][-1]["message"], str(MAX_JOB_EVENTS + 16))

    def test_caps_unbounded_text_and_rejects_invalid_timestamps(self):
        saved = self.store.save(self.job(original_message="p" * 50_000,
                                         response="r" * 50_000,
                                         streamed="s" * 50_000,
                                         error="e" * 4_000,
                                         created=10 ** 1000,
                                         updated=float("nan")))
        self.assertLessEqual(len(saved["original_message"]), 30_000)
        self.assertLessEqual(len(saved["response"]), 40_000)
        self.assertLessEqual(len(saved["streamed"]), 40_000)
        self.assertLessEqual(len(saved["error"]), 2_000)
        self.assertTrue(math.isfinite(saved["updated"]))

    def test_concurrent_saves_leave_one_valid_record(self):
        errors = []
        def writer(index):
            try:
                self.store.save(self.job(response=f"writer-{index}"))
            except Exception as exc:  # surfaced in the assertion below
                errors.append(exc)
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        records = self.store.load_recent()
        self.assertEqual(len(records), 1)
        self.assertRegex(records[0]["response"], r"^writer-\d+$")
        self.assertEqual(list(self.root.glob(".run-*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
