import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from niji.cli import _list_sessions


class SessionSearchTests(unittest.TestCase):
    def test_search_matches_user_messages_and_skips_other_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "session-a.json").write_text(json.dumps([
                {"role": "user", "content": "help me fix terminal input"},
                {"role": "assistant", "content": "done"},
                {"role": "user", "content": "also review models"},
            ]))
            (root / "session-b.json").write_text(json.dumps([
                {"role": "user", "content": "tell me a joke"},
            ]))
            output = []
            with patch("niji.cli.SESSION_DIR", root), patch("builtins.print", side_effect=output.append):
                _list_sessions("models")
            text = "\n".join(str(line) for line in output)
            self.assertIn("session-a", text)
            self.assertNotIn("session-b", text)

    def test_search_reports_no_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "session.json").write_text(json.dumps([
                {"role": "user", "content": "hello"},
            ]))
            output = []
            with patch("niji.cli.SESSION_DIR", root), patch("builtins.print", side_effect=output.append):
                _list_sessions("absent phrase")
            self.assertIn("no sessions found", "\n".join(str(x) for x in output))


if __name__ == "__main__":
    unittest.main()
