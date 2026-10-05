import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".niji"
CONFIG_FILE = CONFIG_DIR / "config.json"
SESSION_DIR = CONFIG_DIR / "sessions"
RUNS_DIR = CONFIG_DIR / "runs"
MCP_FILE = CONFIG_DIR / "mcp.json"
MEMORY_FILE = CONFIG_DIR / "MEMORY.md"

# Any provider with an OpenAI-compatible endpoint works out of the box.
PRESETS = {
    "openai":     {"base_url": "https://api.openai.com/v1",
                   "env_key": "OPENAI_API_KEY", "model": "gpt-5"},
    "openrouter": {"base_url": "https://api.openrouter.ai/api/v1",
                   "env_key": "OPENROUTER_API_KEY", "model": "openai/gpt-4o-mini"},
    "nvidia":     {"base_url": "https://integrate.api.nvidia.com/v1",
                   "env_key": "NVIDIA_API_KEY", "model": "nvidia/nemotron-3.5-lightning-30b-a3b"},
    "anthropic":  {"base_url": "https://api.anthropic.com/v1/",
                   "env_key": "ANTHROPIC_API_KEY", "model": "claude-sonnet-4-5"},
    "gemini":     {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
                   "env_key": "GEMINI_API_KEY", "model": "gemini-2.5-flash"},
    "groq":       {"base_url": "https://api.groq.com/openai/v1",
                   "env_key": "GROQ_API_KEY", "model": "openai/gpt-oss-120b"},
    "deepseek":   {"base_url": "https://api.deepseek.com/v1",
                   "env_key": "DEEPSEEK_API_KEY", "model": "deepseek-chat"},
    "together":   {"base_url": "https://api.together.xyz/v1",
                   "env_key": "TOGETHER_API_KEY",
                   "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo"},
    "ollama":     {"base_url": "http://localhost:11434/v1",
                   "env_key": None, "model": "llama3.1"},
}

# Provider model IDs can be retired while a user's key/config remains unchanged.
# Migrate known retired defaults automatically, without changing explicit
# --model or NIJI_MODEL overrides.
MODEL_MIGRATIONS = {
    "groq": {
        "llama-3.3-70b-versatile": "openai/gpt-oss-120b",
    },
}


def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        pass
    temp_file = CONFIG_FILE.with_suffix(".json.tmp")
    temp_file.write_text(json.dumps(cfg, indent=2))
    try:
        temp_file.chmod(0o600)
    except OSError:
        pass
    temp_file.replace(CONFIG_FILE)
    try:
        CONFIG_FILE.chmod(0o600)
    except OSError:
        pass


def load_mcp_servers(path=None):
    """Load MCP connector servers from mcp.json (or a custom path)."""
    p = Path(path) if path else MCP_FILE
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data.get("servers", data)
    except Exception as e:
        print(f"[niji] warning: could not parse {p}: {e}")
        return {}


def save_mcp_servers(servers):
    """Atomically store MCP configuration with owner-only file permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        CONFIG_DIR.chmod(0o700)
    except OSError:
        pass
    temp_file = MCP_FILE.with_suffix(".json.tmp")
    temp_file.write_text(json.dumps({"servers": servers}, indent=2) + "\n")
    try:
        temp_file.chmod(0o600)
    except OSError:
        pass
    temp_file.replace(MCP_FILE)
    try:
        MCP_FILE.chmod(0o600)
    except OSError:
        pass


def resolve_provider(name=None, model=None, api_key=None):
    cfg = load_config()
    name = name or os.environ.get("NIJI_PROVIDER") or cfg.get("provider")
    if not name:
        name = next((provider for provider, settings in PRESETS.items()
                     if settings.get("env_key") and os.environ.get(settings["env_key"])),
                    "openrouter")

    preset = PRESETS.get(name)

    # custom providers saved via `niji providers add` / setup wizard
    custom = cfg.get("custom_providers", {}).get(name)
    if preset is None and custom:
        env_key = custom.get("env_key")
        return {
            "provider": name,
            "base_url": custom["base_url"],
            "api_key": (api_key or custom.get("api_key")
                        or cfg.get("api_keys", {}).get(name)
                        or (env_key and os.environ.get(env_key))
                        or os.environ.get("NIJI_API_KEY") or "custom"),
            "model": (model or os.environ.get("NIJI_MODEL")
                      or custom.get("model") or "default"),
        }

    # custom provider via env: NIJI_BASE_URL works with ANY name
    custom_base = os.environ.get("NIJI_BASE_URL")
    if preset is None and custom_base:
        return {
            "provider": name,
            "base_url": custom_base,
            "api_key": (api_key or os.environ.get("NIJI_API_KEY")
                        or cfg.get("api_keys", {}).get(name) or "niji"),
            "model": (model or os.environ.get("NIJI_MODEL")
                      or cfg.get("models", {}).get(name) or "default"),
        }

    if preset is None:
        known = ", ".join(PRESETS)
        raise SystemExit(
            f"Unknown provider '{name}'. Options: {known}\n"
            f"Or set NIJI_BASE_URL env var to use any OpenAI-compatible endpoint.")

    # Migrate only a stored known-retired model. Explicit CLI/environment model
    # overrides remain untouched so users retain control.
    configured_model = cfg.get("models", {}).get(name)
    has_model_override = bool(model or os.environ.get("NIJI_MODEL"))
    replacement = MODEL_MIGRATIONS.get(name, {}).get(configured_model)
    if replacement and not has_model_override:
        cfg.setdefault("models", {})[name] = replacement
        save_config(cfg)
        configured_model = replacement

    # Explicit CLI credentials win, then saved wizard config, then environment fallbacks.
    key = (api_key
           or cfg.get("api_keys", {}).get(name)
           or (preset["env_key"] and os.environ.get(preset["env_key"]))
           or os.environ.get("NIJI_API_KEY"))
    if preset["env_key"] and not key:
        raise SystemExit(
            f"API key missing for '{name}'. Either:\n"
            f"  export {preset['env_key']}=sk-...\n"
            f"  niji config set-key {name} sk-...")

    return {
        "provider": name,
        "base_url": preset["base_url"],
        "api_key": key or "ollama",
        "model": (model or os.environ.get("NIJI_MODEL")
                  or configured_model or preset["model"]),
    }
