"""First-run setup wizard and provider connection diagnostics."""
import getpass
import os
import re
import warnings
from urllib.parse import urlparse

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from .terminal_picker import arrow_select

console = Console()

WIZARD_PROVIDERS = [
    ("openrouter", "OpenRouter (recommended — one key, many models)"),
    ("openai", "OpenAI"),
    ("anthropic", "Anthropic (Claude)"),
    ("gemini", "Google Gemini"),
    ("groq", "Groq"),
    ("deepseek", "DeepSeek"),
    ("together", "Together AI"),
    ("ollama", "Ollama (local, free, no API key)"),
    ("nvidia", "NVIDIA NIM (Nemotron chat and more)"),
]



def needs_setup(provider_name: str | None = None, api_key: str | None = None) -> bool:
    """Check whether a first-run wizard is needed for the selected provider."""
    if api_key:
        return False
    from .config import PRESETS, load_config

    cfg = load_config()
    name = provider_name or os.environ.get("NIJI_PROVIDER") or cfg.get("provider")
    if name:
        preset = PRESETS.get(name)
        if preset:
            # Migrate a same-name custom provider through the preset wizard (e.g. NVIDIA).
            if cfg.get("custom_providers", {}).get(name):
                return True
            env_key = preset.get("env_key")
            if not env_key:
                return False
            return not bool(cfg.get("api_keys", {}).get(name)
                            or os.environ.get(env_key)
                            or os.environ.get("NIJI_API_KEY"))
        if cfg.get("custom_providers", {}).get(name) or os.environ.get("NIJI_BASE_URL"):
            return False
        # Unknown provider names should reach resolve_provider for a useful error.
        return False

    if os.environ.get("NIJI_API_KEY") or os.environ.get("NIJI_BASE_URL"):
        return False
    if any(p.get("env_key") and os.environ.get(p["env_key"])
           for p in PRESETS.values()):
        return False
    return True


def test_connection(provider_cfg: dict):
    """Returns (ok, message) after a minimal authenticated chat request; never raises."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=provider_cfg["api_key"],
                        base_url=provider_cfg["base_url"], timeout=25,
                        max_retries=0)
    except Exception as e:
        return False, str(e)[:300]

    # OpenAI-compatible providers disagree on the completion-limit parameter.
    # Newer reasoning models may reject max_tokens, while older providers may
    # not accept max_completion_tokens; retry only that known compatibility case.
    for limit_name in ("max_tokens", "max_completion_tokens"):
        try:
            client.chat.completions.create(
                model=provider_cfg["model"],
                messages=[{"role": "user", "content": "ping"}],
                **{limit_name: 1})
            return True, "chat endpoint OK"
        except Exception as exc:
            message = str(exc)
            status = getattr(exc, "status_code", None)
            if (limit_name == "max_tokens" and status == 400
                    and "max_tokens" in message.lower()):
                continue
            # Listing models is not enough: validate the exact chat route/model.
            return False, message[:300]
    return False, "Provider rejected both supported completion-limit parameters."


def _read_secret(label: str, allow_blank: bool = False):
    """Prompt for a key; visible input is the default for reliable Termux paste."""
    console.print("[yellow]Key-entry mode: [1] hidden  [2] visible (recommended for Termux).[/]")
    mode = Prompt.ask("Choose key-entry mode", choices=["1", "2"], default="2")
    if mode == "2":
        console.print("[yellow]Visible mode: the key appears on screen while entering. "
                      "Use only if nobody else can see your terminal.[/]")
        value = Prompt.ask(label + " (visible)").strip()
        if value:
            return value
        if allow_blank:
            return None
        retry = Prompt.ask("No key received. Try entering it again?", choices=["y", "n"], default="y")
        if retry == "y":
            return Prompt.ask(label + " (visible)").strip() or None
        return None

    console.print("[dim]Hidden mode: no letters or dots will appear. Android users can long-press "
                  "the terminal and choose Paste, then press Enter.[/]")
    warnings_seen = []
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", getpass.GetPassWarning)
            raw = getpass.getpass(label + " (hidden): ")
            warnings_seen = caught
    except Exception:
        raw = ""
    if warnings_seen:
        console.print("[yellow]This terminal could not hide password input; it may have been echoed.[/]")
    raw = raw.strip()
    if raw or allow_blank:
        return raw or None
    retry = Prompt.ask("No key received. Retry with visible input? The key will show on screen",
                       choices=["y", "n"], default="y")
    if retry == "y":
        return Prompt.ask(label + " (visible)").strip() or None
    return None


def _ask_key(provider_name: str, env_key: str | None, stored_key: str | None = None):
    if not env_key:
        return None
    env_value = os.environ.get(env_key) or os.environ.get("NIJI_API_KEY")
    if env_value:
        use = Prompt.ask(f"Use existing {env_key if os.environ.get(env_key) else 'NIJI_API_KEY'} from environment?",
                         choices=["y", "n"], default="y")
        if use == "y":
            return env_value
    if stored_key:
        use = Prompt.ask(f"Keep the API key already saved for {provider_name}?",
                         choices=["y", "n"], default="y")
        if use == "y":
            return stored_key
    return _read_secret(f"Paste your {provider_name} API key (hidden)")


def _wizard_custom_provider():
    console.print("\n[bold]Custom provider[/] (OpenAI-compatible endpoint)")
    name = Prompt.ask("Provider name", default="custom").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", name):
        raise SystemExit("Use a provider name with letters, numbers, _ or - (max 32 chars).")
    base_url = Prompt.ask("Base URL", default="http://localhost:11434/v1").strip()
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SystemExit("Base URL must start with http:// or https:// and include a host.")
    model = Prompt.ask("Default model", default="default").strip()
    key = _read_secret("API key (optional for local/no-auth endpoints)", allow_blank=True)
    return name, {"base_url": base_url, "model": model, "api_key": key}


def _connection_guidance(provider_cfg: dict, message: str) -> str:
    """Turn common provider failures into actionable, provider-aware guidance."""
    text = message.lower()
    status = next((code for code in (400, 401, 403, 404, 408, 409, 413, 422, 429,
                                     500, 502, 503, 504) if str(code) in text), None)
    nvidia = "nvidia.com" in provider_cfg.get("base_url", "")
    groq = (provider_cfg.get("provider") == "groq"
            or "api.groq.com" in provider_cfg.get("base_url", ""))
    if status in (401, 403):
        if nvidia:
            return ("NVIDIA denied this key or its access (HTTP %s). This is an authorization issue, "
                    "not a Niji branding problem; a wrong model usually returns 404. Choose `n` to "
                    "replace the saved key with an NVIDIA NIM API key, and check that your NVIDIA "
                    "account is allowed to use this model. Hidden input is available; visible mode "
                    "shows the key as you type." % status)
        if groq:
            return (f"Groq rejected this API key or account access (HTTP {status}). The model/route "
                    "is separate from authorization. In setup, replace the saved key with a fresh "
                    "GroqCloud API key and confirm the account can use the API. Niji never displays "
                    "or prints the saved key.")
        return (f"The provider denied this API key or its account access (HTTP {status}). "
                "Replace it with a key for this provider and confirm the account has API access.")
    if status == 404 and nvidia:
        return ("NVIDIA could not find this model or route (HTTP 404). Verify the exact model ID in "
                "the NVIDIA API model catalog; Niji's tested chat default is "
                "`nvidia/nemotron-3.5-lightning-30b-a3b`.")
    if status == 404 and groq:
        model = provider_cfg.get("model", "(unknown)")
        if model == "llama-3.3-70b-versatile":
            return ("Groq returned HTTP 404. `llama-3.3-70b-versatile` was shut down for "
                    "developer/free-tier accounts on August 16, 2026. Use the current model "
                    "ID `openai/gpt-oss-120b`; keep base URL `https://api.groq.com/openai/v1`.")
        return (f"Groq returned HTTP 404 for model `{model}`. Check that the exact model ID is "
                "listed in your Groq account and that the base URL is "
                "`https://api.groq.com/openai/v1`. Niji's current Groq default is "
                "`openai/gpt-oss-120b`.")
    if status == 404:
        return "The provider could not find this model or API route (HTTP 404). Check the base URL and exact model ID."
    if status == 400:
        return ("The provider rejected this chat request (HTTP 400). The catalog can include models "
                "that are not chat-capable or require different request options. Choose a text/chat "
                "model; if you knowingly want to try this model, the picker can apply it unverified.")
    if status == 429 and any(word in text for word in ("quota", "billing", "credit", "payment")):
        return "Provider credits/quota are exhausted (HTTP 429); retrying will not fix billing. Check account credits or choose another provider."
    if status == 429:
        return "Provider rate limit reached (HTTP 429). Wait for the provider reset, then run `niji setup` again; do not rapidly retry."
    if status in (408, 409, 500, 502, 503, 504):
        return f"Provider temporarily failed the connection test (HTTP {status}). Check internet/provider status and retry setup later; saved settings remain unchanged."
    if status == 413:
        return "Provider says this request is too large (HTTP 413). Check model/context requirements or choose a different model."
    return "The connection test failed. Check network/DNS, API base URL, and model name."


def _read_replacement_key(provider_name: str) -> str:
    return _read_secret(f"Paste the replacement {provider_name} API key (hidden)") or ""


def run_setup(default_model: str | None = None,
              provider_name: str | None = None) -> dict:
    from .config import (MODEL_MIGRATIONS, PRESETS, load_config,
                         resolve_provider, save_config)
    from .ui import render_setup_banner

    render_setup_banner(console)
    console.print("[bold]Welcome! Let's configure a provider.[/]\n")
    cfg = load_config()
    provider_names = [name for name, _ in WIZARD_PROVIDERS]
    configured_name = cfg.get("provider")
    default_name = (provider_name if provider_name in provider_names
                    else configured_name if configured_name in provider_names
                    else "openrouter")
    choices = [(name, label) for name, label in WIZARD_PROVIDERS]
    choices.append(("custom", "Custom (any OpenAI-compatible endpoint)"))
    default_index = next((i for i, item in enumerate(choices)
                          if item[0] == default_name), 0)
    choice = arrow_select("Choose a provider", choices, default_index)
    if choice is None:
        raise SystemExit("Setup cancelled; existing settings were left unchanged.")

    custom = None
    key = None
    if choice == "custom":
        name, custom = _wizard_custom_provider()
        provider_cfg = {
            "provider": name,
            "base_url": custom["base_url"],
            "api_key": custom.get("api_key") or "custom",
            "model": custom["model"],
        }
    else:
        name = choice
        preset = PRESETS[name]
        stored_key = (cfg.get("api_keys", {}).get(name)
                      or cfg.get("custom_providers", {}).get(name, {}).get("api_key"))
        key = _ask_key(name, preset.get("env_key"), stored_key)
        has_env_key = bool((preset.get("env_key") and os.environ.get(preset["env_key"]))
                           or os.environ.get("NIJI_API_KEY"))
        if preset.get("env_key") and not key and not stored_key and not has_env_key:
            raise SystemExit("No API key entered. Run 'niji setup' again when ready.")
        saved_model = cfg.get("models", {}).get(name)
        saved_model = MODEL_MIGRATIONS.get(name, {}).get(saved_model, saved_model)
        default_m = default_model or saved_model or preset["model"]
        model = Prompt.ask("Default model", default=default_m).strip()
        provider_cfg = resolve_provider(name, model=model, api_key=key)

    with console.status("[cyan]Testing provider connection...[/]"):
        ok, msg = test_connection(provider_cfg)

    if not ok and choice != "custom" and preset.get("env_key") and any(
            code in msg for code in ("401", "403")):
        console.print(Panel(_connection_guidance(provider_cfg, msg),
                            title="[bold yellow]Key or provider access denied[/]",
                            border_style="yellow"))
        replace = Prompt.ask("Replace the saved key and test again?",
                             choices=["y", "n"], default="y")
        if replace == "y":
            replacement = _read_replacement_key(name)
            if replacement:
                key = replacement
                provider_cfg = resolve_provider(name, model=provider_cfg["model"],
                                                api_key=key)
                with console.status("[cyan]Retrying provider connection...[/]"):
                    ok, msg = test_connection(provider_cfg)

    if not ok:
        console.print(Panel(_connection_guidance(provider_cfg, msg),
                            title="[bold red]Niji setup needs attention[/]",
                            border_style="red"))
        console.print("Your existing saved settings were not overwritten by this failed test.")
        console.print("When ready, run: niji setup")
        raise SystemExit("Provider is not ready; chat was not started.")

    if custom is not None:
        cfg.setdefault("custom_providers", {})[name] = custom
        cfg["provider"] = name
    else:
        if key:
            cfg.setdefault("api_keys", {})[name] = key
        cfg.setdefault("custom_providers", {}).pop(name, None)
        cfg["provider"] = name
        cfg.setdefault("models", {})[name] = provider_cfg["model"]
    save_config(cfg)
    console.print(f"[green]✓ Connected[/] — {provider_cfg['provider']}/"
                  f"{provider_cfg['model']} ({msg})")
    return provider_cfg
