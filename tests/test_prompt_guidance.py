import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT_SOURCE = ROOT / "src" / "niji" / "agent.py"
TOOLS_SOURCE = ROOT / "src" / "niji" / "tools" / "__init__.py"


def _system_prompt():
    tree = ast.parse(AGENT_SOURCE.read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "SYSTEM_PROMPT"
                for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("SYSTEM_PROMPT assignment not found")


class BenignRequestGuidanceTests(unittest.TestCase):
    def test_hinglish_and_benign_requests_are_not_generically_refused(self):
        prompt = _system_prompt().lower()
        self.assertIn("simple task", prompt)
        self.assertIn("ordinary, allowed requests", prompt)
        self.assertIn("hinglish", prompt)
        self.assertIn("ask only when ambiguity materially changes", prompt)

    def test_task_plans_list_prerequisites_before_dependents(self):
        prompt = _system_prompt().lower()
        tools = TOOLS_SOURCE.read_text().lower()
        self.assertIn("list every prerequisite before its dependent step", prompt)
        self.assertIn("list each prerequisite before the dependent step", tools)

    def test_current_github_trending_lookup_uses_public_web_source(self):
        prompt = _system_prompt().lower()
        tools = TOOLS_SOURCE.read_text()
        self.assertIn("github trending", prompt)
        self.assertIn("web_fetch", prompt)
        self.assertIn("never imply a live check", prompt)
        self.assertIn('_schema("web_fetch"', tools)


if __name__ == "__main__":
    unittest.main()
