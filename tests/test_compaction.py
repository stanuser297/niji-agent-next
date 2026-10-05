import sys
import types
import unittest
from unittest.mock import Mock, patch

from niji.compaction import estimate_tokens, maybe_compact


class TooLargeError(Exception):
    status_code = 413


class CompactionTests(unittest.TestCase):
    def make_agent(self):
        if "niji.agent" not in sys.modules and "openai" not in sys.modules:
            fake_openai = types.ModuleType("openai")
            fake_openai.OpenAI = lambda **kwargs: types.SimpleNamespace(options=kwargs)
            sys.modules["openai"] = fake_openai
        from niji.agent import Agent
        with patch("niji.agent.OpenAI", return_value=object()):
            return Agent({"provider": "groq", "model": "m", "api_key": "k",
                          "base_url": "https://example.test/v1"}, verbose=False)

    def test_saved_auto_compaction_settings_are_loaded_and_respected(self):
        from niji.agent import Agent
        with patch("niji.agent.load_config", return_value={
            "auto_compact": False, "compaction_threshold": 40000
        }), patch("niji.agent.OpenAI", return_value=object()):
            agent = Agent({"provider": "test", "model": "m", "api_key": "k",
                           "base_url": "https://example.test/v1"}, verbose=False)
        self.assertFalse(agent.auto_compact)
        self.assertEqual(agent.compaction_threshold, 40000)
        tool_call = {"id": "call-1", "name": "read_file", "arguments": {}}
        assistant_tool = {"role": "assistant", "content": "", "tool_calls": [tool_call]}
        agent._chat = Mock(side_effect=[(assistant_tool, "", [tool_call]),
                                                      ({"role": "assistant", "content": "done"}, "done", [])])
        agent._execute = lambda call: "read ok"
        with patch("niji.agent.maybe_compact") as compact:
            self.assertEqual(agent._loop(), "done")
        compact.assert_not_called()

    def test_forced_compaction_keeps_current_request_even_for_short_transcript(self):
        messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "environment"},
            {"role": "assistant", "content": "old output " * 5000},
            {"role": "user", "content": "please continue the current fix"},
        ]
        compacted, changed = maybe_compact(messages, None, "m", max_tokens=8000,
                                            keep_recent=6, force=True, summarize=False)
        self.assertTrue(changed)
        self.assertLess(estimate_tokens(compacted), estimate_tokens(messages))
        self.assertEqual(compacted[-1]["content"], "please continue the current fix")
        self.assertIn("Untrusted summary", compacted[1]["content"])

    def test_compaction_starts_at_user_boundary_not_orphan_tool_result(self):
        tool_call = {"id": "call-1", "type": "function",
                     "function": {"name": "grep", "arguments": "{}"}}
        messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "environment"},
            {"role": "user", "content": "old task"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call]},
            {"role": "tool", "tool_call_id": "call-1", "content": "old result " * 1000},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "current task"},
        ]
        compacted, changed = maybe_compact(messages, None, "m", max_tokens=100,
                                            keep_recent=4, force=True, summarize=False)
        self.assertTrue(changed)
        roles_after_summary = [m["role"] for m in compacted[2:]]
        self.assertNotIn("tool", roles_after_summary)
        self.assertEqual(compacted[-1]["content"], "current task")

    def test_http_413_compacts_once_and_retries_with_smaller_context(self):
        agent = self.make_agent()
        agent.auto_compact = False
        agent.messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "environment"},
            {"role": "assistant", "content": "old output " * 5000},
            {"role": "user", "content": "current user request"},
        ]
        requests = []

        def create(**kwargs):
            requests.append(kwargs)
            if len(requests) == 1:
                raise TooLargeError("request too large")
            return []

        agent.client = types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create)))
        msg, text, tools = agent._chat()
        self.assertEqual(len(requests), 2)
        self.assertGreater(estimate_tokens(requests[0]["messages"]),
                           estimate_tokens(requests[1]["messages"]))
        self.assertEqual(requests[1]["messages"][-1]["content"], "current user request")
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(text, "")
        self.assertEqual(tools, [])

    def test_nvidia_nemotron_fast_chat_disables_reasoning_and_keeps_usage_stream(self):
        agent = self.make_agent()
        agent.provider_name = "nvidia"
        agent.model = "nvidia/nemotron-3.5-lightning-30b-a3b"
        captured = {}
        answer = types.SimpleNamespace(
            usage=None,
            choices=[types.SimpleNamespace(delta=types.SimpleNamespace(
                content="OK", tool_calls=None))],
        )
        usage = types.SimpleNamespace(prompt_tokens=2, completion_tokens=1)
        final = types.SimpleNamespace(usage=usage, choices=[])
        agent.client = types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **kwargs: captured.update(kwargs) or [answer, final])))

        _, text, _ = agent._chat()

        self.assertEqual(text, "OK")
        self.assertTrue(agent.usage_complete)
        self.assertEqual(captured["stream_options"], {"include_usage": True})
        self.assertEqual(captured["extra_body"], {
            "chat_template_kwargs": {"enable_thinking": False},
        })

    def test_plan_only_sends_no_tools_and_does_not_mutate_history(self):
        agent = self.make_agent()
        agent.plan_only = True
        agent.messages = [{"role": "system", "content": "rules"},
                          {"role": "user", "content": "environment"},
                          {"role": "user", "content": "fix my bug"}]
        captured = {}
        agent.client = types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **kwargs: captured.update(kwargs) or [])))
        agent._chat()
        self.assertEqual(captured["tools"], [])
        self.assertIn("planning-only turn", captured["messages"][-1]["content"])
        self.assertIn("numbered list", captured["messages"][-1]["content"])
        self.assertEqual(agent.messages[-1]["content"], "fix my bug")
        self.assertEqual(len(agent.messages), 3)

    def test_stream_callback_receives_live_text_chunks(self):
        agent = self.make_agent()
        def chunk(text):
            delta = types.SimpleNamespace(content=text, tool_calls=None)
            return types.SimpleNamespace(usage=None, choices=[types.SimpleNamespace(delta=delta)])
        agent.client = types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=lambda **kwargs: [chunk("Thinking "), chunk("now")])))
        received = []
        agent.stream_callback = received.append
        _, text, _ = agent._chat()
        self.assertEqual(text, "Thinking now")
        self.assertEqual(received, ["Thinking ", "now"])

    def test_cloud_usage_requires_one_complete_valid_provider_total(self):
        def run(records, *, prompt_cap=100_000, completion_cap=50):
            agent = self.make_agent()
            agent.cloud_mode = True
            agent.cloud_prompt_token_limit = prompt_cap
            agent.cloud_completion_token_limit = completion_cap
            agent.usage = {"prompt_tokens": 0, "completion_tokens": 0, "turns": 0}
            agent.client = types.SimpleNamespace(chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=lambda **_kwargs: [
                    types.SimpleNamespace(usage=item, choices=[]) for item in records
                ])))
            return agent

        valid = run([types.SimpleNamespace(prompt_tokens=12, completion_tokens=5)])
        valid._chat()
        self.assertTrue(valid.usage_reported)
        self.assertTrue(valid.usage_complete)
        self.assertEqual(valid.usage["prompt_tokens"], 12)
        self.assertEqual(valid.usage["completion_tokens"], 5)

        partial = run([types.SimpleNamespace(prompt_tokens=12, completion_tokens=None)])
        with self.assertRaisesRegex(RuntimeError, "complete, valid token-usage"):
            partial._chat()
        self.assertTrue(partial.usage_reported)
        self.assertFalse(partial.usage_complete)

        duplicate = run([
            types.SimpleNamespace(prompt_tokens=6, completion_tokens=2),
            types.SimpleNamespace(prompt_tokens=12, completion_tokens=5),
        ])
        with self.assertRaisesRegex(RuntimeError, "complete, valid token-usage"):
            duplicate._chat()
        self.assertFalse(duplicate.usage_complete)

        over_cap = run([types.SimpleNamespace(prompt_tokens=10, completion_tokens=51)])
        with self.assertRaisesRegex(RuntimeError, "completion-token budget"):
            over_cap._chat()
        self.assertFalse(over_cap.usage_complete)

    def test_one_413_retry_only_and_surface_persistent_failure(self):
        agent = self.make_agent()
        agent.messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "environment"},
            {"role": "assistant", "content": "earlier context"},
            {"role": "user", "content": "current request"},
        ]
        calls = []

        def create(**kwargs):
            calls.append(kwargs)
            raise TooLargeError("still too large")

        agent.client = types.SimpleNamespace(chat=types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create)))
        with self.assertRaises(TooLargeError):
            agent._chat()
        self.assertEqual(len(calls), 2)
        self.assertEqual(agent.messages[-1]["content"], "current request")


if __name__ == "__main__":
    unittest.main()
