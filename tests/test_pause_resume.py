import threading
import unittest

from niji.agent import Agent


class AgentPauseResumeTests(unittest.TestCase):
    @staticmethod
    def bare_agent():
        agent = object.__new__(Agent)
        agent._cancel_event = threading.Event()
        agent._pause_requested = threading.Event()
        agent._resume_gate = threading.Event()
        agent._resume_gate.set()
        agent._pause_control_lock = threading.Lock()
        agent._inflight_model_calls = 0
        agent._inflight_tool_actions = 0
        agent.approved_plan = None
        agent.approval = "auto"
        agent.auto_compact = False
        agent.max_turns = 3
        agent.max_tool_calls = 10
        agent.max_tool_calls_per_turn = 10
        agent._request_tool_calls = 0
        agent.usage = {"turns": 0, "prompt_tokens": 0, "completion_tokens": 0}
        agent.messages = []
        agent.verbose = False
        return agent

    def test_pause_waits_for_inflight_tool_and_does_not_start_next_until_resumed(self):
        agent = self.bare_agent()
        paused = threading.Event()
        first_started = threading.Event()
        release_first = threading.Event()
        activity = []
        agent._record_activity = lambda level, message: (
            activity.append((level, message)), paused.set() if level == "PAUSED" else None)
        calls = [
            {"id": "call-1", "name": "write_file", "args": {}},
            {"id": "call-2", "name": "edit_file", "args": {}},
        ]
        responses = iter([
            ({"role": "assistant", "content": "", "tool_calls": calls}, "", calls),
            ({"role": "assistant", "content": "Finished safely."}, "Finished safely.", []),
        ])
        agent._chat = lambda: next(responses)
        executions = []

        def execute(call):
            executions.append(call["id"])
            if call["id"] == "call-1":
                first_started.set()
                if not release_first.wait(3):
                    raise TimeoutError("test did not release the in-flight action")
            return "result for " + call["id"]

        agent._execute = execute
        answer = []
        errors = []

        def run():
            try:
                answer.append(agent._loop())
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(first_started.wait(2))
        self.assertTrue(agent.request_pause())
        release_first.set()
        self.assertTrue(paused.wait(2))
        self.assertEqual(executions, ["call-1"])
        self.assertTrue(worker.is_alive())

        self.assertTrue(agent.request_resume())
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(answer, ["Finished safely."])
        self.assertEqual(executions, ["call-1", "call-2"])
        self.assertEqual([m.get("tool_call_id") for m in agent.messages if m.get("role") == "tool"],
                         ["call-1", "call-2"])
        self.assertEqual([level for level, _ in activity if level in ("PAUSED", "RESUMED")],
                         ["PAUSED", "RESUMED"])

    def test_pause_wins_before_provider_call_admission(self):
        agent = self.bare_agent()
        paused = threading.Event()
        agent._record_activity = lambda level, message: paused.set() if level == "PAUSED" else None
        agent.request_pause()
        admitted = []
        worker = threading.Thread(target=lambda: admitted.append(agent._admit_model_call()))
        worker.start()
        self.assertTrue(paused.wait(2))
        self.assertEqual(admitted, [])
        self.assertTrue(worker.is_alive())
        agent.request_resume()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(admitted, [True])
        agent._release_model_call()
        self.assertEqual(agent._inflight_model_calls, 0)

    def test_pause_between_boundary_check_and_tool_admission_blocks_action(self):
        agent = self.bare_agent()
        boundary_passed = threading.Event()
        release_boundary = threading.Event()
        paused = threading.Event()
        real_boundary = agent._pause_at_safe_boundary
        calls_to_boundary = []
        agent._record_activity = lambda level, message: paused.set() if level == "PAUSED" else None

        def controlled_boundary():
            if not calls_to_boundary:
                calls_to_boundary.append("pre-pause")
                boundary_passed.set()
                if not release_boundary.wait(2):
                    raise TimeoutError("test did not release the pre-admission boundary")
                return True
            return real_boundary()

        agent._pause_at_safe_boundary = controlled_boundary
        executions = []
        result = []
        agent._execute = lambda call: executions.append(call["id"]) or "done"
        worker = threading.Thread(target=lambda: result.append(
            agent._execute_admitted({"id": "raced-tool", "name": "write_file", "args": {}})))
        worker.start()
        self.assertTrue(boundary_passed.wait(2))
        self.assertTrue(agent.request_pause())
        release_boundary.set()
        self.assertTrue(paused.wait(2))
        self.assertEqual(executions, [])
        self.assertTrue(worker.is_alive())
        agent.request_resume()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [(True, "done")])
        self.assertEqual(executions, ["raced-tool"])

    def test_cancel_between_boundary_check_and_tool_admission_rejects_action(self):
        agent = self.bare_agent()
        boundary_passed = threading.Event()
        release_boundary = threading.Event()
        calls_to_boundary = []

        def controlled_boundary():
            if not calls_to_boundary:
                calls_to_boundary.append("pre-cancel")
                boundary_passed.set()
                if not release_boundary.wait(2):
                    raise TimeoutError("test did not release the pre-admission boundary")
            return not agent._cancel_event.is_set()

        agent._pause_at_safe_boundary = controlled_boundary
        executions = []
        result = []
        agent._execute = lambda call: executions.append(call["id"]) or "done"
        worker = threading.Thread(target=lambda: result.append(
            agent._execute_admitted({"id": "cancel-raced-tool", "name": "write_file", "args": {}})))
        worker.start()
        self.assertTrue(boundary_passed.wait(2))
        agent.cancel()
        release_boundary.set()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [(False, None)])
        self.assertEqual(executions, [])

    def test_pause_wins_before_new_tool_action_admission(self):
        agent = self.bare_agent()
        paused = threading.Event()
        agent._record_activity = lambda level, message: paused.set() if level == "PAUSED" else None
        agent.request_pause()
        executed = []
        result = []
        worker = threading.Thread(target=lambda: result.append(
            agent._execute_admitted({"id": "not-yet-started", "name": "write_file", "args": {}})))
        agent._execute = lambda call: executed.append(call["id"]) or "done"
        worker.start()
        self.assertTrue(paused.wait(2))
        self.assertEqual(executed, [])
        self.assertTrue(worker.is_alive())
        agent.request_resume()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(executed, ["not-yet-started"])
        self.assertEqual(result, [(True, "done")])

    def test_cancel_while_paused_wakes_worker_without_running_pending_tool(self):
        agent = self.bare_agent()
        paused = threading.Event()
        first_started = threading.Event()
        release_first = threading.Event()
        agent._record_activity = lambda level, message: paused.set() if level == "PAUSED" else None
        calls = [
            {"id": "first", "name": "write_file", "args": {}},
            {"id": "second", "name": "edit_file", "args": {}},
        ]
        agent._chat = lambda: ({"role": "assistant", "content": "", "tool_calls": calls}, "", calls)
        executions = []

        def execute(call):
            executions.append(call["id"])
            first_started.set()
            if not release_first.wait(3):
                raise TimeoutError("test did not release the in-flight action")
            return "first result"

        agent._execute = execute
        answer = []
        worker = threading.Thread(target=lambda: answer.append(agent._loop()))
        worker.start()
        self.assertTrue(first_started.wait(2))
        agent.request_pause()
        release_first.set()
        self.assertTrue(paused.wait(2))
        agent.cancel()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(executions, ["first"])
        self.assertEqual(answer, ["[Stopped by user]"])
        tool_results = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_results], ["first", "second"])
        self.assertIn("not executed", tool_results[1]["content"])
        self.assertTrue(agent._cancel_event.is_set())

    def test_cancel_wakes_pause_gate_and_resume_does_not_clear_cancellation(self):
        agent = self.bare_agent()
        agent._record_activity = lambda *args: None
        agent.request_pause()
        worker_result = []
        worker = threading.Thread(target=lambda: worker_result.append(agent._pause_at_safe_boundary()))
        worker.start()
        agent.cancel()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(worker_result, [False])
        self.assertFalse(agent.request_resume())
        self.assertTrue(agent._cancel_event.is_set())

    def test_pause_after_provider_completion_blocks_tool_until_resumed(self):
        agent = self.bare_agent()
        provider_returned = threading.Event()
        release_provider = threading.Event()
        paused = threading.Event()
        agent._record_activity = lambda level, message: paused.set() if level == "PAUSED" else None
        tool_calls = [{"id": "provider-tool", "name": "write_file", "args": {}}]
        calls = []

        def chat():
            calls.append("provider")
            if len(calls) == 1:
                provider_returned.set()
                if not release_provider.wait(2):
                    raise TimeoutError("test did not release completed provider response")
                return ({"role": "assistant", "content": "", "tool_calls": tool_calls}, "", tool_calls)
            return ({"role": "assistant", "content": "verified"}, "verified", [])

        agent._chat = chat
        executions = []
        agent._execute = lambda call: executions.append(call["id"]) or "file written"
        answer, errors = [], []

        def run():
            try:
                answer.append(agent._loop())
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(provider_returned.wait(2))
        self.assertTrue(agent.request_pause())
        release_provider.set()
        self.assertTrue(paused.wait(2))
        self.assertEqual(executions, [])
        self.assertTrue(worker.is_alive())
        self.assertTrue(agent.request_resume())
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(executions, ["provider-tool"])
        self.assertEqual(answer, ["verified"])


if __name__ == "__main__":
    unittest.main()
