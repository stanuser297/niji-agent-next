import io
import unittest
from unittest.mock import patch

from rich.console import Console

from niji.cli import _interactive_chat, _show_context


class FakeAgent:
    def __init__(self):
        self.requests = []
        self.request_seconds = 0

    def chat(self, value):
        self.requests.append(value)

    def cost_line(self):
        return "turns=1 prompt_tokens=2 completion_tokens=3"


class InteractiveChatTests(unittest.TestCase):
    def test_context_command_shows_approximate_breakdown_without_message_contents(self):
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, color_system=None)
        agent = type("Agent", (), {"messages": [
            {"role": "system", "content": "private text"},
            {"role": "user", "content": "user prompt"},
        ]})()
        with patch("niji.cli.Console", return_value=console):
            _show_context(agent)
        text = output.getvalue()
        self.assertIn("Approximation", text)
        self.assertIn("system", text)
        self.assertIn("user", text)
        self.assertNotIn("private text", text)

    def test_exit_resets_the_pinned_terminal_layout(self):
        agent = FakeAgent()
        provider = {"provider": "groq", "model": "test-model"}
        with (
            patch("niji.cli._render_home"),
            patch("niji.chat_prompt.read_chat_prompt", return_value="/exit"),
            patch("niji.chat_prompt.reset_chat_layout") as reset_layout,
        ):
            _interactive_chat(agent, provider)
        reset_layout.assert_called_once_with()
        self.assertEqual(agent.requests, [])

    def test_startup_dashboard_is_shown_before_chat_input(self):
        order = []
        agent = FakeAgent()
        provider = {"provider": "groq", "model": "test-model"}
        with (
            patch("niji.cli._render_home", side_effect=lambda *a, **k: order.append("dashboard")),
            patch("niji.chat_prompt.read_chat_prompt", side_effect=lambda *a: (order.append("prompt") or None)),
            patch("niji.chat_prompt.reset_chat_layout"),
        ):
            _interactive_chat(agent, provider)
        self.assertEqual(order, ["dashboard", "prompt"])

    def test_user_message_is_written_above_pinned_composer(self):
        output = io.StringIO()
        console = Console(file=output, force_terminal=False, color_system=None)
        agent = FakeAgent()
        provider = {"provider": "groq", "model": "test-model"}
        with (
            patch("niji.cli.Console", return_value=console),
            patch("niji.cli._render_home"),
            patch("niji.chat_prompt.read_chat_prompt", side_effect=["meri pehli line", None]),
        ):
            _interactive_chat(agent, provider)
        self.assertIn("you ❯ meri pehli line", output.getvalue())
        self.assertIn("niji ❯", output.getvalue())
        self.assertEqual(agent.requests, ["meri pehli line"])


if __name__ == "__main__":
    unittest.main()
