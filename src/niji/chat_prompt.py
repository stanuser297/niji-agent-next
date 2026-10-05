"""Niji-branded, inline terminal chat composer and live session details bar."""
import os
import select
import shutil
import sys
import time
import unicodedata
from rich.console import Console
from rich.prompt import Prompt
from wcwidth import wcswidth

from .compaction import estimate_tokens

_history = []

# Niji identity: cyan and violet with a small warm amber highlight.
_CYAN = "\x1b[96m"
_BLUE = "\x1b[94m"
_VIOLET = "\x1b[95m"
_AMBER = "\x1b[93m"
_WHITE = "\x1b[97m"
_DIM = "\x1b[2;37m"
_RESET = "\x1b[0m"


def _paint(value, color, enabled):
    return f"{color}{value}{_RESET}" if enabled else value


def _clip(value, cells):
    """Clip a string to terminal cells, reserving the last cell for an ellipsis."""
    value = str(value)
    if wcswidth(value) <= cells:
        return value
    if cells <= 0:
        return ""
    out = ""
    for char in value:
        if wcswidth(out + char + "…") > cells:
            break
        out += char
    return out + "…"


def _is_grapheme_extend(char):
    code = ord(char)
    return (unicodedata.category(char).startswith("M") or char == "\u200d"
            or 0xFE00 <= code <= 0xFE0F or 0xE0100 <= code <= 0xE01EF
            or 0x1F3FB <= code <= 0x1F3FF or 0xE0020 <= code <= 0xE007F)


def _grapheme_boundaries(value):
    """Approximate Unicode grapheme boundaries for reliable terminal editing."""
    if not value:
        return [0]
    boundaries = [0]
    regional_run = 1 if 0x1F1E6 <= ord(value[0]) <= 0x1F1FF else 0
    for index in range(1, len(value)):
        previous, current = value[index - 1], value[index]
        previous_name = unicodedata.name(previous, "")
        is_regional = 0x1F1E6 <= ord(current) <= 0x1F1FF
        join = (_is_grapheme_extend(current) or previous == "\u200d"
                or "VIRAMA" in previous_name or "HALANT" in previous_name)
        if 0x1F1E6 <= ord(previous) <= 0x1F1FF and is_regional:
            join = regional_run % 2 == 1
        if not join:
            boundaries.append(index)
        if is_regional:
            regional_run = regional_run + 1 if 0x1F1E6 <= ord(previous) <= 0x1F1FF else 1
        else:
            regional_run = 0
    boundaries.append(len(value))
    return boundaries


def _previous_boundary(value, cursor):
    previous = 0
    for boundary in _grapheme_boundaries(value):
        if boundary >= cursor:
            return previous
        previous = boundary
    return previous


def _next_boundary(value, cursor):
    for boundary in _grapheme_boundaries(value):
        if boundary > cursor:
            return boundary
    return len(value)


def _visible_input(value, cursor, cells):
    """Build a cell-width-safe viewport while keeping the caret visible."""
    bounds = _grapheme_boundaries(value)
    cursor = min(max(cursor, 0), len(value))
    left = cursor
    while left > 0:
        previous = _previous_boundary(value, left)
        prefix_width = wcswidth(value[previous:cursor])
        indicator_width = 1 if previous > 0 else 0
        if prefix_width + indicator_width > cells:
            break
        left = previous
    leading = "…" if left > 0 else ""
    before_cursor = value[left:cursor]
    display = leading + before_cursor
    right = cursor
    while right < len(value):
        next_pos = _next_boundary(value, right)
        part = value[right:next_pos]
        remaining = value[next_pos:]
        reserve = 1 if remaining else 0
        if wcswidth(display + part) + reserve > cells:
            break
        display += part
        right = next_pos
    if right < len(value) and wcswidth(display + "…") <= cells:
        display += "…"
    return display, wcswidth(leading + before_cursor)


def _fields(agent, provider):
    usage = getattr(agent, "usage", {}) or {}
    tokens = int(usage.get("prompt_tokens", 0) or 0) + int(usage.get("completion_tokens", 0) or 0)
    conversation = getattr(agent, "messages", []) or []
    messages = len(conversation)
    context_size = estimate_tokens(conversation)
    context = f"~{context_size:,} tok" if context_size else f"{messages} msgs"
    tools = sum((getattr(agent, "tool_usage", {}) or {}).values())
    seconds = getattr(agent, "request_seconds", None)
    if seconds is None:
        started = getattr(agent, "started_at", None)
        seconds = max(0, int(time.monotonic() - started)) if started else 0
    seconds = max(0, int(seconds))
    duration = f"{seconds // 60}m{seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"
    return [
        ("MODEL", provider.get("model", getattr(agent, "model", "default"))),
        ("PROVIDER", provider.get("provider", getattr(agent, "provider_name", "unknown"))),
        ("CONTEXT", context),
        ("AGENT", "Niji-Agent"),
        ("RUNTIME", f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"),
        ("TOKENS", f"{tokens:,}"),
        ("TOOLS", str(tools)),
        ("TIME", duration),
    ]


def _status_rows(agent, provider, width, enabled=True):
    """Wrap the exact screenshot-inspired session fields for narrow Termux screens."""
    inner = max(18, width - 2)
    groups = []
    current = []
    current_width = 0
    for label, value in _fields(agent, provider):
        label_width = len(label)
        allowance = max(5, inner - label_width - 5)
        value = _clip(value, min(allowance, 32))
        part_width = label_width + 1 + wcswidth(value)
        candidate_width = current_width + (5 if current else 0) + part_width
        if current and candidate_width > inner:
            groups.append(current)
            current, current_width = [], 0
            candidate_width = part_width
        current.append((label, value))
        current_width = candidate_width
    if current:
        groups.append(current)

    rows = []
    for group in groups:
        pieces = []
        for label, value in group:
            pieces.append(f"{_paint(label, _AMBER, enabled)} {_paint(value, _WHITE, enabled)}")
        rows.append("  ·  ".join(pieces))
    return rows


def _frame(title, contents, width, enabled=True):
    """Build a one-cell-safe rounded frame; `contents` may include ANSI colors."""
    width = max(24, width)
    inner = width - 2
    title_text = f" {title} "
    top_fill = max(0, inner - wcswidth(title_text) - 1)
    top = "╭" + "─" + title_text + "─" * top_fill + "╮"
    lines = [_paint(top, _CYAN, enabled)]
    for content in contents:
        visible = wcswidth(_strip_ansi(content))
        room = inner
        if visible > room:
            # Clip plain text when possible; colored fragments are pre-sized by callers.
            content = _clip(_strip_ansi(content), room)
            visible = wcswidth(content)
        line = "│" + content + " " * max(0, room - visible) + "│"
        lines.append(_paint("│", _CYAN, enabled) + line[1:-1] + _paint("│", _CYAN, enabled))
    bottom = "╰" + "─" * inner + "╯"
    lines.append(_paint(bottom, _CYAN, enabled))
    return lines


def _strip_ansi(value):
    # Generated styles use only SGR sequences, so a tiny parser avoids another dependency.
    out = []
    skip = False
    for char in value:
        if char == "\x1b":
            skip = True
        elif skip and char == "m":
            skip = False
        elif not skip:
            out.append(char)
    return "".join(out)


def _prompt_lines(agent, provider, value, cursor, width, enabled=True):
    width = max(24, width - 1)  # avoid terminal autowrap in the rightmost cell
    inner = width - 2
    prefix = " ❯ "
    available = max(1, inner - wcswidth(prefix) - 1)
    placeholder = "Ask anything… (type your message here)"
    if value:
        visible_value, visible_cursor = _visible_input(value, cursor, available)
        input_text = _paint(visible_value, _WHITE, enabled)
        cursor_column = wcswidth(prefix) + visible_cursor
    else:
        input_text = _paint(_clip(placeholder, available), _DIM, enabled)
        cursor_column = wcswidth(prefix)
    input_row = prefix + input_text + " "
    rows = _frame("✧  NIJI  ·  CHAT", [input_row], width, enabled)
    status_rows = _status_rows(agent, provider, width, enabled)
    rows.extend(_frame("SESSION DETAILS", status_rows, width, enabled))
    return rows, cursor_column, len(status_rows)


def _write_prompt_frame(agent, provider, value, cursor, width, enabled=True, initial=False):
    """Draw the composer and details into a fixed bottom panel.

    Model output uses the terminal's scrolling region above this panel, so long
    replies scroll without pushing the input or session details off-screen.
    Absolute cursor positioning also prevents a new frame being appended for
    every keystroke (the old relative cursor math drifted on multi-row panels).
    """
    rows, cursor_column, status_count = _prompt_lines(agent, provider, value, cursor, width, enabled)
    height = max(8, shutil.get_terminal_size((80, 24)).lines)
    panel_top = max(1, height - len(rows) + 1)
    content_bottom = max(1, panel_top - 1)
    # Keep the scrolling region above the fixed composer/details panel.
    sys.stdout.write(f"\x1b[1;{content_bottom}r")
    for offset, row in enumerate(rows):
        sys.stdout.write(f"\x1b[{panel_top + offset};1H\x1b[2K{row}")
    # `cursor_column` already includes the ` ❯ ` prefix; add only the left
    # frame border (col 1) to reach the actual terminal column.
    sys.stdout.write(f"\x1b[{panel_top + 1};{2 + cursor_column}H")
    sys.stdout.flush()
    return status_count, content_bottom, panel_top


def reset_chat_layout():
    """Remove the pinned chat UI and restore a clean, normal shell terminal."""
    if sys.stdout.isatty():
        # Restore full-screen scrolling and normal terminal modes before clearing
        # only the visible screen (ED 2 preserves scrollback). Leave the cursor
        # at the top-left so the parent shell prints a fresh prompt there.
        sys.stdout.write("\x1b[r\x1b[?2004l\x1b[?25h\x1b[?7h\x1b[0m\x1b[2J\x1b[H")
        sys.stdout.flush()


def _read_char(fd):
    first = os.read(fd, 1)
    if not first:
        return ""
    lead = first[0]
    size = 1 if lead < 0x80 else (2 if lead & 0xE0 == 0xC0 else 3 if lead & 0xF0 == 0xE0 else 4)
    data = bytearray(first)
    for _ in range(size - 1):
        if not select.select([fd], [], [], 0.2)[0]:
            break
        data.extend(os.read(fd, 1))
    return bytes(data).decode("utf-8", errors="replace")


def _read_escape(fd):
    """Read a CSI/SS3 key or bracketed-paste body from the controlling terminal."""
    if not select.select([fd], [], [], 0.15)[0]:
        return "ESC", ""
    first = os.read(fd, 1)
    if first not in (b"[", b"O"):
        return "ESC", ""
    prefix = first.decode("ascii")
    sequence = ""
    deadline = time.monotonic() + 0.2
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], 0.04)[0]:
            continue
        byte = os.read(fd, 1)
        if not byte:
            break
        char = byte.decode("ascii", errors="ignore")
        sequence += char
        if prefix == "[" and char and "@" <= char <= "~":
            break
        if prefix == "O":
            break
    if sequence == "200~":
        pasted = bytearray()
        marker = b"\x1b[201~"
        tail = bytearray()
        deadline = time.monotonic() + 2.0
        while len(pasted) < 65536 and time.monotonic() < deadline:
            if not select.select([fd], [], [], 0.1)[0]:
                if pasted:
                    break
                continue
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            tail.extend(chunk)
            position = tail.find(marker)
            if position >= 0:
                pasted.extend(tail[:position])
                break
            keep = min(len(tail), len(marker) - 1)
            if len(tail) > keep:
                pasted.extend(tail[:-keep])
                del tail[:-keep]
        return "PASTE", bytes(pasted).decode("utf-8", errors="replace").replace("\r", " ").replace("\n", " ")
    final = sequence[-1:] if sequence else ""
    if final == "A":
        return "UP", ""
    if final == "B":
        return "DOWN", ""
    if final == "C":
        return "RIGHT", ""
    if final == "D":
        return "LEFT", ""
    if final in ("H", "~") and (final == "H" or sequence.startswith("1~")):
        return "HOME", ""
    if final in ("F", "~") and (final == "F" or sequence.startswith("4~")):
        return "END", ""
    if final == "~" and sequence.startswith("3~"):
        return "DELETE", ""
    return "OTHER", ""


def read_chat_prompt(agent, provider):
    """Read one editable chat line inside a Niji frame with a live metrics footer."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        Console().print("[dim]Session: " + " · ".join(f"{k} {v}" for k, v in _fields(agent, provider)) + "[/]")
        return Prompt.ask("you ❯").strip()
    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        original_settings = termios.tcgetattr(fd)
    except (ImportError, OSError, AttributeError, ValueError):
        return Prompt.ask("you ❯").strip()

    width = max(24, shutil.get_terminal_size((80, 24)).columns)
    enabled = sys.stdout.isatty() and "NO_COLOR" not in os.environ
    buffer = ""
    cursor = 0
    history_index = len(_history)
    saved_current = ""
    footer_count = 0
    content_bottom = 1
    try:
        tty.setcbreak(fd, termios.TCSANOW)
        mode = termios.tcgetattr(fd)
        mode[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        sys.stdout.write("\x1b[?2004h")  # bracketed paste: safe multiline clipboard handling
        sys.stdout.flush()
        footer_count, content_bottom, _ = _write_prompt_frame(
            agent, provider, buffer, cursor, width, enabled, initial=True)
        while True:
            key = _read_char(fd)
            if not key:
                result = None
                sys.stdout.write(f"\x1b[{content_bottom};1H\x1b[2K")
                sys.stdout.flush()
                break
            if key == "\x1b":
                action, pasted = _read_escape(fd)
                if action == "LEFT":
                    cursor = _previous_boundary(buffer, cursor)
                elif action == "RIGHT":
                    cursor = _next_boundary(buffer, cursor)
                elif action == "HOME":
                    cursor = 0
                elif action == "END":
                    cursor = len(buffer)
                elif action == "DELETE" and cursor < len(buffer):
                    buffer = buffer[:cursor] + buffer[cursor + 1:]
                elif action == "UP" and _history:
                    if history_index == len(_history):
                        saved_current = buffer
                    history_index = max(0, history_index - 1)
                    buffer, cursor = _history[history_index], len(_history[history_index])
                elif action == "DOWN":
                    history_index = min(len(_history), history_index + 1)
                    buffer = saved_current if history_index == len(_history) else _history[history_index]
                    cursor = len(buffer)
                elif action == "PASTE":
                    buffer = buffer[:cursor] + pasted + buffer[cursor:]
                    cursor += len(pasted)
                elif action == "ESC":
                    pass
            elif key in ("\r", "\n"):
                result = buffer.strip()
                if result:
                    _history.append(result)
                width = max(24, shutil.get_terminal_size((80, 24)).columns)
                footer_count, content_bottom, _ = _write_prompt_frame(
                    agent, provider, buffer, cursor, width, enabled)
                # Leave the caret at the last scrollable content row. Model output
                # then renders above the fixed input/details panel.
                sys.stdout.write(f"\x1b[{content_bottom};1H\x1b[2K")
                sys.stdout.flush()
                break
            elif key in ("\x7f", "\b"):
                if cursor:
                    previous_cursor = _previous_boundary(buffer, cursor)
                    buffer = buffer[:previous_cursor] + buffer[cursor:]
                    cursor = previous_cursor
            elif key == "\x03":
                raise KeyboardInterrupt
            elif key == "\x04":
                if not buffer:
                    result = None
                    sys.stdout.write(f"\x1b[{content_bottom};1H\x1b[2K")
                    sys.stdout.flush()
                    break
                if cursor < len(buffer):
                    next_cursor = _next_boundary(buffer, cursor)
                    buffer = buffer[:cursor] + buffer[next_cursor:]
            elif key == "\x01":
                cursor = 0
            elif key == "\x05":
                cursor = len(buffer)
            elif key == "\x15":
                buffer = buffer[cursor:]
                cursor = 0
            elif key == "\x0b":
                buffer = buffer[:cursor]
            elif key == "\x17":
                start = cursor
                while start and buffer[start - 1].isspace():
                    start -= 1
                while start and not buffer[start - 1].isspace():
                    start -= 1
                buffer = buffer[:start] + buffer[cursor:]
                cursor = start
            elif key == "\x0c":
                sys.stdout.write("\x1b[2J\x1b[H")
            elif key.isprintable():
                buffer = buffer[:cursor] + key + buffer[cursor:]
                cursor += len(key)
            else:
                continue
            width = max(24, shutil.get_terminal_size((80, 24)).columns)
            footer_count, content_bottom, _ = _write_prompt_frame(
                agent, provider, buffer, cursor, width, enabled)
    except KeyboardInterrupt:
        sys.stdout.write(f"\x1b[{content_bottom};1H\x1b[2K")
        sys.stdout.flush()
        raise
    finally:
        try:
            sys.stdout.write("\x1b[?2004l")
            sys.stdout.flush()
            termios.tcsetattr(fd, termios.TCSADRAIN, original_settings)
        except OSError:
            pass
    return result
