import argparse
import json
import re
import sys
import time
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.markup import escape
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from . import __version__
from .ui import render_home
from .config import (CONFIG_DIR, CONFIG_FILE, MCP_FILE, PRESETS, SESSION_DIR,
                     load_config, load_mcp_servers, resolve_provider,
                     save_config, save_mcp_servers)
from .model_catalog import (fetch_provider_models, provider_is_configured,
                            provider_names, resolve_catalog_provider)
from .planning import load_plan
from .terminal import OUTPUT_LOCK


def _build_agent(args, mcp_path=None):
    from .agent import Agent
    from .mcp import connect_all
    provider = resolve_provider(getattr(args, "provider", None),
                                getattr(args, "model", None),
                                getattr(args, "api_key", None))
    clients = [] if getattr(args, "no_mcp", False) or mcp_path == "skip" \
        else connect_all(load_mcp_servers(mcp_path or getattr(args, "mcp", None)))
    agent = Agent(provider,
                  approval="ask" if getattr(args, "ask", False) else "auto",
                  max_turns=getattr(args, "max_turns", 20),
                  max_tool_calls=getattr(args, "max_tool_calls", 30),
                  max_tool_calls_per_turn=getattr(args, "max_tool_calls_per_turn", 6),
                  verbose=not getattr(args, "quiet", False),
                  mcp_clients=clients)
    if not getattr(args, "quiet", False):
        agent.activity_callback = _activity_notice
    return agent, provider


def _activity_notice(event):
    """Render concise execution phases, never private model reasoning."""
    level = event.get("level")
    message = event.get("message", "")
    with OUTPUT_LOCK:
        console = Console()
        if level == "THINKING":
            console.print(f"[dim bright_cyan]↻ {message}[/]")
        elif level == "RETRY":
            console.print(f"[yellow]↻ {message}[/]")
        elif level == "LIMIT":
            console.print(f"[bold yellow]⏸ {message}[/]")
        elif level == "PLAN":
            console.print(f"[bold cyan]⚙ {message}[/]")
        elif level == "COMPACT":
            console.print(f"[bold yellow]↘ {message}[/]")
        elif level == "TOOL":
            console.print(f"[bold cyan]⚒ {message}[/]")
        elif level == "TOOL_PROGRESS":
            console.print(f"[dim cyan]↳ {message}[/]")
        elif level == "TOOL_DONE":
            console.print(f"[green]✓ {message}[/]")
        elif level == "CHECKPOINT":
            console.print(f"[dim cyan]↶ {message}[/]")
        elif level == "UNDO":
            console.print(f"[bold green]↶ {message}[/]")
        elif level == "DENIED":
            console.print(f"[yellow]⊘ {message}[/]")
        elif level == "ERROR":
            console.print(f"[bold red]✗ {message}[/]")
        elif level == "DONE":
            console.print("[dim green]✓ Response complete[/]")


# ---------------- provider management ----------------

def _cmd_providers(argv):
    cfg = load_config()
    custom = cfg.get("custom_providers", {})
    if len(argv) >= 2 and argv[1] == "add":
        _provider_add()
        return True
    if len(argv) >= 3 and argv[1] == "remove":
        name = argv[2]
        changed = False
        for section in ("custom_providers", "api_keys", "models"):
            if name in cfg.get(section, {}):
                del cfg[section][name]
                changed = True
        if cfg.get("provider") == name:
            cfg.pop("provider", None)
            changed = True
        save_config(cfg)
        print(f"[{'ok' if changed else 'no change'}] provider '{name}' removed")
        return True
    if len(argv) >= 3 and argv[1] == "use":
        name = argv[2]
        if name not in PRESETS and name not in custom:
            print(f"unknown provider '{name}' — see: niji providers")
            return True
        cfg["provider"] = name
        save_config(cfg)
        print(f"[ok] default provider = {name}")
        return True
    if len(argv) == 1 or argv[1] == "list":
        default = cfg.get("provider", "(none)")
        print(f"{'NAME':14s} {'MODEL':40s} KEY")
        for name, p in PRESETS.items():
            state = ("env:" + p["env_key"]) if p["env_key"] else "no key needed"
            if cfg.get("api_keys", {}).get(name):
                state = "stored"
            tag = "*" if name == default else " "
            print(f"{tag}{name:13s} {p['model']:40s} {state}")
        for name, c in custom.items():
            tag = "*" if name == default else " "
            print(f"{tag}{name:13s} {c.get('model', 'default'):40s} "
                  f"custom {c['base_url']}")
        print("\n* = default. Manage: niji providers add | use <name> | remove <name>")
        return True
    print("usage: niji providers [list] | add | use <name> | remove <name>")
    return True


def _cmd_models(argv):
    """List provider model catalogs; unconfigured providers are clearly marked."""
    cfg = load_config()
    requested = argv[2] if len(argv) >= 3 else None
    names = provider_names(cfg)
    if requested:
        if requested not in names:
            print(f"Unknown provider '{requested}'. See: niji providers")
            return True
        names = [requested]

    console = Console()
    for name in names:
        preset = PRESETS.get(name, {})
        custom = cfg.get("custom_providers", {}).get(name, {})
        active = cfg.get("models", {}).get(name) or custom.get("model") or preset.get("model", "(manual)")
        console.print(f"\n[bold cyan]{name}[/]" + (" [green](default)[/]" if cfg.get("provider") == name else ""))
        if not provider_is_configured(name, cfg):
            console.print(f"  [dim]Not connected. Preset model: {active}. Run `niji setup` to connect.[/]")
            continue
        resolved, error = resolve_catalog_provider(name)
        if error:
            console.print(f"  [yellow]{error}[/]")
            continue
        active = resolved.get("model", active)
        models, message = fetch_provider_models(resolved)
        if not models:
            console.print(f"  [yellow]{message}[/]")
            console.print(f"  [dim]Configured model: {active}[/]")
            continue
        table = Table(show_header=True, header_style="bold")
        table.add_column("Model ID", style="white")
        table.add_column("State", style="green")
        for model_id in models:
            table.add_row(escape(model_id), "active" if model_id == active else "")
        console.print(table)
    if not requested:
        console.print("\n[dim]Catalogs require a connected provider key and a compatible `/models` endpoint. "
                      "Use `niji models <provider>` to inspect one; `/model` to switch.[/]")
    return True


def _activate_model(agent, provider, name, model_id):
    """Validate a selected model, then persist and switch the live session."""
    try:
        provider_cfg = resolve_provider(name, model=model_id)
    except SystemExit as exc:
        Console().print(f"[yellow]{exc}[/]")
        Console().print("Run `/setup` to connect this provider first.")
        return False
    from .setup_wizard import _connection_guidance, test_connection
    with Console().status(f"[cyan]Testing {name}/{model_id}...[/]"):
        ok, message = test_connection(provider_cfg)
    verified = ok
    if not ok:
        Console().print(Panel(_connection_guidance(provider_cfg, message),
                              title="Model chat check did not pass", border_style="yellow"))
        match = re.search(r"\b(401|403|404)\b", message)
        if match:
            current = f"{agent.provider_name}/{agent.model}"
            Console().print(f"[bold yellow]Model was NOT switched. Niji is still using {current}.[/]")
            if match.group(1) in ("401", "403"):
                Console().print("[dim]Fix/re-enter the selected provider key or confirm model access with `/setup`, then try `/model` again.[/]")
            else:
                Console().print("[dim]Choose a valid model ID in `/model`, then try again.[/]")
            return False
        apply_anyway = Prompt.ask(
            "Switch to this model without a successful chat test?",
            choices=["y", "n"], default="n")
        if apply_anyway != "y":
            Console().print("[dim]No changes made; current model remains active.[/]")
            return False

    cfg = load_config()
    cfg["provider"] = name
    if name in PRESETS:
        cfg.setdefault("models", {})[name] = model_id
    elif name in cfg.get("custom_providers", {}):
        cfg["custom_providers"][name]["model"] = model_id
    save_config(cfg)

    from openai import OpenAI
    agent.client = OpenAI(api_key=provider_cfg["api_key"],
                          base_url=provider_cfg["base_url"],
                          timeout=120, max_retries=0)
    agent.model = model_id
    agent.provider_name = name
    agent.provider_cfg = provider_cfg
    provider.clear()
    provider.update(provider_cfg)
    agent.messages.append({"role": "system", "content":
                           f"The active model was changed to {name}/{model_id}. Continue the same task and conversation."})
    if verified:
        Console().print(f"[green]✓ Switched to {name}/{model_id}[/] — chat test passed")
    else:
        Console().print(f"[yellow]Switched to {name}/{model_id} (not verified).[/] Send a short test prompt; use `/model` to switch back if needed.")
    _render_home(agent, provider)
    return True


def _arrow_select(title, choices, selected=0):
    """Compatibility wrapper for the shared unbuffered, Termux-safe picker."""
    from .terminal_picker import arrow_select
    return arrow_select(title, choices, selected)


def _interactive_model_picker(agent, provider, provider_name=None, requested_model=None):
    """Browse providers and their live model lists, with manual-ID fallback."""
    cfg = load_config()
    names = provider_names(cfg)
    console = Console()
    name = provider_name
    if name is None:
        choices = []
        for candidate in names:
            preset = PRESETS.get(candidate, {})
            custom = cfg.get("custom_providers", {}).get(candidate, {})
            status = "connected" if provider_is_configured(candidate, cfg) else "setup needed"
            default_model = cfg.get("models", {}).get(candidate) or custom.get("model") or preset.get("model", "")
            marker = " · current" if candidate == provider.get("provider") else ""
            choices.append((candidate, f"{candidate} · {status} · {default_model}{marker}"))
        current = provider.get("provider")
        selected = names.index(current) if current in names else 0
        name = _arrow_select("Choose a provider", choices, selected)
        if name is None:
            console.print("[dim]Provider selection cancelled.[/]")
            return
    elif name not in names:
        console.print(f"[yellow]Unknown provider '{name}'. Use `/providers` to see options.[/]")
        return

    provider_cfg, error = resolve_catalog_provider(name)
    if error:
        console.print(f"[yellow]{name}: {error}[/]")
        return

    model_ids, catalog_message = fetch_provider_models(provider_cfg)
    if requested_model is None and model_ids:
        choices = []
        for model_id in model_ids:
            if name == agent.provider_name and model_id == agent.model:
                marker = " · active session model"
            elif model_id == provider_cfg.get("model"):
                marker = " · saved for this provider"
            else:
                marker = ""
            choices.append((model_id, model_id + marker))
        choices.append(("__manual__", "Enter a model ID manually…"))
        selected = next((i for i, model_id in enumerate(model_ids)
                         if model_id == provider_cfg.get("model")), 0)
        selected = _arrow_select(f"{name}: choose a model", choices, selected)
        if selected is None:
            console.print("[dim]Model selection cancelled.[/]")
            return
        if selected == "__manual__":
            requested_model = Prompt.ask("Model ID").strip()
        else:
            requested_model = selected
    elif requested_model is None:
        console.print(f"[yellow]{catalog_message}[/]")
        requested_model = Prompt.ask("Enter model ID manually", default=provider_cfg["model"]).strip()

    if not requested_model:
        console.print("[yellow]No model ID entered; no changes made.[/]")
        return
    _activate_model(agent, provider, name, requested_model)


def _provider_add():
    from .setup_wizard import _wizard_custom_provider
    cfg = load_config()
    custom = cfg.setdefault("custom_providers", {})
    name, settings = _wizard_custom_provider()
    if name in PRESETS or name in custom:
        print(f"'{name}' already exists")
        return
    custom[name] = settings
    cfg["provider"] = name
    save_config(cfg)
    print(f"[ok] provider '{name}' added and set as default")


# ---------------- MCP connectors ----------------

def _cmd_connectors(argv):
    """Manage trusted local and Nango authenticated MCP connectors."""
    parser = argparse.ArgumentParser(prog="niji connectors",
                                     description="Connect Nango integrations or manage MCP servers")
    sub = parser.add_subparsers(dest="action")
    add = sub.add_parser("add", help="Add a connector")
    add.add_argument("provider", choices=["nango"], help="Connector platform")
    add.add_argument("--name", help="Local connector name (used as a tool prefix)")
    sub.add_parser("list", help="List configured connectors")
    test = sub.add_parser("test", help="Connect and check discovered tools")
    test.add_argument("name", nargs="?", help="Connector name (defaults to all)")
    remove = sub.add_parser("remove", help="Remove a connector configuration")
    remove.add_argument("name", help="Connector name")
    remove.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    args = parser.parse_args(argv)
    if not args.action:
        parser.print_help()
        print("\nQuick start: niji connectors add nango")
        return

    servers = load_mcp_servers()
    if not isinstance(servers, dict):
        print("[error] ~/.niji/mcp.json must contain a JSON object")
        return

    if args.action == "list":
        if not servers:
            print("No connectors configured. Add one with: niji connectors add nango")
            return
        for name, cfg in servers.items():
            transport = str(cfg.get("transport", "stdio")) if isinstance(cfg, dict) else "invalid"
            print(f"  {name} · {transport}")
        return

    if args.action == "add":
        print("Create a Nango integration and authorize its account at https://app.nango.dev/ first.")
        print("You will need its API key, provider-config/integration ID, and authorized connection ID.")
        if not sys.stdin.isatty():
            print("[error] Nango setup needs an interactive terminal. Run: niji connectors add nango")
            return
        from getpass import getpass
        import re as _re
        try:
            api_key = getpass("Nango API key (input hidden): ").strip()
        except Exception:
            print("[note] Hidden entry unavailable; type/paste the key visibly.")
            api_key = input("Nango API key: ").strip()
        if not api_key:
            print("[error] API key cannot be empty")
            return
        integration = input("Provider-config key / integration ID: ").strip()
        connection = input("Authorized connection ID: ").strip()
        if not integration or not connection:
            print("[error] Both integration ID and connection ID are required")
            return
        default_name = "nango_" + _re.sub(r"[^A-Za-z0-9_]", "_", integration)[:18]
        name = (args.name or input(f"Local name [{default_name}]: ").strip() or default_name).strip()
        if not _re.fullmatch(r"[A-Za-z0-9_]{1,32}", name):
            print("[error] Name must be 1–32 letters, digits, or underscores")
            return
        if name in servers:
            print(f"[error] Connector '{name}' already exists; choose a different --name")
            return
        servers[name] = {
            "transport": "http",
            "url": "https://api.nango.dev/proxy/v2/mcp",
            "api_key": api_key,
            "provider_config_key": integration,
            "connection_id": connection,
        }
        save_mcp_servers(servers)
        print(f"[ok] Nango connector '{name}' saved in ~/.niji/mcp.json with private file permissions")
        print("Restart Niji to load it. Check access with: niji connectors test " + name)
        print("Nango Free has usage limits; review current plan limits in your Nango dashboard.")
        return

    if args.action == "remove":
        if args.name not in servers:
            print(f"[error] No connector named '{args.name}'")
            return
        if not args.yes:
            answer = input(f"Remove '{args.name}' and its saved credentials? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("Cancelled")
                return
        del servers[args.name]
        save_mcp_servers(servers)
        print(f"[ok] Removed connector '{args.name}'")
        return

    from .mcp import HttpMCPServer, MCPServer
    targets = ({args.name: servers[args.name]} if args.name in servers
               else ({} if args.name else servers))
    if args.name and args.name not in servers:
        print(f"[error] No connector named '{args.name}'")
        return
    if not targets:
        print("No connectors configured.")
        return
    ok = True
    for name, cfg in targets.items():
        client = None
        try:
            transport = str(cfg.get("transport", "stdio")).lower()
            client = (HttpMCPServer(name, cfg) if transport in ("http", "streamable-http", "nango")
                      else MCPServer(name, cfg))
            client.start()
            print(f"[ok] {name}: connected; {len(client.tools)} tools discovered")
        except Exception as exc:
            ok = False
            print(f"[error] {name}: connection failed ({type(exc).__name__}). Check credentials, IDs, and network.")
        finally:
            if client:
                client.stop()
    if not ok:
        print("If this is Nango, confirm the API key, integration/provider-config key, and connection ID in Nango.")


# ---------------- doctor ----------------

def _cmd_doctor():

    from .setup_wizard import test_connection
    print("niji doctor — checking your setup\n")
    ok_all = True

    def check(label, ok, fix=""):
        nonlocal ok_all
        ok_all = ok_all and ok
        print(f"  {'✓' if ok else '✗'} {label}" + (f"  → {fix}" if not ok and fix else ""))

    check("config file exists", CONFIG_FILE.exists(),
          "run: niji setup")
    cfg = load_config()
    name = cfg.get("provider")
    check("default provider set", bool(name),
          "run: niji providers use <name>")
    if name:
        try:
            p = resolve_provider()
            preset = PRESETS.get(name)
            if preset and preset.get("env_key"):
                check(f"API key for '{name}'", bool(p["api_key"] and p["api_key"] not in ("custom",)),
                      f"run: niji config set-key {name} sk-...")
            ok, msg = test_connection(p)
            check(f"connection ({p['model']})", ok, msg)
        except SystemExit as e:
            check(f"resolve provider '{name}'", False, str(e))
    mcp_valid = True
    mcp = {}
    if MCP_FILE.exists():
        try:
            raw_mcp = json.loads(MCP_FILE.read_text())
            mcp = raw_mcp.get("servers", raw_mcp) if isinstance(raw_mcp, dict) else None
            mcp_valid = isinstance(mcp, dict)
        except Exception:
            mcp_valid = False
    check("mcp.json valid", mcp_valid,
          "check ~/.niji/mcp.json syntax and ensure it contains an object")
    if mcp_valid and mcp:
        print(f"  • {len(mcp)} MCP connector(s) configured")
    print("\n" + ("[ok] all good — happy coding!" if ok_all else "[!] fix the items above"))
    return ok_all


# ---------------- sessions ----------------

def _list_sessions(query=""):
    if not SESSION_DIR.exists():
        print("no sessions yet")
        return
    files = sorted(SESSION_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    query = str(query or "").strip().casefold()
    shown = 0
    for f in files:
        try:
            msgs = json.loads(f.read_text())
            user_msgs = [str(m.get("content", "")) for m in msgs
                         if m.get("role") == "user"]
            first_user = (user_msgs[0] if user_msgs else "?").replace("\n", " ")
            haystack = (f.stem + " " + " ".join(user_msgs)).casefold()
            if query and query not in haystack:
                continue
            if query:
                first_user = next((msg.replace("\n", " ") for msg in user_msgs
                                   if query in msg.casefold()), first_user)
        except Exception:
            first_user = "?"
            if query:
                continue
        print(f"  {f.stem}  ({f.stat().st_size // 1024} KB)  {first_user[:80]}")
        shown += 1
        if shown >= 20:
            break
    if shown == 0:
        suffix = f" matching {query!r}" if query else ""
        print(f"no sessions found{suffix}")


def _undo_last_change(agent):
    change = agent.latest_file_change()
    if not change:
        Console().print("[dim]No reversible file changes in this session.[/]")
        return
    Console().print(Panel(
        f"[bold]{change['operation']}[/] · {change['path']}\n"
        "Undo is allowed only if the file still matches Niji's last saved version.",
        title="Undo latest file change?", border_style="yellow"))
    if Prompt.ask("Restore the previous contents?", choices=["y", "n"], default="n") != "y":
        Console().print("[dim]No changes made.[/]")
        return
    result = agent.undo_last_file_change()
    style = "green" if result["ok"] else "red"
    Console().print(f"[{style}]{result['message']}[/]")


# ---------------- interactive terminal UI ----------------


def _render_home(agent, provider, quiet=False):
    render_home(agent, provider, quiet=quiet)


def _show_context(agent):
    from .compaction import estimate_tokens
    messages = list(getattr(agent, "messages", []) or [])
    table = Table(title="Prompt context estimate", show_header=True, header_style="bold cyan")
    table.add_column("Role", style="green")
    table.add_column("Msgs", justify="right")
    table.add_column("Chars", justify="right")
    table.add_column("≈ Tokens", justify="right")
    totals = {}
    for message in messages:
        role = str(message.get("role", "other"))
        content = message.get("content") or ""
        chars = len(str(content))
        if message.get("tool_calls"):
            chars += len(json.dumps(message["tool_calls"], default=str))
        row = totals.setdefault(role, [0, 0])
        row[0] += 1
        row[1] += chars
    for role, (count, chars) in sorted(totals.items()):
        table.add_row(role, str(count), f"{chars:,}", f"~{chars // 4:,}")
    table.add_row("TOTAL", str(len(messages)),
                  f"{sum(row[1] for row in totals.values()):,}",
                  f"~{estimate_tokens(messages):,}")
    Console().print(table)
    Console().print("[dim]Approximation from message text only; provider tokenizers, tool schemas, and request overhead differ.[/]")


def _show_activity(agent):
    table = Table(title="Niji activity feed", show_header=True, header_style="bold cyan")
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Phase", style="bold", no_wrap=True)
    table.add_column("Event", overflow="fold")
    events = list(getattr(agent, "activity", []))[-20:]
    if not events:
        table.add_row("—", "INFO", "No activity yet")
    for event in events:
        table.add_row(str(event.get("time", "—")), str(event.get("level", "INFO")),
                      str(event.get("message", "")))
    Console().print(table)


def _show_help():
    console = Console()
    table = Table(title="Niji commands", show_header=True, header_style="bold cyan")
    table.add_column("Command", style="green", no_wrap=True)
    table.add_column("What it does")
    rows = [
        ("/help", "Show this command list"),
        ("/status", "Show provider, model, workspace and usage"),
        ("/tools", "List built-in and connected MCP tools"),
        ("/skills", "List reusable project and user SKILL.md workflows"),
        ("/activity", "Show recent thinking/execution phases and tool outcomes"),
        ("/limits", "Show turn/tool-call limits for this request"),
        ("/context", "Show approximate prompt size and message breakdown for 413 debugging"),
        ("/model", "Browse providers and their available models; switch for this session"),
        ("/models", "List model catalogs for configured providers"),
        ("/approval", "Toggle auto/ask confirmation mode for tool execution"),
        ("/cost", "Show token usage so far"),
        ("/compact", "Summarize older context to free space"),
        ("/memory [show|add <note>|clear]", "View or manage long-term memory (never store secrets)"),
        ("/undo", "Safely restore the last Niji file write/edit in this session"),
        ("/sessions [search words]", "List or search saved chat sessions"),
        ("/setup", "Reconnect/switch provider and model"),
        ("/providers", "List provider choices"),
        ("/doctor", "Test the saved provider and setup"),
        ("/sessions", "List saved sessions"),
        ("/clear", "Clear the screen and redraw the home panel"),
        ("/exit", "Save and leave Niji"),
    ]
    for command, description in rows:
        table.add_row(command, description)
    console.print(table)
    console.print("[dim]Or just type what you want Niji to do.[/]")


def _show_provider_error(provider, exc):
    """Give a safe, actionable recovery path without dumping a traceback or key."""
    console = Console(stderr=True)
    status = getattr(exc, "status_code", None)
    provider_name = str(provider.get("provider", ""))
    base_url = str(provider.get("base_url", ""))
    groq = provider_name == "groq" or "api.groq.com" in base_url
    message = str(exc)[:500]
    api_key = str(provider.get("api_key", ""))
    if api_key:
        message = message.replace(api_key, "[redacted]")
    lower = message.lower()
    if status == 404 and (provider_name == "nvidia" or "nvidia.com" in base_url):
        detail = ("NVIDIA returned 404. Verify the model ID and base URL. "
                  "Niji's tested chat default is `nvidia/nemotron-3.5-lightning-30b-a3b`; "
                  "use `/model` or `/setup` to choose another NVIDIA model.")
    elif status == 404 and groq:
        model = str(provider.get("model", "(unknown)"))
        if model == "llama-3.3-70b-versatile":
            detail = ("Groq returned 404. `llama-3.3-70b-versatile` was retired for developer/free accounts "
                      "on August 16, 2026. Choose an active model (for example `openai/gpt-oss-120b`) "
                      "from `/model` or run `/setup`. Keep base URL `https://api.groq.com/openai/v1`.")
        else:
            detail = (f"Groq returned 404 for `{model}`. Check the exact model ID and base URL; "
                      "use `/model` to choose an active chat model or `/setup` to change the endpoint.")
    elif status == 404:
        detail = "HTTP 404: provider route or model ID not found. Check `/setup` base URL and choose a model in `/model`; do not retry the same ID blindly."
    elif status in (401, 403) and groq:
        detail = (f"Groq rejected the API key or account access (HTTP {status}). Replace it with a fresh GroqCloud API key in `/setup` "
                  "and confirm account/model access. Niji will not display the saved key.")
    elif status == 401:
        detail = "HTTP 401: API key is missing, invalid, or revoked. Run `/setup` and enter a key for this provider; retry only after updating it."
    elif status == 403:
        detail = "HTTP 403: key/account lacks permission for this model or feature. Check provider eligibility/region, then use `/model` for an allowed model. Re-entering the same key usually will not help."
    elif status in (400, 422):
        detail = "The provider rejected the request (HTTP %s). This is usually an unsupported model/request option. Choose a chat/text model with `/model`; do not auto-retry the same request." % status
    elif status == 413:
        detail = ("Request too large (HTTP 413). Niji now tries one bounded automatic trim of older chat context. "
                  "If this still appears, run `/compact`, shorten the current prompt or large pasted output, "
                  "or switch to a model with a larger context window. A single oversized current message cannot be compacted away.")
    elif status == 429 and any(word in lower for word in ("quota", "billing", "credit", "payment", "insufficient_balance")):
        detail = "Provider quota/credit is exhausted (HTTP 429); waiting/retrying will not fix billing. Check account credits/limits or switch provider with `/model`."
    elif status == 429:
        detail = "Rate limit reached (HTTP 429). Niji made at most one bounded retry. Wait for the provider reset, reduce requests, or choose another model/provider with `/model`."
    elif status in (408, 409) or (status is not None and 500 <= status <= 599):
        detail = (f"Provider transient failure (HTTP {status}). Niji made at most one bounded retry. "
                  "Check connection/provider status, then retry once; use `/model` to switch if it persists.")
    elif any(token in exc.__class__.__name__.lower() for token in
             ("connectionerror", "apiconnectionerror", "timeouterror", "apitimeouterror", "connecterror", "readtimeout")):
        detail = ("Network/DNS/timeout error while contacting the model. The failed prompt was not kept as a dangling chat turn. "
                  "Check internet with `/doctor`, then retry once; don't keep retrying while offline.")
    else:
        detail = f"Request failed ({exc.__class__.__name__}). The interactive session stays open. Run `/doctor`, then `/setup` or `/model` if needed."
        if message and not api_key:
            detail += "\nProvider detail: " + message.replace("\n", " ")[:180]
    console.print(Panel(Text(detail), title="Request failed — chat is still open",
                        border_style="red"))


def _interactive_chat(agent, provider, quiet=False):
    console = Console()
    # Show the full command-center dashboard above the pinned composer at launch.
    # /status can redraw it whenever needed during the session.
    _render_home(agent, provider, quiet=quiet)
    while True:
        try:
            from .chat_prompt import read_chat_prompt
            user = read_chat_prompt(agent, provider)
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Niji saved. See you next time.[/]")
            break
        if user is None:
            console.print("[dim]Niji saved. See you next time.[/]")
            break
        user = user.strip()
        if not user:
            continue
        if user.lower() in ("/exit", "exit", "quit", "/quit"):
            break
        if user == "/help":
            _show_help()
            continue
        if user in ("/model", "/provider"):
            _interactive_model_picker(agent, provider)
            continue
        if user == "/models":
            _cmd_models(["niji", "models"])
            continue
        if user.startswith("/model "):
            parts = user.split(maxsplit=2)
            if len(parts) >= 2 and parts[1].lower() == "list":
                _cmd_models(["niji", "models", parts[2] if len(parts) > 2 else provider["provider"]])
            elif len(parts) >= 2:
                _interactive_model_picker(agent, provider, parts[1],
                                          parts[2] if len(parts) > 2 else None)
            continue
        if user == "/approval" or user.startswith("/approval "):
            parts = user.split(maxsplit=1)
            if len(parts) == 1:
                agent.approval = "ask" if agent.approval != "ask" else "auto"
            elif parts[1].strip().lower() in ("ask", "auto"):
                agent.approval = parts[1].strip().lower()
            else:
                console.print("Usage: /approval [ask|auto]")
                continue
            if agent.approval == "ask":
                console.print("[yellow]Approval mode: ask — tool actions need confirmation. This is not a sandbox.[/]")
            else:
                console.print("[green]Approval mode: auto — tools can execute actions without per-action confirmation.[/]")
            continue
        if user == "/status":
            _render_home(agent, provider, quiet=False)
            console.print(agent.cost_line())
            continue
        if user == "/tools":
            names = [schema.get("function", {}).get("name", "tool")
                     for schema in agent.tool_schemas]
            console.print(Panel(Text("\n".join(names) or "No tools available"),
                                title=f"Available tools ({len(names)})", border_style="blue"))
            continue
        if user == "/skills":
            skills = getattr(agent, "skills", {})
            table = Table(title=f"Reusable skills ({len(skills)})", show_header=True,
                          header_style="bold cyan")
            table.add_column("Name", style="green")
            table.add_column("Description")
            for name, meta in sorted(skills.items()):
                table.add_row(name, meta.get("description", "Reusable workflow"))
            if not skills:
                console.print("No SKILL.md files found. Add them under .agents/skills/<name>/SKILL.md or ~/.niji/skills/<name>/SKILL.md.")
            else:
                console.print(table)
            continue
        if user == "/activity":
            _show_activity(agent)
            continue
        if user == "/limits":
            table = Table(title="Execution limits", show_header=False)
            table.add_column("Limit", style="cyan")
            table.add_column("Value", style="white")
            table.add_row("Turns per request", str(agent.max_turns))
            table.add_row("Tool calls per request", f"{agent._request_tool_calls}/{agent.max_tool_calls}")
            table.add_row("Tool calls per model turn", str(agent.max_tool_calls_per_turn))
            table.add_row("Shell command timeout", "max 120 seconds")
            table.add_row("Provider retry", "one retry, transient errors only")
            Console().print(table)
            continue
        if user == "/context":
            _show_context(agent)
            continue
        if user == "/cost":
            console.print(agent.cost_line())
            continue
        if user == "/undo":
            _undo_last_change(agent)
            continue
        if user == "/memory" or user.startswith("/memory "):
            parts = user.split(maxsplit=2)
            action = parts[1].lower() if len(parts) > 1 else "show"
            from .config import MEMORY_FILE
            if action in ("show", "list"):
                content = MEMORY_FILE.read_text(errors="replace") if MEMORY_FILE.exists() else "Memory is empty."
                console.print(Panel(Text(content), title="Long-term memory · private local file", border_style="cyan"))
            elif action == "add" and len(parts) == 3:
                note = parts[2].strip()
                if not note:
                    console.print("Usage: /memory add <short preference or project fact>")
                elif len(note) > 1000:
                    console.print("[yellow]Keep each memory note under 1,000 characters.[/]")
                else:
                    from .tools.stateful import memory_write
                    memory_write(note)
                    console.print("[green]Saved memory locally. Avoid storing API keys or other secrets.[/]")
            elif action == "clear":
                if not MEMORY_FILE.exists():
                    console.print("[dim]Memory is already empty.[/]")
                elif Prompt.ask("Permanently clear Niji's saved memory?", choices=["y", "n"], default="n") == "y":
                    MEMORY_FILE.unlink(missing_ok=True)
                    console.print("[green]Saved memory cleared.[/]")
                else:
                    console.print("[dim]No changes made.[/]")
            else:
                console.print("Usage: /memory [show|add <note>|clear]")
            continue
        if user == "/compact":
            from .compaction import maybe_compact
            agent.messages, done = maybe_compact(
                agent.messages, agent.client, agent.model, force=True)
            console.print("[green]Context compacted.[/]" if done else "[dim]Nothing to compact.[/]")
            continue
        if user == "/clear":
            console.clear()
            continue
        if user == "/providers":
            _cmd_providers(["niji", "providers"])
            continue
        if user == "/doctor":
            _cmd_doctor()
            continue
        if user == "/sessions" or user.startswith("/sessions "):
            query = user[len("/sessions"):].strip()
            if query.startswith("search "):
                query = query[len("search "):].strip()
            _list_sessions(query)
            continue
        if user == "/setup":
            try:
                from .setup_wizard import run_setup
                from openai import OpenAI
                new_provider = run_setup()
                agent.provider_cfg = new_provider
                agent.client = OpenAI(api_key=new_provider["api_key"],
                                      base_url=new_provider["base_url"],
                                      timeout=120, max_retries=0)
                agent.model = new_provider["model"]
                agent.provider_name = new_provider["provider"]
                provider.update(new_provider)
                console.print("[green]Provider switched. Continue chatting.[/]")
                _render_home(agent, provider, quiet)
            except SystemExit as exc:
                console.print(f"[yellow]{exc}[/]")
            continue
        request_started = time.monotonic()
        try:
            # Keep each user turn in the scrollback above the pinned composer;
            # the editor itself is intentionally cleared for the next prompt.
            console.print()
            console.print(Text.assemble(("you ❯ ", "bold cyan"), (user, "white")))
            console.print(Text("niji ❯", style="bold magenta"))
            agent.chat(user)
            if not quiet:
                console.print(f"[dim]{agent.cost_line()}[/]")
        except KeyboardInterrupt:
            console.print("\n[yellow]Request interrupted.[/]")
        except Exception as exc:
            _show_provider_error(provider, exc)
        finally:
            agent.request_seconds = max(0, int(time.monotonic() - request_started))

    # The chat composer reserves a bottom scroll panel; restore normal terminal
    # scrolling once the interactive loop exits.
    from .chat_prompt import reset_chat_layout
    reset_chat_layout()


# ---------------- main ----------------

def _cmd_ui(argv):
    """Start the private, loopback-only browser UI."""
    parser = argparse.ArgumentParser(prog="niji ui", description="Run Niji's private localhost browser interface")
    parser.add_argument("--host", default="127.0.0.1", choices=["127.0.0.1", "localhost"],
                        help="Loopback interface only (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Local port (default: 8765; 0 picks an available port)")
    parser.add_argument("--open", action="store_true", help="Try opening the private URL in a browser")
    parser.add_argument("--auto-approve", action="store_true", help="Allow tool actions without per-action confirmation (not recommended)")
    parser.add_argument("--provider", help="Provider override")
    parser.add_argument("--model", help="Model override")
    parser.add_argument("--api-key", help="API key override")
    parser.add_argument("--mcp", help="Path to a custom mcp.json")
    parser.add_argument("--no-mcp", action="store_true", help="Skip MCP connectors")
    parser.add_argument("--max-turns", type=int, default=20)
    parser.add_argument("--max-tool-calls", type=int, default=30)
    parser.add_argument("--max-tool-calls-per-turn", type=int, default=6)
    args = parser.parse_args(argv)

    from .setup_wizard import needs_setup, run_setup
    if needs_setup(provider_name=args.provider, api_key=args.api_key):
        if not sys.stdin.isatty():
            print("No provider configured. Run: niji setup")
            return
        run_setup(provider_name=args.provider)
        print()

    agent_args = argparse.Namespace(
        provider=args.provider, model=args.model, api_key=args.api_key,
        ask=not args.auto_approve, max_turns=args.max_turns,
        max_tool_calls=args.max_tool_calls,
        max_tool_calls_per_turn=args.max_tool_calls_per_turn,
        quiet=True, mcp=args.mcp, no_mcp=args.no_mcp)
    agent, _provider = _build_agent(agent_args)
    from .webui import NijiWebUI
    try:
        ui = NijiWebUI(agent, host=args.host, port=args.port)
    except OSError as exc:
        if args.port == 0:
            raise
        print(f"[niji] localhost port {args.port} is unavailable ({exc}); choosing an open port")
        ui = NijiWebUI(agent, host=args.host, port=0)
    try:
        ui.serve_forever(open_browser=args.open)
    finally:
        for client in agent.mcp_clients:
            client.stop()


def _cmd_worktree(args):
    """niji worktree new|list|rm: isolate agent work on its own git branch."""
    from . import worktree
    usage = "usage: niji worktree new <name> [base] | list | rm <name> [--force]"
    try:
        if args[:1] == ["new"] and len(args) in (2, 3):
            path = worktree.create(args[1], ".", args[2] if len(args) == 3 else "HEAD")
            print(f"[ok] worktree ready: {path}\n     branch niji/{args[1]}\n     cd {path} && niji")
        elif args[:1] == ["list"]:
            for item in worktree.list_worktrees("."):
                print(f"{item.get('branch', '').removeprefix('refs/heads/')}  {item.get('worktree')}")
        elif args[:1] == ["rm"] and len(args) in (2, 3):
            worktree.remove(args[1], ".", force="--force" in args[2:])
            print(f"[ok] removed worktree {args[1]}")
        else:
            print(usage)
            return 2
    except worktree.WorktreeError as exc:
        print(f"[error] {exc}")
        return 1
    return 0


def _cmd_events(args):
    """niji events [session-id] [--follow-types t1,t2]: replay the typed event log."""
    from . import events
    if not args:
        for sid in events.list_sessions()[:20]:
            print(sid)
        return 0
    try:
        log = events.EventLog(args[0])
    except ValueError as exc:
        print(f"[error] {exc}")
        return 1
    types = None
    if len(args) >= 3 and args[1] == "--types":
        types = set(args[2].split(","))
    found = log.read(types=types)
    if not found:
        print("[no events]")
        return 1
    for event in found:
        print(events.format_event(event))
    return 0


def main():
    argv = sys.argv[1:]

    # ---------- subcommands ----------
    if argv and argv[0] == "ui":
        _cmd_ui(argv[1:])
        return

    if argv and argv[0] == "worktree":
        sys.exit(_cmd_worktree(argv[1:]))
    if argv and argv[0] == "events":
        sys.exit(_cmd_events(argv[1:]))

    if argv and argv[0] == "connectors":
        _cmd_connectors(argv[1:])
        return

    if argv and argv[0] == "providers":
        _cmd_providers(argv)
        return
    if argv and argv[0] == "models":
        _cmd_models(["niji", *argv])
        return
    if argv and argv[0] == "doctor":
        ok = _cmd_doctor()
        sys.exit(0 if ok else 1)
    if argv and argv[0] == "setup":
        from .setup_wizard import run_setup
        run_setup()
        return
    if argv and argv[0] == "sessions":
        query = " ".join(argv[1:]).strip()
        if query.startswith("search "):
            query = query[len("search "):].strip()
        _list_sessions(query)
        return
    if argv and argv[0] == "config":
        if len(argv) >= 4 and argv[1] == "set-key":
            cfg = load_config()
            cfg.setdefault("api_keys", {})[argv[2]] = argv[3]
            save_config(cfg)
            print(f"[ok] API key for '{argv[2]}' saved")
        elif len(argv) >= 3 and argv[1] == "set-default":
            cfg = load_config()
            cfg["provider"] = argv[2]
            save_config(cfg)
            print(f"[ok] default provider = {argv[2]}")
        else:
            print("usage: niji config set-key <provider> <api_key>")
            print("       niji config set-default <provider>")
        return

    # ---------- args ----------
    p = argparse.ArgumentParser(
        prog="niji",
        description="niji-agent — powerful provider-agnostic coding agent "
                    "(MCP connectors, subagents, memory, planning, parallel tools)")
    p.add_argument("--version", action="version", version=f"niji-agent {__version__}")
    p.add_argument("task", nargs="*", help="Task in plain language (omit for chat)")
    p.add_argument("--provider", help="openai | openrouter | anthropic | ... | any custom name")
    p.add_argument("--model", help="Override model name")
    p.add_argument("--api-key", help="Override API key")
    p.add_argument("--ask", action="store_true", help="Ask before shell, file-write, network, and MCP actions")
    p.add_argument("--max-turns", type=int, default=20,
                   help="Maximum model turns per user request (1-100; default 20)")
    p.add_argument("--max-tool-calls", type=int, default=30,
                   help="Maximum tool executions per user request (default 30; hard cap 1000)")
    p.add_argument("--max-tool-calls-per-turn", type=int, default=6,
                   help="Maximum tools executed from one model response (default 6; hard cap 20)")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--resume", help="Resume a saved session id (see: niji sessions)")
    p.add_argument("--continue", dest="cont", action="store_true",
                   help="Resume the most recent session")
    p.add_argument("--mcp", help="Path to a custom mcp.json")
    p.add_argument("--no-mcp", action="store_true", help="Skip MCP connectors")
    args = p.parse_args(argv)

    # ---------- first-run setup (Claude Code style) — runs BEFORE provider resolution ----------
    from .setup_wizard import needs_setup, run_setup
    if needs_setup(provider_name=args.provider, api_key=args.api_key):
        if not sys.stdin.isatty():
            print("No provider configured. Run: niji setup")
            sys.exit(1)
        run_setup(provider_name=args.provider)
        print()

    mcp_path = None if args.no_mcp else args.mcp

    if args.resume or args.cont:
        sid = args.resume
        if args.cont:
            files = sorted(SESSION_DIR.glob("*.json"),
                           key=lambda f: f.stat().st_mtime, reverse=True)
            if not files:
                print("no sessions to continue")
                return
            sid = files[0].stem
        f = SESSION_DIR / f"{sid}.json"
        if not f.exists():
            print(f"session not found: {sid}  (see: niji sessions)")
            return
        agent, provider = _build_agent(args, mcp_path)
        agent.resume(json.loads(f.read_text()))
        agent.session_id = sid
        agent.todos = {"items": load_plan(sid)}
        Console().print(f"[green]Resumed session[/] {sid} ({len(agent.messages)} messages)")
        try:
            _interactive_chat(agent, provider, args.quiet)
        finally:
            for client in agent.mcp_clients:
                client.stop()
        return

    task = " ".join(args.task).strip()
    agent, provider = _build_agent(args, mcp_path)

    if task:
        Console().print(f"[bold green]niji[/] • {provider['provider']}/{provider['model']}")
        try:
            agent.chat(task)
        except Exception as exc:
            _show_provider_error(provider, exc)
            sys.exit(1)
        finally:
            Console().print(f"[dim]{agent.cost_line()}[/]")
            for c in agent.mcp_clients:
                c.stop()
        return

    try:
        _interactive_chat(agent, provider, args.quiet)
    finally:
        for c in agent.mcp_clients:
            c.stop()


if __name__ == "__main__":
    main()
