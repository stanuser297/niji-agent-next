"""Shared terminal output primitives for concurrent agent activity."""
import threading

OUTPUT_LOCK = threading.RLock()


def safe_terminal_text(value):
    """Strip terminal control characters from untrusted/model-provided text."""
    return "".join(char for char in str(value)
                   if char in "\n\t" or char.isprintable())
