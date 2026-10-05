import os
import pty
import select
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from io import StringIO

from niji.chat_prompt import (_fields, _prompt_lines, _strip_ansi,
                              _write_prompt_frame, reset_chat_layout)


class DummyAgent:
    model = "deepseek-v4-pro"
    provider_name = "groq"
    started_at = time.monotonic() - 4
    request_seconds = 0
    usage = {"prompt_tokens": 1234, "completion_tokens": 321}
    messages = [{"role": "user"}]
    tool_usage = {"read_file": 3, "bash": 2}


class ChatPromptTests(unittest.TestCase):
    def setUp(self):
        self.agent = DummyAgent()
        self.provider = {"provider": "groq", "model": "deepseek-v4-pro"}

    def test_reset_chat_layout_restores_terminal_modes_and_clears_visible_screen(self):
        class TTYBuffer(StringIO):
            def isatty(self):
                return True

        output = TTYBuffer()
        with patch("niji.chat_prompt.sys.stdout", output):
            reset_chat_layout()
        rendered = output.getvalue()
        self.assertIn("\x1b[r", rendered)       # reset scroll margins
        self.assertIn("\x1b[?2004l", rendered)  # disable bracketed paste
        self.assertIn("\x1b[?25h", rendered)   # show cursor
        self.assertIn("\x1b[?7h", rendered)    # restore autowrap
        self.assertIn("\x1b[2J\x1b[H", rendered)  # clear screen, top-left
        self.assertNotIn("\x1b[3J", rendered)  # preserve scrollback

    def test_footer_contains_screenshot_fields_and_real_session_values(self):
        rows, _, _ = _prompt_lines(self.agent, self.provider, "", 0, 160, enabled=False)
        rendered = "\n".join(rows)
        for expected in ("MODEL", "deepseek-v4-pro", "PROVIDER", "groq", "CONTEXT",
                         "AGENT", "Niji-Agent", "RUNTIME", "Python", "TOKENS",
                         "1,555", "TOOLS", "5", "TIME"):
            self.assertIn(expected, rendered)

    def test_composer_uses_absolute_redraw_and_reserves_bottom_scrolling_panel(self):
        output = StringIO()
        with patch("niji.chat_prompt.sys.stdout", output), \
             patch("niji.chat_prompt.shutil.get_terminal_size", return_value=os.terminal_size((80, 24))):
            status_count, content_bottom, panel_top = _write_prompt_frame(
                self.agent, self.provider, "", 0, 80, enabled=False)
            _write_prompt_frame(self.agent, self.provider, "hello", 5, 80,
                                enabled=False, initial=False)
        rendered = output.getvalue()
        self.assertEqual(panel_top, 24 - (status_count + 5) + 1)
        self.assertEqual(content_bottom, panel_top - 1)
        self.assertIn(f"\x1b[1;{content_bottom}r", rendered)
        self.assertIn(f"\x1b[{panel_top};1H\x1b[2K", rendered)
        # Placeholder caret begins before the word Ask; typed caret advances by its text width.
        self.assertIn(f"\x1b[{panel_top + 1};5H", rendered)
        self.assertTrue(rendered.endswith(f"\x1b[{panel_top + 1};10H"))
        self.assertNotIn("\x1b[1A", rendered)

    def test_context_field_is_estimated_from_messages_not_cumulative_usage(self):
        self.agent.messages = [{"role": "user", "content": "x" * 4000}]
        fields = dict(_fields(self.agent, self.provider))
        self.assertEqual(fields["CONTEXT"], "~1,000 tok")
        self.assertEqual(fields["TOKENS"], "1,555")

    def test_brand_input_and_footer_fit_narrow_and_wide_terminals(self):
        for width in (48, 56, 80, 120, 180):
            with self.subTest(width=width):
                rows, cursor, _ = _prompt_lines(self.agent, self.provider, "hello", 5, width, enabled=False)
                self.assertEqual(cursor, 8)
                self.assertTrue(all(len(line) <= width - 1 for line in rows))
                self.assertIn("NIJI", rows[0])
                self.assertTrue(any("deepseek-v4-pro" in line for line in rows))

    def _run_pty_prompt(self, chunks, expected):
        pid, fd = pty.fork()
        if pid == 0:
            root = Path(__file__).resolve().parents[1]
            sys.path.insert(0, str(root / "src"))
            import termios
            original_attrs = termios.tcgetattr(sys.stdin.fileno())
            from niji.chat_prompt import read_chat_prompt
            class Agent:
                started_at = time.monotonic()
                usage = {"prompt_tokens": 4, "completion_tokens": 2}
                messages = []
                tool_usage = {"bash": 1}
            value = read_chat_prompt(Agent(), {"provider": "groq", "model": "test-model"})
            print("RESULT=" + repr(value), flush=True)
            restored = termios.tcgetattr(sys.stdin.fileno()) == original_attrs
            print("TERMIOS_RESTORED=" + str(restored), flush=True)
            os._exit(0)

        output = bytearray()
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and b"SESSION DETAILS" not in output:
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    output.extend(os.read(fd, 4096))
            self.assertIn(b"SESSION DETAILS", output)
            for chunk in chunks:
                os.write(fd, chunk)
                time.sleep(0.15)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and (
                    b"RESULT=" not in output or (b"TERMIOS_RESTORED=True" not in output
                                               and b"TERMIOS_RESTORED=False" not in output)):
                ready, _, _ = select.select([fd], [], [], 0.2)
                if ready:
                    try:
                        output.extend(os.read(fd, 4096))
                    except OSError:
                        break
            self.assertIn(expected.encode(), output)
            self.assertIn(b"MODEL", output)
            self.assertIn(b"TERMIOS_RESTORED=True", output)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass

    def test_interactive_composer_accepts_typing_and_backspace_in_a_pty(self):
        self._run_pty_prompt([b"hellx", b"\x7f", b"o\r"], "RESULT='hello'")

    def test_interactive_composer_handles_bracketed_clipboard_paste(self):
        self._run_pty_prompt([b"\x1b[200~hello world\x1b[201~", b"\r"], "RESULT='hello world'")

    def test_cursor_editing_inserts_at_middle_of_message(self):
        self._run_pty_prompt([b"helo", b"\x1b[D", b"l\r"], "RESULT='hello'")

    def test_backspace_removes_previous_character_after_cursor_move(self):
        self._run_pty_prompt([b"hello", b"\x1b[D", b"\x7f\r"], "RESULT='helo'")

    def test_delete_removes_next_character_after_cursor_move(self):
        self._run_pty_prompt([b"hello", b"\x1b[D", b"\x1b[3~\r"], "RESULT='hell'")

    def test_backspace_removes_whole_emoji_cluster(self):
        self._run_pty_prompt(["A😀B".encode(), b"\x1b[D", b"\x7f\r"], "RESULT='AB'")

    def test_control_d_exits_cleanly_from_empty_prompt(self):
        self._run_pty_prompt([b"\x04"], "RESULT=None")


if __name__ == "__main__":
    unittest.main()
