"""Safe cloud-chat model choices, discovered from the configured provider.

The browser receives model names and IDs only. Provider credentials and request
URLs remain server-side. When model discovery is unavailable, the previously
supported allowlisted Nemotron choices remain available as a safe fallback.
"""
from __future__ import annotations

import json
import os
import re
import time
from functools import lru_cache
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,239}$")
_MAX_CATALOG_BYTES = 1_000_000
_CATALOG_TTL_SECONDS = 300

# Stable aliases keep old clients/runs compatible. Newly discovered provider
# model IDs are passed through only after they are returned by the provider.
_MODEL_CHOICES = {
    "nvidia": (
        {"id": "nemotron-3.5-lightning", "name": "Nemotron 3.5 Lightning",
         "model": "nvidia/nemotron-3.5-lightning-30b-a3b", "default": True},
        {"id": "nemotron-3-super-120b", "name": "Nemotron 3 Super 120B",
         "model": "nvidia/nemotron-3-super-120b-a12b", "default": False},
    ),
}


def _safe_display_name(model_id: str, item: dict[str, Any]) -> str:
    """Produce a readable model-only label without its vendor/provider prefix."""
    name = item.get("name")
    if isinstance(name, str) and name.strip() and len(name.strip()) <= 160:
        candidate = name.strip().rsplit("/", 1)[-1]
    else:
        candidate = model_id.rsplit("/", 1)[-1]
    # Remove control characters and normalize separators; do not display the
    # provider's `owned_by` field or any endpoint metadata.
    candidate = "".join(char for char in candidate if char.isprintable()).strip()
    candidate = re.sub(r"[-_]+", " ", candidate)
    return candidate[:160] or model_id.rsplit("/", 1)[-1]


@lru_cache(maxsize=8)
def _discover_provider_models(provider: str, base_url: str, api_key: str) -> tuple[float, tuple[dict[str, Any], ...]]:
    """Read and cache the provider's authenticated OpenAI-compatible model list."""
    if provider != "nvidia" or not api_key:
        return time.monotonic(), ()
    try:
        parts = urlsplit(base_url)
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
            return time.monotonic(), ()
        path = parts.path.rstrip("/") + "/models"
        url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        request = Request(url, headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": "Niji-Cloud/1 model-catalog",
        }, method="GET")
        with urlopen(request, timeout=3.0) as response:
            raw = response.read(_MAX_CATALOG_BYTES + 1)
        if len(raw) > _MAX_CATALOG_BYTES:
            return time.monotonic(), ()
        payload = json.loads(raw.decode("utf-8"))
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            return time.monotonic(), ()
        models: dict[str, dict[str, Any]] = {}
        default_model = os.environ.get("NIJI_CLOUD_MODEL", "").strip()
        for item in entries[:500]:
            if not isinstance(item, dict):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
                continue
            models[model_id] = {
                "id": model_id,
                "name": _safe_display_name(model_id, item),
                "default": model_id == default_model,
            }
        if default_model in models and not any(item["default"] for item in models.values()):
            models[default_model]["default"] = True
        ordered = tuple(sorted(models.values(), key=lambda item: item["name"].casefold()))
        return time.monotonic(), ordered
    except (HTTPError, URLError, TimeoutError, OSError, UnicodeError, ValueError, TypeError):
        # Discovery is best-effort. Never pass provider error text to users/logs;
        # retain fallback choices rather than breaking the chat interface.
        return time.monotonic(), ()


def _provider_settings() -> tuple[str, str, str]:
    provider = os.environ.get("NIJI_CLOUD_PROVIDER", "nvidia").strip().lower()
    default_url = "https://integrate.api.nvidia.com/v1" if provider == "nvidia" else ""
    base_url = os.environ.get("NIJI_CLOUD_BASE_URL", "").strip() or default_url
    api_key = os.environ.get("NIJI_CLOUD_PROVIDER_API_KEY", "")
    return provider, base_url, api_key


def _available_models(provider: str, base_url: str, api_key: str) -> tuple[dict[str, Any], ...]:
    stamp, models = _discover_provider_models(provider, base_url, api_key)
    if time.monotonic() - stamp > _CATALOG_TTL_SECONDS:
        _discover_provider_models.cache_clear()
        _, models = _discover_provider_models(provider, base_url, api_key)
    return models


def public_model_catalog() -> list[dict[str, Any]]:
    """Return provider model names and safe IDs, without endpoint or secret data."""
    provider, base_url, api_key = _provider_settings()
    discovered = _available_models(provider, base_url, api_key)
    if discovered:
        configured_default = os.environ.get("NIJI_CLOUD_MODEL", "").strip()
        if configured_default and any(item["id"] == configured_default for item in discovered):
            return [{**item, "default": item["id"] == configured_default} for item in discovered]
        return [dict(item) for item in discovered]
    return [
        {"id": choice["id"], "name": choice["name"], "default": choice["default"]}
        for choice in _MODEL_CHOICES.get(provider, ())
    ]


def is_known_cloud_model(model: Any) -> bool:
    """Validate a UI model choice against live discovery or legacy safe aliases."""
    if not isinstance(model, str):
        return False
    if any(choice["id"] == model for choices in _MODEL_CHOICES.values() for choice in choices):
        return True
    provider, base_url, api_key = _provider_settings()
    return any(item["id"] == model for item in _available_models(provider, base_url, api_key))


def resolve_cloud_model(model: str, provider: str) -> str | None:
    """Resolve a safe model ID for the configured provider only."""
    for choice in _MODEL_CHOICES.get(provider, ()):
        if choice["id"] == model:
            return choice["model"]
    configured_provider, base_url, api_key = _provider_settings()
    if provider != configured_provider:
        return None
    if any(item["id"] == model for item in _available_models(provider, base_url, api_key)):
        return model
    return None
