import base64
import unittest

from niji.agent import Agent


class AgentVisionInputTests(unittest.TestCase):
    def test_provider_message_gets_ephemeral_openai_compatible_image_parts(self):
        agent = Agent.__new__(Agent)
        original = [
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": "Describe this"},
        ]
        image_bytes = b"fake image bytes"
        agent.messages = [dict(message) for message in original]
        agent._image_attachment_turn = 1
        agent._image_attachment_text = "Describe this"
        agent._image_attachments = [
            {"name": "pic.png", "mime": "image/png", "data": image_bytes}
        ]

        request = agent._provider_messages()

        self.assertEqual(agent.messages, original)
        self.assertEqual(request[0], original[0])
        self.assertEqual(request[1]["role"], "user")
        self.assertTrue(request[1]["content"][0]["text"].startswith("Describe this [Attached images: pic.png."))
        self.assertIn("untrusted user-provided data", request[1]["content"][0]["text"])
        image_part = request[1]["content"][1]
        self.assertEqual(image_part["type"], "image_url")
        self.assertEqual(image_part["image_url"]["detail"], "auto")
        self.assertEqual(image_part["image_url"]["url"],
                         "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii"))
        self.assertIsInstance(agent.messages[1]["content"], str)

    def test_image_parts_are_cleaned_before_saving_session_history(self):
        agent = Agent.__new__(Agent)
        agent._request_tool_calls = 0
        agent.messages = [{"role": "system", "content": "private system prompt"}]
        agent._loop = lambda: agent._provider_messages() and "seen image"
        saved = []
        agent._save_session = lambda: saved.append(list(agent.messages))
        agent._reconcile_interrupted_tool_calls = lambda: None

        result = agent.chat("look at this", image_attachments=[
            {"name": "chart.webp", "mime": "image/webp", "data": b"bytes"}
        ])

        self.assertEqual(result, "seen image")
        self.assertEqual(agent.messages[-1], {"role": "user", "content": "look at this"})
        self.assertEqual(agent._image_attachments, [])
        self.assertIsNone(agent._image_attachment_turn)
        self.assertIsNone(agent._image_attachment_text)
        self.assertEqual(saved[0][-1]["content"], "look at this")
        self.assertNotIn(b"bytes", repr(saved).encode())

    def test_compaction_can_still_locate_the_image_turn_by_its_prompt(self):
        agent = Agent.__new__(Agent)
        agent.messages = [
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": "Describe this"},
        ]
        agent._image_attachment_turn = 99
        agent._image_attachment_text = "Describe this"
        agent._image_attachments = [
            {"mime": "image/jpeg", "data": b"jpeg bytes"}
        ]
        request = agent._provider_messages()
        self.assertTrue(request[1]["content"][0]["text"].startswith(
            "Describe this [Attached images: image."))
        self.assertTrue(request[1]["content"][1]["image_url"]["url"].startswith(
            "data:image/jpeg;base64,"))


if __name__ == "__main__":
    unittest.main()
