import io
import sys
import types
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from rich.console import Console

from niji.cli import _activate_model, _arrow_select, _cmd_models
from niji.model_catalog import fetch_provider_models, provider_is_configured, provider_names


class ModelCatalogTests(unittest.TestCase):
    def test_provider_names_include_presets_and_saved_custom_names(self):
        names = provider_names({"custom_providers": {"my-endpoint": {}, "openai": {}}})
        self.assertIn("groq", names)
        self.assertIn("my-endpoint", names)
        self.assertEqual(names.count("openai"), 1)

    def test_provider_is_marked_configured_when_saved_key_exists(self):
        self.assertTrue(provider_is_configured(
            "groq", {"api_keys": {"groq": "hidden"}, "custom_providers": {}}))
        self.assertFalse(provider_is_configured(
            "groq", {"api_keys": {}, "custom_providers": {}}))

    def test_keyless_local_provider_is_configured_from_saved_model(self):
        self.assertTrue(provider_is_configured("ollama", {
            "api_keys": {}, "models": {"ollama": "qwen2.5"}, "custom_providers": {},
        }))
        self.assertFalse(provider_is_configured("ollama", {
            "api_keys": {}, "models": {}, "custom_providers": {},
        }))

    def test_fetch_provider_models_sorts_ids_and_keeps_active_model_visible(self):
        fake_openai = types.ModuleType("openai")

        class FakeClient:
            def __init__(self, **kwargs):
                self.models = types.SimpleNamespace(list=lambda: types.SimpleNamespace(
                    data=[types.SimpleNamespace(id="z-model"),
                          types.SimpleNamespace(id="a-model")]))

        fake_openai.OpenAI = FakeClient
        cfg = {"api_key": "not-printed", "base_url": "https://example.test/v1",
               "model": "current-model"}
        with patch.dict(sys.modules, {"openai": fake_openai}):
            models, message = fetch_provider_models(cfg)
        self.assertEqual(models, ["current-model", "a-model", "z-model"])
        self.assertEqual(message, "")

    def test_model_catalog_failure_does_not_echo_exception_details(self):
        fake_openai = types.ModuleType("openai")

        class FakeClient:
            def __init__(self, **kwargs):
                self.models = types.SimpleNamespace(list=lambda: fail())

        def fail():
            raise RuntimeError("sensitive request headers should not print")

        fake_openai.OpenAI = FakeClient
        with patch.dict(sys.modules, {"openai": fake_openai}):
            models, message = fetch_provider_models({
                "api_key": "not-printed", "base_url": "https://example.test/v1",
                "model": "configured-model"})
        self.assertEqual(models, [])
        self.assertIn("RuntimeError", message)
        self.assertNotIn("sensitive", message)

    def test_cli_model_catalog_marks_unconnected_providers_without_network_calls(self):
        cfg = {"provider": "groq", "api_keys": {}, "models": {},
               "custom_providers": {}}
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch("niji.cli.load_config", return_value=cfg))
            fetch = stack.enter_context(patch("niji.cli.fetch_provider_models"))
            stack.enter_context(patch("niji.cli.Console",
                                      return_value=Console(file=output, color_system=None)))
            _cmd_models(["niji", "models", "groq"])
        fetch.assert_not_called()
        self.assertIn("Not connected", output.getvalue())
        self.assertNotIn("not-printed", output.getvalue())

    def test_successful_model_switch_updates_session_and_persists_choice(self):
        cfg = {"api_keys": {"openai": "key"}, "models": {},
               "custom_providers": {}}
        provider_cfg = {"provider": "openai", "base_url": "https://api.openai.com/v1",
                        "api_key": "key", "model": "gpt-5-mini"}
        fake_openai = types.ModuleType("openai")
        class FakeClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
        fake_openai.OpenAI = FakeClient

        class Agent:
            client = None
            model = "old-model"
            provider_name = "groq"
            provider_cfg = {}
            messages = []

        agent = Agent()
        active_provider = {"provider": "groq", "model": "old-model"}
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch("niji.cli.resolve_provider", return_value=provider_cfg))
            stack.enter_context(patch("niji.cli.load_config", return_value=cfg))
            save = stack.enter_context(patch("niji.cli.save_config"))
            stack.enter_context(patch("niji.setup_wizard.test_connection",
                                      return_value=(True, "chat endpoint OK")))
            stack.enter_context(patch("niji.cli._render_home"))
            stack.enter_context(patch("niji.cli.Console",
                                      return_value=Console(file=output, color_system=None)))
            stack.enter_context(patch.dict(sys.modules, {"openai": fake_openai}))
            result = _activate_model(agent, active_provider, "openai", "gpt-5-mini")
        self.assertTrue(result)
        self.assertEqual(agent.model, "gpt-5-mini")
        self.assertEqual(agent.provider_name, "openai")
        self.assertEqual(active_provider["provider"], "openai")
        self.assertEqual(cfg["models"]["openai"], "gpt-5-mini")
        self.assertEqual(cfg["provider"], "openai")
        save.assert_called_once_with(cfg)

    def test_auth_rejected_model_switch_explains_current_model_stays_active(self):
        cfg = {"api_keys": {"nvidia": "saved-key"}, "models": {},
               "custom_providers": {}}
        provider_cfg = {"provider": "nvidia", "base_url": "https://example.test/v1",
                        "api_key": "saved-key", "model": "z-ai/glm-5.3-flash"}
        class Agent:
            model = "openai/gpt-oss-120b"
            provider_name = "groq"
        agent = Agent()
        provider = {"provider": "groq", "model": "openai/gpt-oss-120b"}
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch("niji.cli.resolve_provider", return_value=provider_cfg))
            stack.enter_context(patch("niji.setup_wizard.test_connection",
                                      return_value=(False, "HTTP 403 Authorization failed")))
            stack.enter_context(patch("niji.cli.Console",
                                      return_value=Console(file=output, color_system=None)))
            save = stack.enter_context(patch("niji.cli.save_config"))
            applied = _activate_model(agent, provider, "nvidia", "z-ai/glm-5.3-flash")
        self.assertFalse(applied)
        self.assertIn("Model was NOT switched", output.getvalue())
        self.assertIn("groq/openai/gpt-oss-120b", output.getvalue())
        self.assertIn("/setup", output.getvalue())
        save.assert_not_called()

    def test_arrow_menu_fallback_uses_provider_name_not_an_index(self):
        fake_stdin = types.SimpleNamespace(isatty=lambda: False)
        fake_stdout = types.SimpleNamespace(isatty=lambda: False)
        with (
            patch("niji.cli.sys.stdin", fake_stdin),
            patch("niji.cli.sys.stdout", fake_stdout),
            patch("niji.cli.Prompt.ask", return_value="groq") as prompt,
        ):
            selected = _arrow_select("Choose provider", [("openai", "OpenAI"), ("groq", "Groq")])
        self.assertEqual(selected, "groq")
        self.assertIn("type an option", prompt.call_args.args[0])

    def test_selected_model_can_be_applied_if_non_auth_probe_is_unavailable(self):
        cfg = {"api_keys": {"openai": "key"}, "models": {},
               "custom_providers": {}}
        provider_cfg = {"provider": "openai", "base_url": "https://api.openai.com/v1",
                        "api_key": "key", "model": "gpt-5-mini"}
        fake_openai = types.ModuleType("openai")
        fake_openai.OpenAI = lambda **kwargs: object()

        class Agent:
            messages = []
        agent = Agent()
        provider = {"provider": "groq", "model": "old-model"}
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch("niji.cli.resolve_provider", return_value=provider_cfg))
            stack.enter_context(patch("niji.cli.load_config", return_value=cfg))
            stack.enter_context(patch("niji.cli.save_config"))
            stack.enter_context(patch("niji.setup_wizard.test_connection",
                                      return_value=(False, "HTTP 400 unsupported test parameter")))
            stack.enter_context(patch("niji.cli._render_home"))
            stack.enter_context(patch("niji.cli.Console",
                                      return_value=Console(file=output, color_system=None)))
            stack.enter_context(patch("niji.cli.Prompt.ask", return_value="y"))
            stack.enter_context(patch.dict(sys.modules, {"openai": fake_openai}))
            applied = _activate_model(agent, provider, "openai", "gpt-5-mini")
        self.assertTrue(applied)
        self.assertEqual(agent.model, "gpt-5-mini")
        self.assertIn("not verified", output.getvalue())


if __name__ == "__main__":
    unittest.main()
