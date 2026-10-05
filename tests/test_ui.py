import io
import time
import unittest

from rich.console import Console

from niji.ui import render_home


class DummyAgent:
    session_id = "20260930-example-session-123456"
    started_at = time.monotonic() - 75
    approval = "ask"
    mcp_clients = [object()]
    todos = {"items": []}
    usage = {"turns": 2, "prompt_tokens": 120, "completion_tokens": 45}
    tool_usage = {"read_file": 2, "bash": 1}
    activity = [{"time": "14:36:17", "level": "TOOL", "message": "Tool call: read_file"}]
    tool_schemas = [
        {"function": {"name": "read_file", "description": "Read a text file with line numbers."}},
        {"function": {"name": "edit_file", "description": "Update an existing file."}},
        {"function": {"name": "bash", "description": "Run a shell command."}},
        {"function": {"name": "web_fetch", "description": "Fetch a web page."}},
        {"function": {"name": "todo_write", "description": "Plan and track tasks."}},
    ]


class DashboardTests(unittest.TestCase):
    def render(self, width):
        output = io.StringIO()
        console = Console(file=output, width=width, color_system=None, force_terminal=False)
        render_home(DummyAgent(), {
            "provider": "nvidia", "model": "z-ai/glm-5.3-flash"
        }, console=console)
        return output.getvalue()

    def test_dashboard_shows_real_agent_and_capability_details(self):
        output = self.render(120)
        for expected in ("Niji-Agent", "AGENT PROFILE", "AGENT OVERVIEW",
                         "z-ai/glm-5.3-flash", "NVIDIA".lower(), "AVAILABLE TOOLS",
                         "TOOL USAGE", "SYSTEM STATUS", "RECENT ACTIVITY",
                         "QUICK COMMANDS", "Workspace & Files", "Tool call: read_file"):
            self.assertIn(expected.lower(), output.lower())
        self.assertNotIn("Skills Loaded", output)

    def test_narrow_terminal_stacks_panels_and_avoids_horizontal_overflow(self):
        output = self.render(56)
        self.assertIn("Niji-Agent", output)
        self.assertIn("AGENT PROFILE", output)
        self.assertIn("QUICK COMMANDS", output)
        self.assertTrue(all(len(line) <= 56 for line in output.splitlines()),
                        "dashboard overflowed the narrow terminal")

    def test_dashboard_renders_tool_usage_counts(self):
        output = self.render(120)
        self.assertIn("Total calls", output)
        self.assertIn("3", output)

    def test_quiet_mode_renders_nothing(self):
        output = io.StringIO()
        console = Console(file=output, width=72, color_system=None)
        render_home(DummyAgent(), {"provider": "test", "model": "test-model"},
                    quiet=True, console=console)
        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
