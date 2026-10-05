import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from niji.planning import (extract_plan_steps, load_plan, normalize_plan, save_plan,
                           validate_approved_plan_progress)
from niji.tools.stateful import todo_read, todo_write


class PlanningTests(unittest.TestCase):
    def test_normalize_plan_bounds_text_and_assigns_stable_step_ids(self):
        plan = normalize_plan([{"content": "  Inspect files  ", "status": "in_progress"}])
        self.assertEqual(plan, [{"id": "step-1", "content": "Inspect files",
                                 "status": "in_progress", "activeForm": ""}])
        self.assertEqual(len(normalize_plan([{"content": "x" * 900}])[0]["content"]), 500)

    def test_rejects_malformed_or_ambiguous_active_steps(self):
        with self.assertRaises(ValueError):
            normalize_plan([{"content": "one", "status": "in_progress"},
                            {"content": "two", "status": "in_progress"}])
        with self.assertRaises(ValueError):
            normalize_plan([{"content": "bad status", "status": "unknown"}])
        with self.assertRaises(ValueError):
            normalize_plan([{"content": "x"}] * 61)

    def test_dependency_graph_accepts_valid_chains_and_rejects_invalid_edges(self):
        valid = normalize_plan([
            {"id": "inspect", "content": "Inspect", "status": "completed"},
            {"id": "implement", "content": "Implement", "depends_on": ["inspect"]},
            {"id": "verify", "content": "Verify", "depends_on": ["implement"]},
        ])
        self.assertEqual(valid[1]["depends_on"], ["inspect"])
        self.assertNotIn("depends_on", valid[0])
        invalid_plans = [
            [{"id": "a", "content": "A", "depends_on": ["missing"]}],
            [{"id": "a", "content": "A", "depends_on": ["a"]}],
            [{"id": "a", "content": "A"}, {"id": "a", "content": "duplicate"}],
            [{"id": "a", "content": "A", "depends_on": ["b"]},
             {"id": "b", "content": "B", "depends_on": ["a"]}],
            [{"id": "a", "content": "A", "depends_on": ["b", "b"]},
             {"id": "b", "content": "B"}],
            [{"id": "a", "content": "A", "status": "in_progress", "depends_on": ["b"]},
             {"id": "b", "content": "B"}],
            [{"id": "a", "content": "A", "status": "completed", "depends_on": ["b"]},
             {"id": "b", "content": "B"}],
            [{"id": "a/b", "content": "Invalid id"}],
            [{"id": 7, "content": "Non-string id"}],
            [{"id": "x" * 81, "content": "Overlong id"}],
            [{"id": "first", "content": "Depends later", "depends_on": ["later"]},
             {"id": "later", "content": "Later step"}],
        ]
        for plan in invalid_plans:
            with self.subTest(plan=plan), self.assertRaises(ValueError):
                normalize_plan(plan)

    def test_approved_plan_progress_enforces_exact_steps_order_and_safe_transitions(self):
        approved = normalize_plan([
            {"id": "inspect", "content": "Inspect repository",
             "acceptance_criteria": "Relevant files have been read."},
            {"id": "test", "content": "Run focused tests", "depends_on": ["inspect"]},
        ])
        pending = approved
        active_first = validate_approved_plan_progress([
            {**approved[0], "status": "in_progress"}, approved[1]], approved, pending)
        done_first_active_second = validate_approved_plan_progress([
            {**approved[0], "status": "completed",
             "evidence": "Read source and confirmed the relevant call path."},
            {**approved[1], "status": "in_progress"},
        ], approved, active_first)
        self.assertEqual([step["status"] for step in done_first_active_second],
                         ["completed", "in_progress"])
        completed = validate_approved_plan_progress([
            {**done_first_active_second[0]},
            {**approved[1], "status": "completed",
             "evidence": "Focused suite passed: 12 tests, zero failures."},
        ], approved, done_first_active_second)
        self.assertEqual(completed[1]["status"], "completed")

        invalid = [
            # Skip the required active state.
            [{**approved[0], "status": "completed"}, approved[1]],
            # Start a dependent step before its prerequisite completes.
            [approved[0], {**approved[1], "status": "in_progress"}],
            # Change approved content, dependency, or ordering.
            [{**approved[0], "content": "Different"}, approved[1]],
            [approved[0], {**approved[1], "depends_on": []}],
            [approved[1], approved[0]],
        ]
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                validate_approved_plan_progress(candidate, approved, pending)

    def test_approved_completion_requires_concrete_reported_evidence_and_locks_it(self):
        approved = normalize_plan([{"id": "verify", "content": "Run checks",
                                   "acceptance_criteria": "The focused test suite passes."}])
        active = validate_approved_plan_progress(
            [{**approved[0], "status": "in_progress"}], approved, approved)
        for evidence in ("", "done", "verified", "ok"):
            with self.subTest(evidence=evidence), self.assertRaisesRegex(ValueError, "evidence"):
                validate_approved_plan_progress(
                    [{**approved[0], "status": "completed", "evidence": evidence}],
                    approved, active)
        completed = validate_approved_plan_progress(
            [{**approved[0], "status": "completed",
              "evidence": "Focused tests passed: 9 tests, zero failures."}],
            approved, active)
        with self.assertRaisesRegex(ValueError, "cannot be changed"):
            validate_approved_plan_progress(
                [{**completed[0], "evidence": "Different evidence result."}],
                approved, completed)
        with self.assertRaisesRegex(ValueError, "criteria"):
            validate_approved_plan_progress(
                [{**approved[0], "status": "in_progress", "acceptance_criteria": "Changed"}],
                approved, approved)

    def test_step_criteria_and_evidence_are_bounded_and_shown_in_todo_read(self):
        for criteria in ("x" * 401, " " * 401):
            with self.subTest(criteria_size=len(criteria)), self.assertRaises(ValueError):
                normalize_plan([{"content": "Step", "acceptance_criteria": criteria}])
        for evidence in ("x" * 801, " " * 801):
            with self.subTest(evidence_size=len(evidence)), self.assertRaises(ValueError):
                normalize_plan([{"content": "Step", "evidence": evidence}])
        plan = normalize_plan([{"id": "check", "content": "Run checks",
                                "acceptance_criteria": "All focused tests pass.",
                                "evidence": "9 tests passed with no failures."}])
        text = todo_read({"todos": {"items": plan}})
        self.assertIn("Check: All focused tests pass.", text)
        self.assertIn("Reported evidence (agent-reported, not independently attested): 9 tests passed", text)

    def test_blocked_approved_step_can_be_reset_to_pending_for_resume(self):
        approved = normalize_plan([{"id": "work", "content": "Do work"}])
        active = validate_approved_plan_progress(
            [{**approved[0], "status": "in_progress"}], approved, approved)
        blocked = validate_approved_plan_progress(
            [{**approved[0], "status": "blocked"}], approved, active)
        resumed = validate_approved_plan_progress(
            [{**approved[0], "status": "pending"}], approved, blocked)
        self.assertEqual(resumed[0]["status"], "pending")

    def test_pending_dependent_step_is_visible_as_waiting_and_blocked(self):
        plan = normalize_plan([
            {"id": "one", "content": "First step", "status": "pending"},
            {"id": "two", "content": "Second step", "status": "pending",
             "depends_on": ["one"]},
            {"id": "three", "content": "Third step", "status": "blocked",
             "depends_on": ["two"]},
        ])
        output = todo_read({"todos": {"items": plan}})
        self.assertIn("(waiting for: one)", output)
        self.assertIn("(waiting for: two)", output)
        self.assertIn("! 3. Third step", output)

    def test_plan_round_trips_atomically_and_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = [{"id": "inspect", "content": "Inspect the workspace", "status": "completed"},
                        {"id": "implement", "content": "Implement changes", "status": "in_progress",
                         "depends_on": ["inspect"]},
                        {"id": "verify", "content": "Run checks", "status": "pending",
                         "depends_on": ["implement"]}]
            saved = save_plan("session-123", original, root=tmp)
            self.assertEqual(load_plan("session-123", root=tmp), saved)
            path = Path(tmp) / "plans" / "session-123.json"
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_load_plan_migrates_legacy_unsafe_step_ids_without_losing_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans = Path(tmp) / "plans"
            plans.mkdir()
            (plans / "legacy-session.json").write_text(json.dumps({
                "version": 1,
                "session_id": "legacy-session",
                "items": [
                    {"id": "inspect files / source", "content": "Inspect source",
                     "status": "completed", "activeForm": ""},
                    {"id": "verify tests", "content": "Verify tests",
                     "status": "pending", "activeForm": ""},
                ],
            }), encoding="utf-8")
            plan = load_plan("legacy-session", root=tmp)
        self.assertEqual([item["content"] for item in plan], ["Inspect source", "Verify tests"])
        self.assertEqual([item["status"] for item in plan], ["completed", "pending"])
        self.assertEqual([item["id"] for item in plan], ["legacy-step-1", "legacy-step-2"])

    def test_legacy_long_ids_remap_dependencies_without_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            plans = Path(tmp) / "plans"
            plans.mkdir()
            long_id = "old-step-" + "x" * 120
            (plans / "legacy-long.json").write_text(json.dumps({
                "version": 1, "session_id": "legacy-long", "items": [
                    {"id": long_id, "content": "Legacy first step", "status": "completed"},
                    {"id": "dependent", "content": "Legacy dependent step",
                     "depends_on": [long_id], "status": "pending"},
                ],
            }), encoding="utf-8")
            migrated = load_plan("legacy-long", root=tmp)
        self.assertEqual(migrated[0]["id"], "legacy-step-1")
        self.assertEqual(migrated[1]["depends_on"], ["legacy-step-1"])

    def test_load_and_save_reject_symlinked_plan_directories(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
            root = Path(tmp)
            outside_plans = Path(elsewhere) / "plans"
            outside_plans.mkdir()
            (root / "plans").symlink_to(outside_plans, target_is_directory=True)
            with self.assertRaises(OSError):
                save_plan("session-safe", [], root=root)
            self.assertEqual(load_plan("session-safe", root=root), [])

    def test_load_and_save_reject_symlinked_ancestor_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual = root / "actual"
            actual.mkdir()
            alias = root / "alias"
            alias.symlink_to(actual, target_is_directory=True)
            redirected_root = alias / "sessions"
            with self.assertRaises(OSError):
                save_plan("session-safe", [], root=redirected_root)
            self.assertEqual(load_plan("session-safe", root=redirected_root), [])
            self.assertFalse((actual / "sessions").exists())

    def test_load_repairs_permissive_legacy_plan_permissions(self):
        if os.name != "posix":
            self.skipTest("POSIX file modes are required")
        with tempfile.TemporaryDirectory() as tmp:
            plans = Path(tmp) / "plans"
            plans.mkdir(mode=0o755)
            plan_file = plans / "session-permissions.json"
            plan_file.write_text(json.dumps({
                "version": 1, "session_id": "session-permissions",
                "items": [{"id": "inspect", "content": "Inspect", "status": "pending"}],
            }), encoding="utf-8")
            plans.chmod(0o755)
            plan_file.chmod(0o644)
            result = load_plan("session-permissions", root=tmp)
            self.assertEqual(result[0]["content"], "Inspect")
            self.assertEqual(plans.stat().st_mode & 0o777, 0o700)
            self.assertEqual(plan_file.stat().st_mode & 0o777, 0o600)

    def test_load_and_save_reject_symlinked_session_directory(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
            root = Path(tmp)
            redirected = root / "redirected-session"
            redirected.symlink_to(Path(elsewhere), target_is_directory=True)
            with self.assertRaises(OSError):
                save_plan("session-safe", [], root=redirected)
            self.assertEqual(load_plan("session-safe", root=redirected), [])

    def test_rejects_path_traversal_and_ignores_corrupt_or_symlinked_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                save_plan("../outside", [], root=tmp)
            folder = Path(tmp) / "plans"
            folder.mkdir()
            target = folder / "target.json"
            target.write_text(json.dumps({"session_id": "session-x", "items": []}))
            (folder / "session-x.json").symlink_to(target)
            self.assertEqual(load_plan("session-x", root=tmp), [])
            (folder / "session-bad.json").write_text("not json")
            self.assertEqual(load_plan("session-bad", root=tmp), [])

    def test_extracts_only_numbered_steps_for_a_plan_preview(self):
        text = """Goal: polish the app\n\nProposed steps:\n1. Audit current behavior\n2. Implement and test the planner\n\nRisks:\n- Keep user changes safe\n\nVerification:\n- Run unit tests"""
        steps = extract_plan_steps(text)
        self.assertEqual([step["content"] for step in steps],
                         ["Audit current behavior", "Implement and test the planner"])
        self.assertTrue(all(step["status"] == "pending" for step in steps))

    def test_numbered_prose_is_not_mistaken_for_a_plan(self):
        self.assertEqual(extract_plan_steps(
            """My notes from the review:
1. This is a quoted item, not a proposed plan
2. Nor is this one"""), [])
        self.assertEqual([step["content"] for step in extract_plan_steps(
            """1. Inspect the project
2. Run tests""")], ["Inspect the project", "Run tests"])

    def test_todo_write_enforces_approved_plan_before_persisting_changes(self):
        approved = normalize_plan([
            {"id": "inspect", "content": "Inspect"},
            {"id": "implement", "content": "Implement", "depends_on": ["inspect"]},
        ])
        with tempfile.TemporaryDirectory() as tmp:
            agent = SimpleNamespace(session_id="approved-session", approved_plan=approved,
                                    todos={"items": approved}, plan_callback=None)
            with patch("niji.planning.SESSION_DIR", Path(tmp)):
                with self.assertRaises(ValueError):
                    todo_write([approved[0], {**approved[1], "status": "in_progress"}],
                               ctx={"agent": agent, "todos": agent.todos})
                self.assertEqual(agent.todos["items"], approved)
                self.assertEqual(load_plan("approved-session", root=tmp), [])
                active = [{**approved[0], "status": "in_progress"}, approved[1]]
                todo_write(active, "Inspecting", ctx={"agent": agent, "todos": agent.todos})
                finished_first = [{**approved[0], "status": "completed",
                                   "evidence": "Source inspection confirmed the target path."}, approved[1]]
                todo_write(finished_first, "Inspect complete", ctx={"agent": agent, "todos": agent.todos})
                with self.assertRaises(ValueError):
                    todo_write([{**approved[0], "status": "completed"},
                                {**approved[1], "status": "completed"}],
                               ctx={"agent": agent, "todos": agent.todos})
                self.assertEqual(load_plan("approved-session", root=tmp), finished_first)

    def test_todo_write_persists_and_notifies_live_plan_callback(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = SimpleNamespace(session_id="session-abc", plan_callback=None,
                                    todos={"items": []})
            notified = []
            agent.plan_callback = lambda plan, label: notified.append((plan, label))
            state = agent.todos
            with patch("niji.planning.SESSION_DIR", Path(tmp)):
                result = todo_write([{"content": "Inspect", "status": "in_progress"}],
                                    "Inspecting files", ctx={"agent": agent, "todos": state})
                self.assertTrue(result.startswith("[ok] plan saved"))
                self.assertEqual(load_plan("session-abc", root=tmp)[0]["content"], "Inspect")
            self.assertEqual(agent.todos["items"][0]["content"], "Inspect")
            self.assertEqual(notified[0][1], "Inspecting files")


if __name__ == "__main__":
    unittest.main()

