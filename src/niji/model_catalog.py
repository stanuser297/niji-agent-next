"""Best-effort model discovery for OpenAI-compatible providers."""
import os

from .config import PRESETS, load_config, resolve_provider


def provider_names(config=None):
    cfg = config if config is not None else load_config()
    names = list(PRESETS)
    names.extend(sorted(name for name in cfg.get("custom_providers", {})
                        if name not in PRESETS))
    return names


def provider_is_configured(name, config=None):
    cfg = config if config is not None else load_config()
    custom = cfg.get("custom_providers", {}).get(name)
    preset = PRESETS.get(name)
    if preset:
        env_key = preset.get("env_key")
        if not env_key:
            models = cfg.get("models", {})
            if not isinstance(models, dict):
                models = {}
            return (name == cfg.get("provider") or bool(os.environ.get("NIJI_BASE_URL"))
                    or bool(models.get(name)))
        generic_key_provider = os.environ.get("NIJI_PROVIDER") or cfg.get("provider")
        return bool(cfg.get("api_keys", {}).get(name)
                    or os.environ.get(env_key)
                    or (os.environ.get("NIJI_API_KEY") and generic_key_provider == name))
    if custom:
        generic_key_provider = os.environ.get("NIJI_PROVIDER") or cfg.get("provider")
        return bool(custom.get("api_key")
                    or cfg.get("api_keys", {}).get(name)
                    or (custom.get("env_key") and os.environ.get(custom["env_key"]))
                    or (os.environ.get("NIJI_API_KEY") and generic_key_provider == name)
                    or name == cfg.get("provider"))
    return False


def resolve_catalog_provider(name):
    """Resolve a saved provider without exposing credentials in errors."""
    try:
        return resolve_provider(name), None
    except SystemExit:
        return None, "Provider is not configured yet; run `/setup` to connect it."
    except Exception:
        return None, "Could not resolve this provider configuration. Run `/doctor`."


def fetch_provider_models(provider_cfg, timeout=15):
    """Return (model_ids, message); model listing is optional/provider-dependent."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=provider_cfg["api_key"],
                        base_url=provider_cfg["base_url"], timeout=timeout,
                        max_retries=0)
        response = client.models.list()
        models = sorted({str(item.id) for item in response.data
                         if getattr(item, "id", None)})
        active = provider_cfg.get("model")
        if active and active not in models:
            models.insert(0, active)
        if not models:
            return [], "The provider returned an empty model list; enter a model ID manually."
        return models, ""
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            return [], f"Model catalog denied access (HTTP {status}); check `/setup` and the provider key."
        if status == 404:
            return [], "This provider does not expose a compatible models endpoint; enter a model ID manually."
        if isinstance(exc, TimeoutError) or "timeout" in exc.__class__.__name__.lower():
            return [], "Model catalog request timed out; enter a model ID manually or retry later."
        return [], f"Could not fetch the model catalog ({exc.__class__.__name__}); enter a model ID manually."
