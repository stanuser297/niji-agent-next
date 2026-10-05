import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from niji.agent import Agent


class ApprovedPlanExecutionTests(unittest.TestCase):
    @staticmethod
    def _bare_agent():
        agent = object.__new__(Agent)
        agent.approved_plan = [{"id": "inspect", "content": "Inspect", "status": "pending",
                               "activeForm": ""}]
        agent.todos = {"items": list(agent.approved_plan)}
        agent.session_id = "approved-session"
        agent.plan_callback = None
        agent._activity_lock = threading.Lock()
        agent.tool_usage = {}
        agent.mcp_clients = []
        agent.depth = 0
        agent.allowed_tools = None
        agent._cancel_event = threading.Event()
        agent._pause_requested = threading.Event()
        agent._resume_gate = threading.Event()
        agent._resume_gate.set()
        agent._pause_control_lock = threading.Lock()
        agent._inflight_model_calls = 0
        agent._inflight_tool_actions = 0
        agent._record_activity = lambda *args, **kwargs: None
        agent.approval = "auto"
        agent.tool_policies = {}
        agent.verbose = False
        agent._request_tool_calls = 0
        agent.max_tool_calls = 10
        agent.max_tool_calls_per_turn = 10
        agent.max_turns = 3
        agent.auto_compact = False
        agent.messages = []
        agent.usage = {"turns": 0, "prompt_tokens": 0, "completion_tokens": 0}
        return agent

    def test_non_checklist_tools_are_blocked_until_an_approved_step_is_active(self):
        agent = self._bare_agent()
        call = {"id": "call-1", "name": "read_file", "args": {"path": "README.md"}}
        with patch("niji.agent.dispatch") as dispatch:
            result = agent._execute(call)
        self.assertIn("start exactly one approved plan step", result)
        dispatch.assert_not_called()

    def test_todo_write_transition_validation_runs_through_agent_dispatch(self):
        approved = [{"id": "inspect", "content": "Inspect", "status": "pending",
                     "activeForm": ""}]
        agent = self._bare_agent()
        agent.approved_plan = approved
        with tempfile.TemporaryDirectory() as tmp, patch("niji.planning.SESSION_DIR", Path(tmp)):
            invalid = agent._execute({
                "id": "todo-invalid", "name": "todo_write", "args": {
                    "todos": [{**approved[0], "status": "completed"}],
                },
            })
            self.assertTrue(invalid.startswith("[error]"))
            self.assertEqual(agent.todos["items"], approved)
            started = agent._execute({
                "id": "todo-start", "name": "todo_write", "args": {
                    "todos": [{**approved[0], "status": "in_progress"}],
                },
            })
            self.assertTrue(started.startswith("[ok]"))
            self.assertEqual(agent.todos["items"][0]["status"], "in_progress")

    def test_coder_subagents_are_blocked_during_approved_plan_execution(self):
        agent = self._bare_agent()
        agent.todos["items"][0]["status"] = "in_progress"
        with patch("niji.agent.dispatch") as dispatch:
            result = agent._execute({"id": "task-1", "name": "task", "args": {"prompt": "x"}})
        self.assertIn("subagent execution is not available", result)
        dispatch.assert_not_called()

    def test_approved_plan_disables_parallel_read_only_tool_batches(self):
        agent = self._bare_agent()
        agent.todos = {"items": [{"id": "inspect", "content": "Inspect", "status": "in_progress",
                                  "activeForm": ""}]}
        calls = [
            {"id": "call-1", "name": "read_file", "args": {}},
            {"id": "call-2", "name": "grep", "args": {}},
        ]
        responses = iter([
            ({"role": "assistant", "content": "", "tool_calls": []}, "", calls),
            ({"role": "assistant", "content": "Verified."}, "Verified.", []),
        ])
        agent._chat = lambda: next(responses)
        execution_order = []
        agent._execute = lambda call: execution_order.append(call["name"]) or "[ok]"
        with patch("niji.agent.ThreadPoolExecutor", side_effect=AssertionError("parallel call used")):
            answer = agent._loop()
        self.assertEqual(answer, "Verified.")
        self.assertEqual(execution_order, ["read_file", "grep"])


if __name__ == "__main__":
    unittest.main()
