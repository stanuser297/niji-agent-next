import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# Let this focused suite run in minimal environments too; when OpenAI is
# installed, keep the real package. Agent captures OpenAI at import time.
_fake_openai = types.ModuleType("openai")
_fake_openai.OpenAI = lambda **options: types.SimpleNamespace(options=options)
sys.modules.setdefault("openai", _fake_openai)

from niji.instructions import discover_skills, load_project_guidance, read_skill
from niji.tools.builtin import bash, list_files, read_file, write_file
from niji.tools.subprocess_runner import run_process
from niji.agent import Agent
from niji.tools.stateful import task as spawn_task


def make_agent(**kwargs):
    fake_client = lambda **options: types.SimpleNamespace(options=options)
    with patch("niji.agent.OpenAI", side_effect=fake_client):
        return Agent({"provider": "test", "model": "m", "api_key": "k",
                      "base_url": "https://example.test/v1"}, verbose=False, **kwargs)


class InstructionAndSkillTests(unittest.TestCase):
    def test_loads_common_instruction_files_from_project_root_to_nested_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            nested = root / "src" / "app"
            nested.mkdir(parents=True)
            (root / "AGENTS.md").write_text("Run the focused tests.")
            (nested / "CLAUDE.md").write_text("Follow the module naming convention.")
            guidance = load_project_guidance(nested)
            self.assertIn("AGENTS.md", guidance)
            self.assertIn("CLAUDE.md", guidance)
            self.assertIn("untrusted project context", guidance)
            self.assertIn("module naming convention", guidance)

    def test_discovers_and_reads_named_skill_without_accepting_arbitrary_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill = root / ".agents" / "skills" / "testing" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\nname: testing\ndescription: Run focused tests first.\n---\nUse the project test runner.")
            skills = discover_skills(root)
            self.assertIn("testing", skills)
            self.assertIn("focused tests", skills["testing"]["description"])
            self.assertIn("Use the project test runner", read_skill("testing", skills))
            self.assertIn("unknown skill", read_skill("../../outside", skills))

    def test_symlinked_skill_is_not_discovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside"
            outside.mkdir()
            skill = outside / "SKILL.md"
            skill.write_text("do unsafe things")
            skills_root = root / ".agents" / "skills"
            skills_root.mkdir(parents=True)
            (skills_root / "linked").symlink_to(outside, target_is_directory=True)
            self.assertNotIn("linked", discover_skills(root))

    def test_agent_exposes_skill_read_and_loads_project_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "AGENTS.md").write_text("Use pytest.")
            skill = root / ".agents" / "skills" / "testing" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_text("---\ndescription: Test safely.\n---\nRun a focused check.")
            with patch("niji.agent.Path.cwd", return_value=root):
                agent = make_agent()
            self.assertIn("Use pytest", agent.messages[0]["content"])
            self.assertIn("testing", agent.messages[0]["content"])
            self.assertIn("skill_read", [s["function"]["name"] for s in agent.tool_schemas])


class ReliabilityUpgradeTests(unittest.TestCase):
    def test_nonzero_shell_exit_is_reported_explicitly(self):
        result = bash(f"{sys.executable} -c 'import sys; print(\"failed\"); sys.exit(7)'", timeout=5)
        self.assertTrue(result.startswith("[exit code 7]"), result)
        agent = make_agent()
        self.assertTrue(agent._tool_result_failed(result))
        self.assertFalse(agent._tool_result_failed("[exit code 0]\nTests passed"))

    def test_interrupted_tool_call_batch_is_reconciled_for_provider_history(self):
        agent = make_agent()
        agent.messages.append({"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
            {"id": "call-2", "type": "function", "function": {"name": "git", "arguments": "{}"}},
        ]})
        agent._reconcile_interrupted_tool_calls()
        results = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertEqual([m["tool_call_id"] for m in results], ["call-1", "call-2"])
        self.assertTrue(all("inspect its effects" in m["content"].lower() for m in results))

    def test_workspace_switch_starts_a_fresh_project_thread(self):
        agent = make_agent()
        agent.messages.append({"role": "user", "content": "old project secret context"})
        with tempfile.TemporaryDirectory() as tmp, patch.object(agent, "_save_session"):
            root = Path(tmp)
            (root / "AGENTS.md").write_text("New project instructions")
            old_session = agent.session_id
            agent.switch_workspace(root)
            self.assertNotEqual(agent.session_id, old_session)
            self.assertNotIn("old project secret context", str(agent.messages))
            self.assertIn("New project instructions", agent.messages[0]["content"])

    def test_explore_subagent_inherits_workspace_and_read_only_tool_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = make_agent(workspace=tmp)
            child = types.SimpleNamespace(
                file_change_history=[], _file_change_lock=threading.Lock(),
                approval_callback=None, activity_callback=None,
                chat=lambda prompt: "evidence found")
            with patch("niji.agent.Agent", return_value=child) as factory:
                report = spawn_task("inspect the module", role="explore",
                                    ctx={"agent": parent, "depth": 0})
            args = factory.call_args.kwargs
            self.assertEqual(args["workspace"], Path(tmp))
            self.assertNotIn("bash", args["allowed_tools"])
            self.assertIn("read_file", args["allowed_tools"])
            self.assertIn("role=explore", report)

    def test_delegated_tool_scope_is_enforced_even_for_unadvertised_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = make_agent(allowed_tools=["read_file"], workspace=tmp)
            result = agent._execute({"name": "write_file", "args": {
                "path": "should-not-exist.txt", "content": "blocked"}})
            self.assertTrue(result.startswith("[blocked]"), result)
            self.assertFalse((Path(tmp) / "should-not-exist.txt").exists())
            self.assertNotIn("bash", [s["function"]["name"] for s in agent.tool_schemas])

    def test_relative_tool_paths_follow_active_workspace_not_process_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "guide.txt").write_text("active project file")
            agent = make_agent(workspace=root)
            self.assertIn("active project file", read_file("guide.txt", ctx={"agent": agent}))
            result = write_file("new.txt", "created in workspace", ctx={"agent": agent})
            self.assertIn("[ok]", result)
            self.assertEqual((root / "new.txt").read_text(), "created in workspace")

    @unittest.skipUnless(os.name == "posix", "process-group regression needs POSIX")
    def test_exited_shell_cannot_leave_output_reader_hanging_on_child_pipe(self):
        code = ("import subprocess,sys; subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(20)'])")
        started = time.monotonic()
        result = run_process([sys.executable, "-c", code], timeout=8)
        self.assertEqual(result[0], 0)
        self.assertLess(time.monotonic() - started, 7)

    def test_running_subprocess_cancels_and_returns_promptly(self):
        class FakeCancellation:
            def __init__(self):
                self._cancel_event = threading.Event()
                self.activity = []
            def _record_activity(self, level, message):
                self.activity.append((level, message))
        fake = FakeCancellation()
        timer = threading.Timer(0.35, fake._cancel_event.set)
        timer.start()
        started = time.monotonic()
        try:
            code, output, timed_out, cancelled = run_process(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                timeout=10, ctx={"agent": fake}, tool_name="run_tests")
        finally:
            timer.cancel()
        self.assertTrue(cancelled)
        self.assertFalse(timed_out)
        self.assertLess(time.monotonic() - started, 4)


class ListFilesDepthPerformanceTests(unittest.TestCase):
    def test_long_file_list_scan_stops_at_requested_depth(self):
        self.assertEqual(Agent._tool_progress_label("list_files"), "Listing workspace files")
        self.assertEqual(Agent._tool_progress_label("grep"), "Searching project files")

    def test_list_files_prunes_at_requested_depth_without_recursive_glob(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "visible.txt").write_text("visible")
            nested = root / "folder"
            nested.mkdir()
            (nested / "nested.txt").write_text("nested")
            deep = nested / "deep"
            deep.mkdir()
            (deep / "slow.txt").write_text("deep")
            with patch.object(Path, "rglob", side_effect=AssertionError("must not scan below depth")):
                output = list_files(str(root), depth=1)
            self.assertIn(str(root / "visible.txt"), output)
            self.assertIn(str(nested), output)
            self.assertNotIn(str(nested / "nested.txt"), output)


if __name__ == "__main__":
    unittest.main()
