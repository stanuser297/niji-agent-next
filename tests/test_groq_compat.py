import io
import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from rich.console import Console

from niji.cli import _show_provider_error
from niji.config import PRESETS, resolve_provider
from niji.setup_wizard import _connection_guidance, run_setup


class GroqCompatibilityTests(unittest.TestCase):
    def test_groq_preset_uses_active_replacement_model_and_expected_route(self):
        self.assertEqual(PRESETS["groq"]["base_url"], "https://api.groq.com/openai/v1")
        self.assertEqual(PRESETS["groq"]["model"], "openai/gpt-oss-120b")

    def test_saved_retired_groq_model_is_migrated_without_touching_api_key(self):
        cfg = {
            "provider": "groq",
            "api_keys": {"groq": "secret-not-printed"},
            "models": {"groq": "llama-3.3-70b-versatile"},
        }
        with ExitStack() as stack:
            stack.enter_context(patch("niji.config.load_config", return_value=cfg))
            save = stack.enter_context(patch("niji.config.save_config"))
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            resolved = resolve_provider()
        self.assertEqual(resolved["model"], "openai/gpt-oss-120b")
        self.assertEqual(cfg["models"]["groq"], "openai/gpt-oss-120b")
        self.assertEqual(cfg["api_keys"]["groq"], "secret-not-printed")
        save.assert_called_once_with(cfg)

    def test_explicit_model_override_is_not_migrated(self):
        cfg = {
            "provider": "groq",
            "api_keys": {"groq": "secret"},
            "models": {"groq": "llama-3.3-70b-versatile"},
        }
        with ExitStack() as stack:
            stack.enter_context(patch("niji.config.load_config", return_value=cfg))
            save = stack.enter_context(patch("niji.config.save_config"))
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            resolved = resolve_provider(model="my-explicit-model")
        self.assertEqual(resolved["model"], "my-explicit-model")
        save.assert_not_called()

    def test_setup_diagnoses_groq_404_as_retired_model_and_suggests_current_id(self):
        hint = _connection_guidance(
            {"provider": "groq", "base_url": "https://api.groq.com/openai/v1",
             "model": "llama-3.3-70b-versatile"},
            "Error code: 404 - page not found",
        )
        self.assertIn("August 16, 2026", hint)
        self.assertIn("openai/gpt-oss-120b", hint)
        self.assertIn("api.groq.com/openai/v1", hint)

    def test_setup_diagnoses_groq_401_as_key_or_account_issue(self):
        hint = _connection_guidance(
            {"provider": "groq", "base_url": "https://api.groq.com/openai/v1"},
            "Error code: 401 - Unauthorized",
        )
        self.assertIn("Groq rejected", hint)
        self.assertIn("fresh GroqCloud API key", hint)

    def test_setup_uses_migrated_default_for_saved_retired_model(self):
        cfg = {
            "provider": "groq",
            "api_keys": {"groq": "saved-key"},
            "models": {"groq": "llama-3.3-70b-versatile"},
            "custom_providers": {},
        }
        resolved = {
            "provider": "groq",
            "base_url": "https://api.groq.com/openai/v1",
            "api_key": "saved-key",
            "model": "openai/gpt-oss-120b",
        }
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch("niji.config.load_config", return_value=cfg))
            stack.enter_context(patch("niji.config.resolve_provider", return_value=resolved))
            stack.enter_context(patch("niji.config.save_config"))
            stack.enter_context(patch("niji.setup_wizard._ask_key", return_value="saved-key"))
            stack.enter_context(patch("niji.setup_wizard.test_connection",
                                      return_value=(True, "chat endpoint OK")))
            picker = stack.enter_context(patch("niji.setup_wizard.arrow_select", return_value="groq"))
            ask = stack.enter_context(patch("niji.setup_wizard.Prompt.ask",
                                            side_effect=["openai/gpt-oss-120b"]))
            stack.enter_context(patch("niji.setup_wizard.console",
                                      Console(file=output, width=90, color_system=None)))
            run_setup()
        self.assertEqual(picker.call_args.args[1][0][0], "openrouter")
        self.assertEqual(ask.call_args_list[0].kwargs["default"], "openai/gpt-oss-120b")

    def test_runtime_groq_401_message_distinguishes_auth_from_model_route(self):
        class ProviderError(Exception):
            status_code = 401

        output = io.StringIO()
        with patch("niji.cli.Console", return_value=Console(file=output, color_system=None)):
            _show_provider_error(
                {"provider": "groq", "base_url": "https://api.groq.com/openai/v1"},
                ProviderError("Unauthorized"),
            )
        self.assertIn("Groq rejected", output.getvalue())
        self.assertIn("fresh GroqCloud API key", output.getvalue())
        self.assertNotIn("Unauthorized", output.getvalue())

    def test_runtime_groq_404_explains_retired_model(self):
        class ProviderError(Exception):
            status_code = 404

        output = io.StringIO()
        with patch("niji.cli.Console", return_value=Console(file=output, color_system=None)):
            _show_provider_error(
                {"provider": "groq", "base_url": "https://api.groq.com/openai/v1",
                 "model": "llama-3.3-70b-versatile"},
                ProviderError("Not found"),
            )
        self.assertIn("August 16, 2026", output.getvalue())
        self.assertIn("openai/gpt-oss-120b", output.getvalue())


if __name__ == "__main__":
    unittest.main()
