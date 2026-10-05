import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from niji.agent import Agent
from niji.tools import dispatch


class FileUndoTests(unittest.TestCase):
    def make_agent(self):
        with patch("niji.agent.OpenAI", return_value=object()):
            return Agent({"provider": "test", "model": "test-model", "api_key": "x",
                          "base_url": "https://example.test/v1"}, verbose=False)

    def test_write_file_can_restore_exact_previous_contents(self):
        agent = self.make_agent()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "notes.txt"
            target.write_bytes(b"old\r\ncontents\n")
            result = dispatch("write_file", {"path": str(target), "content": "new"},
                              {"agent": agent})
            self.assertIn("undo checkpoint", result)
            self.assertEqual(target.read_text(), "new")
            restored = agent.undo_last_file_change()
            self.assertTrue(restored["ok"])
            self.assertEqual(target.read_bytes(), b"old\r\ncontents\n")

    def test_undo_removes_file_created_by_agent(self):
        agent = self.make_agent()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "new.txt"
            dispatch("write_file", {"path": str(target), "content": "created"},
                     {"agent": agent})
            self.assertTrue(target.exists())
            result = agent.undo_last_file_change()
            self.assertTrue(result["ok"])
            self.assertFalse(target.exists())

    def test_undo_refuses_to_overwrite_newer_external_changes(self):
        agent = self.make_agent()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "notes.txt"
            target.write_text("before")
            dispatch("edit_file", {"path": str(target), "old_text": "before",
                                    "new_text": "after"}, {"agent": agent})
            target.write_text("newer human change")
            result = agent.undo_last_file_change()
            self.assertFalse(result["ok"])
            self.assertIn("refusing to overwrite", result["message"])
            self.assertEqual(target.read_text(), "newer human change")
            self.assertIsNotNone(agent.latest_file_change())

    def test_session_tool_policy_can_block_or_allow_an_action(self):
        agent = self.make_agent()
        agent.approval = "ask"
        agent.approval_callback = lambda *args: (_ for _ in ()).throw(AssertionError("approval should be bypassed"))
        call = {"name": "write_file", "args": {"path": "/tmp/niji-policy-test.txt", "content": "ok"}}
        agent.tool_policies["write_file"] = "block"
        blocked = agent._execute(call)
        self.assertIn("blocked by session tool policy", blocked)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "allowed.txt"
            call["args"]["path"] = str(target)
            agent.tool_policies["write_file"] = "allow"
            result = agent._execute(call)
            self.assertTrue(target.exists())
            self.assertIn("ok", result)

    def test_edit_tool_exposes_undo_checkpoint_to_user(self):
        agent = self.make_agent()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "notes.txt"
            target.write_text("a unique phrase")
            result = dispatch("edit_file", {"path": str(target), "old_text": "unique",
                                    "new_text": "changed"}, {"agent": agent})
            self.assertIn("undo checkpoint", result)
            self.assertEqual(agent.latest_file_change()["operation"], "edit_file")


if __name__ == "__main__":
    unittest.main()
