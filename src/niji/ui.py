"""Niji's responsive terminal command-center dashboard."""
from datetime import datetime
from getpass import getuser
from pathlib import Path
import platform

from rich.columns import Columns
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__


def _tool_name(schema):
    return schema.get("function", {}).get("name", "tool")


def _tool_desc(schema):
    return schema.get("function", {}).get("description", "Available agent tool.").split(".")[0]


def _tool_groups(agent):
    groups = {
        "Workspace & Files": [],
        "Web & Media": [],
        "Planning & Memory": [],
        "Terminal & Delegation": [],
        "MCP Connectors": [],
    }
    for schema in getattr(agent, "tool_schemas", []):
        name = _tool_name(schema)
        if "__" in name:
            category = "MCP Connectors"
        elif name in {"read_file", "read_document", "write_file", "edit_file", "list_files", "grep", "glob"}:
            category = "Workspace & Files"
        elif name in {"web_fetch", "read_image"}:
            category = "Web & Media"
        elif name in {"todo_read", "todo_write", "memory_read", "memory_write"}:
            category = "Planning & Memory"
        else:
            category = "Terminal & Delegation"
        groups[category].append((name, _tool_desc(schema)))
    return {name: items for name, items in groups.items() if items}


def _format_uptime(agent):
    started = getattr(agent, "started_at", None)
    if started is None:
        return "just started"
    seconds = max(0, int(__import__("time").monotonic() - started))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _brand_panel(width=None):
    logo = Text()
    glyph = ["██    ██", "███   ██", "████  ██", "██ ██ ██", "██  ████", "██   ███", "██    ██"]
    shades = ["bright_blue", "blue", "bright_cyan", "cyan", "bright_cyan", "blue", "bright_blue"]
    for line, shade in zip(glyph, shades):
        logo.append(line + "\n", style=f"bold {shade}")

    title = Text("Niji", style="bold bright_white")
    title.append("-Agent", style="bold bright_cyan")
    copy = Text("Your Personal AI Workspace", style="bright_white")
    motto = Text("Think  /  Plan  /  Execute  /  Automate", style="bold bright_cyan")
    release = Text(f"v{__version__}  ·  YOUR IDEAS, IN MOTION", style="dim")
    if (width or Console().size.width) >= 68:
        content = Table.grid(padding=(0, 2))
        content.add_column(no_wrap=True)
        content.add_column()
        content.add_row(logo, Group(title, copy, Text(""), motto, release))
    else:
        content = Group(logo, title, copy, motto, release)
    return Panel(content, border_style="bright_cyan", padding=(0, 1))


def _metadata_panel(agent, provider):
    rows = Table.grid(padding=(0, 1))
    rows.add_column(style="bold white", no_wrap=True)
    rows.add_column(style="bold bright_cyan", overflow="fold")
    rows.add_row("Agent", "Niji-Agent")
    rows.add_row("Version", __version__)
    rows.add_row("Model", str(provider.get("model", "default")))
    rows.add_row("Provider", str(provider.get("provider", "unknown")))
    rows.add_row("Runtime", f"Python {platform.python_version()}")
    rows.add_row("Uptime", _format_uptime(agent))
    rows.add_row("Host", platform.node() or "local")
    try:
        user = getuser()
    except Exception:
        user = "local"
    rows.add_row("User", user)
    return Panel(rows, title="[bold bright_cyan]AGENT PROFILE[/]", border_style="cyan",
                 padding=(0, 1))


def _overview_panel(agent, provider):
    tools = getattr(agent, "tool_schemas", [])
    todos = getattr(agent, "todos", {}).get("items", [])
    running = sum(1 for item in todos if item.get("status") == "in_progress")
    left = Table.grid(padding=(0, 1))
    left.add_column(style="bright_cyan", no_wrap=True)
    left.add_column(overflow="fold")
    left.add_row("Name", "Niji-Agent")
    left.add_row("Version", __version__)
    left.add_row("Model", str(provider.get("model", "default")))
    left.add_row("Provider", str(provider.get("provider", "unknown")))
    left.add_row("Runtime", f"Python {platform.python_version()}")

    right = Table.grid(padding=(0, 1))
    right.add_column(style="bright_cyan", no_wrap=True)
    right.add_column(style="bold white")
    right.add_row("Status", Text("● READY", style="bold green"))
    right.add_row("Tasks Running", str(running))
    right.add_row("Tools Loaded", str(len(tools)))
    right.add_row("MCP Links", str(len(getattr(agent, "mcp_clients", []))))
    right.add_row("Session", str(getattr(agent, "session_id", "—"))[-12:])
    content = Table.grid(expand=True, padding=(0, 1))
    content.add_column(ratio=1)
    content.add_column(ratio=1)
    content.add_row(left, right)
    return Panel(content, title="[bold bright_cyan]✧  AGENT OVERVIEW[/]",
                 border_style="bright_cyan", padding=(0, 1))


def _tool_catalog(agent, width):
    groups = _tool_groups(agent)
    if not groups:
        return Panel(Text("No tools loaded", style="dim"),
                     title="[bold bright_cyan]AVAILABLE TOOLS[/]", border_style="cyan")
    columns = 2 if width >= 100 else 1
    grid = Table.grid(expand=True, padding=(0, 1))
    for _ in range(columns):
        grid.add_column(ratio=1)
    items = list(groups.items())
    cells = []
    desc_limit = 40 if columns == 2 else max(20, width - 27)
    for category, entries in items:
        block = Text()
        block.append(f"◆  {category}  ({len(entries)})\n", style="bold bright_cyan")
        for name, desc in entries[:6]:
            desc = desc.replace("\n", " ")
            if len(desc) > desc_limit:
                desc = desc[:desc_limit - 1].rstrip() + "…"
            block.append(f"   {name:<18}  ", style="bold white")
            block.append(desc + "\n", style="bright_white")
        if len(entries) > 6:
            block.append(f"   … and {len(entries) - 6} more\n", style="dim")
        cells.append(block)
    if columns == 1:
        for cell in cells:
            grid.add_row(cell)
    else:
        for index in range(0, len(cells), 2):
            grid.add_row(cells[index], cells[index + 1] if index + 1 < len(cells) else Text(""))
    return Panel(grid, title=f"[bold bright_cyan]⚒  AVAILABLE TOOLS  ·  {sum(map(len, groups.values()))} loaded[/]",
                 border_style="bright_cyan", padding=(0, 1))


def _usage_panel(agent):
    counts = getattr(agent, "tool_usage", {})
    total = sum(counts.values())
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="bright_white")
    grid.add_column(justify="right", style="bold bright_cyan")
    grid.add_column(style="bright_cyan")
    if total == 0:
        grid.add_row("No tool calls yet", "0", "")
    else:
        grouped = {}
        category_by_tool = {
            name: category for category, items in _tool_groups(agent).items()
            for name, _ in items
        }
        for name, count in counts.items():
            category = category_by_tool.get(name, "MCP Connectors")
            grouped[category] = grouped.get(category, 0) + count
        for category, count in sorted(grouped.items(), key=lambda item: item[1], reverse=True)[:5]:
            grid.add_row(category, str(count), "▰" * min(12, count))
        grid.add_row(Text("Total calls", style="bold"), str(total), "")
    return Panel(grid, title="[bold bright_cyan]TOOL USAGE · SESSION[/]",
                 border_style="bright_blue", padding=(0, 1))


def _system_panel(agent):
    usage = getattr(agent, "usage", {})
    mode = "CONFIRM ACTIONS" if getattr(agent, "approval", "auto") == "ask" else "AUTO"
    rows = Table.grid(padding=(0, 1))
    rows.add_column(style="bright_cyan", no_wrap=True)
    rows.add_column(style="bright_white", overflow="fold")
    rows.add_row("Mode", mode)
    rows.add_row("Uptime", _format_uptime(agent))
    rows.add_row("MCP", f"{len(getattr(agent, 'mcp_clients', []))} connected")
    rows.add_row("Turns", f"{usage.get('turns', 0)} total · max {getattr(agent, 'max_turns', 20)}/request")
    rows.add_row("Tool budget", f"{getattr(agent, '_request_tool_calls', 0)}/{getattr(agent, 'max_tool_calls', 30)} this request")
    rows.add_row("Tokens", f"{usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0):,}")
    return Panel(rows, title="[bold bright_cyan]SYSTEM STATUS[/]", border_style="cyan",
                 padding=(0, 1))


def _activity_panel(agent):
    events = list(getattr(agent, "activity", []))[-5:]
    if not events:
        now = datetime.now().strftime("%H:%M:%S")
        events = [{"time": now, "level": "INFO", "message": "Provider configuration loaded"},
                  {"time": now, "level": "INFO", "message": f"{len(getattr(agent, 'tool_schemas', []))} tool definitions ready"}]
    content = Text()
    for event in events:
        level = str(event.get("level", "INFO"))
        color = "green" if level in {"READY", "OK"} else "bright_cyan"
        content.append(f"{event.get('time', '--:--:--')}  [{level}]  ", style=f"bold {color}")
        content.append(str(event.get("message", ""))[:72] + "\n", style="bright_white")
    return Panel(content, title="[bold bright_cyan]◷  RECENT ACTIVITY[/]", border_style="cyan",
                 padding=(0, 1))


def _quick_commands():
    commands = [("/help", "Show help"), ("/tools", "List tools"), ("/status", "Agent status"),
                ("/model", "Browse/switch models"), ("/models", "List model catalogs"),
                ("/approval", "Toggle tool confirmations"), ("/activity", "Execution activity feed"),
                ("/limits", "Execution budgets"), ("/setup", "Provider setup"), ("/providers", "Providers"),
                ("/doctor", "Diagnostics"), ("/sessions", "Sessions"),
                ("/clear", "Redraw"), ("/exit", "Quit Niji")]
    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="bold bright_white", no_wrap=True)
    grid.add_column(style="dim bright_white")
    for command, description in commands:
        grid.add_row(command, description)
    return Panel(grid, title="[bold bright_cyan]⚡ QUICK COMMANDS[/]", border_style="bright_blue",
                 padding=(0, 1))


def render_setup_banner(console=None):
    """Show Niji branding before provider setup or connection checks."""
    console = console or Console()
    console.print(_brand_panel(console.size.width))
    console.print(Text("First, connect your AI provider. The API key is stored locally.", style="dim"))


def render_home(agent, provider, quiet=False, console=None):
    """Render a detailed dashboard that adapts to desktop and phone terminals."""
    if quiet:
        return
    console = console or Console()
    width = console.size.width
    brand = _brand_panel(width)
    metadata = _metadata_panel(agent, provider)
    console.print(Columns([brand, metadata], equal=False, expand=True, padding=(0, 1))
                  if width >= 100 else Group(brand, metadata))

    status = Text("●  Niji-Agent is ready.", style="bold green")
    status.append("  Type /help for commands.", style="bright_white")
    console.print(Panel(status, border_style="bright_cyan", padding=(0, 1)))

    overview = _overview_panel(agent, provider)
    catalog = _tool_catalog(agent, width)
    usage = _usage_panel(agent)
    system = _system_panel(agent)
    activity = _activity_panel(agent)
    commands = _quick_commands()
    if width >= 110:
        main = Table.grid(expand=True, padding=(0, 1))
        main.add_column(ratio=3)
        main.add_column(ratio=1)
        main.add_row(Group(overview, catalog), Group(usage, system))
        footer = Table.grid(expand=True, padding=(0, 1))
        footer.add_column(ratio=3)
        footer.add_column(ratio=1)
        footer.add_row(activity, commands)
        console.print(main)
        console.print(footer)
    else:
        console.print(overview)
        console.print(catalog)
        console.print(usage)
        console.print(system)
        console.print(activity)
        console.print(commands)
    console.print(Text('" Your ideas, in motion. "  — Niji-Agent', style="italic dim bright_cyan"))
