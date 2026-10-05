import io
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from rich.console import Console

from niji.setup_wizard import (_connection_guidance, _read_secret,
                               run_setup, test_connection)


class ConnectionGuidanceTests(unittest.TestCase):
    def test_visible_key_entry_is_default_and_accepts_text(self):
        output = io.StringIO()
        with ExitStack() as stack:
            prompt = stack.enter_context(patch(
                "niji.setup_wizard.Prompt.ask", side_effect=["2", "nvapi-visible"]))
            stack.enter_context(patch("niji.setup_wizard.console", Console(file=output, color_system=None)))
            value = _read_secret("API key")
        self.assertEqual(value, "nvapi-visible")
        self.assertIn("visible mode", output.getvalue().lower())
        self.assertIn("API key (visible)", prompt.call_args_list[1].args[0])

    def test_hidden_mode_can_be_selected(self):
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch("niji.setup_wizard.Prompt.ask", return_value="1"))
            stack.enter_context(patch("niji.setup_wizard.getpass.getpass", return_value="nvapi-secret"))
            stack.enter_context(patch("niji.setup_wizard.console", Console(file=output, color_system=None)))
            value = _read_secret("API key")
        self.assertEqual(value, "nvapi-secret")
        self.assertNotIn("nvapi-secret", output.getvalue())

    def test_nvidia_403_is_identified_as_auth_not_model_typo(self):
        hint = _connection_guidance(
            {"base_url": "https://integrate.api.nvidia.com/v1"},
            "Error code: 403 - {'detail': 'Authorization failed'}",
        )
        self.assertIn("authorization", hint.lower())
        self.assertIn("not a Niji branding problem", hint)
        self.assertIn("NVIDIA NIM API key", hint)
        self.assertIn("wrong model usually returns 404", hint)

    def test_nvidia_404_recommends_the_tested_default_model(self):
        hint = _connection_guidance(
            {"base_url": "https://integrate.api.nvidia.com/v1"},
            "Error code: 404 - model not found",
        )
        self.assertIn("nvidia/nemotron-3.5-lightning-30b-a3b", hint)

    def test_other_provider_auth_failure_is_actionable(self):
        hint = _connection_guidance(
            {"base_url": "https://api.openai.com/v1"},
            "Error code: 401 - Unauthorized",
        )
        self.assertIn("denied", hint.lower())
        self.assertIn("replace", hint.lower())

    def test_setup_offers_hidden_key_retry_and_saves_only_valid_key(self):
        cfg = {"provider": "nvidia", "api_keys": {"nvidia": "rejected-key"},
               "models": {"nvidia": "z-ai/glm-5.3-flash"}}
        output = io.StringIO()
        provider_result = {"provider": "nvidia", "base_url": "https://integrate.api.nvidia.com/v1",
                           "api_key": "accepted-key", "model": "z-ai/glm-5.3-flash"}
        with ExitStack() as stack:
            stack.enter_context(patch("niji.config.load_config", return_value=cfg))
            stack.enter_context(patch("niji.config.resolve_provider", return_value=provider_result))
            save = stack.enter_context(patch("niji.config.save_config"))
            stack.enter_context(patch("niji.setup_wizard._ask_key", return_value="rejected-key"))
            stack.enter_context(patch("niji.setup_wizard._read_replacement_key", return_value="accepted-key"))
            stack.enter_context(patch("niji.setup_wizard.test_connection", side_effect=[
                (False, "Error code: 403 - Authorization failed"),
                (True, "chat endpoint OK")]))
            picker = stack.enter_context(patch("niji.setup_wizard.arrow_select", return_value="nvidia"))
            stack.enter_context(patch("niji.setup_wizard.Prompt.ask",
                                      side_effect=["z-ai/glm-5.3-flash", "y"]))
            stack.enter_context(patch("niji.setup_wizard.console",
                                      Console(file=output, width=72, color_system=None)))
            result = run_setup()
        self.assertEqual(result["api_key"], "accepted-key")
        self.assertEqual(cfg["api_keys"]["nvidia"], "accepted-key")
        self.assertEqual(picker.call_args.args[1][-1][0], "custom")
        self.assertEqual(picker.call_args.args[2], 8)
        save.assert_called_once_with(cfg)
        self.assertIn("Niji-Agent", output.getvalue())
        self.assertIn("Connected", output.getvalue())

    def test_failed_authorization_keeps_saved_key_unchanged(self):
        cfg = {"provider": "nvidia", "api_keys": {"nvidia": "rejected-key"},
               "models": {"nvidia": "z-ai/glm-5.3-flash"}}
        with ExitStack() as stack:
            stack.enter_context(patch("niji.config.load_config", return_value=cfg))
            stack.enter_context(patch("niji.config.resolve_provider", return_value={
                "provider": "nvidia", "base_url": "https://integrate.api.nvidia.com/v1",
                "api_key": "rejected-key", "model": "z-ai/glm-5.3-flash"}))
            save = stack.enter_context(patch("niji.config.save_config"))
            stack.enter_context(patch("niji.setup_wizard._ask_key", return_value="rejected-key"))
            stack.enter_context(patch("niji.setup_wizard.test_connection", return_value=(
                False, "Error code: 403 - Authorization failed")))
            stack.enter_context(patch("niji.setup_wizard.arrow_select", return_value="nvidia"))
            stack.enter_context(patch("niji.setup_wizard.Prompt.ask",
                                      side_effect=["z-ai/glm-5.3-flash", "n"]))
            stack.enter_context(patch("niji.setup_wizard.console",
                                      Console(file=io.StringIO(), width=72, color_system=None)))
            with self.assertRaises(SystemExit):
                run_setup()
        save.assert_not_called()
        self.assertEqual(cfg["api_keys"]["nvidia"], "rejected-key")

    def test_connection_probe_retries_newer_completion_limit_parameter(self):
        import sys
        import types
        calls = []
        class BadParameter(Exception):
            status_code = 400
        class Completions:
            def create(self, **kwargs):
                calls.append(kwargs)
                if "max_tokens" in kwargs:
                    raise BadParameter("Unsupported parameter max_tokens")
                return object()
        fake_openai = types.ModuleType("openai")
        fake_openai.OpenAI = lambda **kwargs: types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=Completions()))
        with patch.dict(sys.modules, {"openai": fake_openai}):
            ok, message = test_connection({"provider": "openai", "model": "gpt-5-mini",
                                          "api_key": "not-printed",
                                          "base_url": "https://api.openai.com/v1"})
        self.assertTrue(ok)
        self.assertEqual(message, "chat endpoint OK")
        self.assertEqual(calls[0]["max_tokens"], 1)
        self.assertEqual(calls[1]["max_completion_tokens"], 1)


if __name__ == "__main__":
    unittest.main()
