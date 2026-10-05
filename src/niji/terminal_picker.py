"""Small, dependency-free arrow-key picker for POSIX terminals (including Termux)."""
import os
import select
import shutil
import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text


def _escape_key(fd: int) -> str:
    """Read a complete CSI/SS3 key sequence from the tty without TextIO buffering."""
    if not select.select([fd], [], [], 0.12)[0]:
        return "ESC"
    first = os.read(fd, 1)
    if first in (b"O", b"["):
        prefix = first.decode("ascii", errors="ignore")
        end = time.monotonic() + 0.18
        sequence = ""
        while time.monotonic() < end:
            remaining = end - time.monotonic()
            if not select.select([fd], [], [], min(0.04, remaining))[0]:
                continue
            byte = os.read(fd, 1)
            if not byte:
                break
            char = byte.decode("ascii", errors="ignore")
            sequence += char
            # CSI parameters/intermediates continue until a final byte (0x40-0x7e).
            if prefix == "[" and char and "@" <= char <= "~":
                break
            if prefix == "O":
                break
        if sequence.endswith("A"):
            return "UP"
        if sequence.endswith("B"):
            return "DOWN"
        if sequence.endswith("5~"):
            return "PAGEUP"
        if sequence.endswith("6~"):
            return "PAGEDOWN"
        return "OTHER"
    return "ESC"


def arrow_select(title, choices, selected=0):
    """Pick a (value, label) option; use typed values when no interactive TTY exists."""
    if not choices:
        return None
    selected = max(0, min(selected, len(choices) - 1))
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        value = Prompt.ask(f"{title} (type an option)", default=choices[selected][0]).strip()
        return next((item[0] for item in choices if value == item[0]), None)

    try:
        import termios
        import tty
        fd = sys.stdin.fileno()
        previous = termios.tcgetattr(fd)
    except (ImportError, OSError, AttributeError, ValueError):
        value = Prompt.ask(f"{title} (type an option)", default=choices[selected][0]).strip()
        return next((item[0] for item in choices if value == item[0]), None)

    console = Console()
    try:
        # Use OS-level byte reads below: TextIO.read() can buffer bytes from an
        # ESC sequence, making select() miss the rest of an arrow key in Termux.
        tty.setcbreak(fd, termios.TCSANOW)
        mode = termios.tcgetattr(fd)
        mode[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        while True:
            height = shutil.get_terminal_size((80, 24)).lines
            page_size = max(4, height - 8)
            start = min(max(0, selected - page_size + 1), max(0, len(choices) - page_size))
            end = min(len(choices), start + page_size)
            console.clear()
            console.print(Panel(title, border_style="bright_cyan"))
            for index in range(start, end):
                label = choices[index][1]
                row = Text()
                if index == selected:
                    row.append(" ❯ ", style="bold black on bright_cyan")
                    row.append(label, style="bold bright_white on blue")
                else:
                    row.append("   ")
                    row.append(label)
                console.print(row)
            console.print(
                f"[dim]↑/↓ or j/k move · Enter select · q/Esc cancel · {selected + 1}/{len(choices)}[/]"
            )
            sys.stdout.flush()
            key = os.read(fd, 1)
            if not key:
                return None
            if key in (b"\r", b"\n"):
                return choices[selected][0]
            if key in (b"q", b"Q", b"\x04"):
                return None
            if key in (b"j", b"J"):
                selected = min(len(choices) - 1, selected + 1)
            elif key in (b"k", b"K"):
                selected = max(0, selected - 1)
            elif key == b"\x1b":
                key_name = _escape_key(fd)
                if key_name == "UP":
                    selected = max(0, selected - 1)
                elif key_name == "DOWN":
                    selected = min(len(choices) - 1, selected + 1)
                elif key_name == "PAGEUP":
                    selected = max(0, selected - page_size)
                elif key_name == "PAGEDOWN":
                    selected = min(len(choices) - 1, selected + page_size)
                elif key_name == "ESC":
                    return None
    except KeyboardInterrupt:
        return None
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)
        except OSError:
            pass
        console.print()
