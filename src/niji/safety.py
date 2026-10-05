"""Safety helpers for shell and child-process execution."""
import os
import re

DANGEROUS = [
    r"rm\s+-[rfRF]+[^|;&]*\s+/\s*$",
    r"rm\s+-[rfRF]+\s+/(bin|boot|dev|etc|home|lib|proc|root|sbin|sys|usr|var)(\s|/|$)",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b[^\n]*of=/dev/",
    r"\b(shutdown|reboot|poweroff|halt)\b",
    r"\b(chown|chmod)\b[^\n]*-R[^\n]*/\s*$",
    r"wget[^\n]*\|\s*(sudo\s+)?(ba)?sh",
    r"curl[^\n]*\|\s*(sudo\s+)?(ba)?sh",
    r"base64\s+(-d|--decode)[^\n]*\|\s*(ba)?sh",
    r":\(\)\s*\{\s*:\|:&\s*\};:",
]
_SECRET_ENV_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD", "_ACCESS_KEY")


def check_command(command: str):
    for pat in DANGEROUS:
        if re.search(pat, command):
            raise PermissionError(f"blocked dangerous pattern: /{pat}/")


def subprocess_environment(extra=None):
    """Keep PATH/runtime settings but avoid inheriting common credentials.

    Explicit per-server variables are added back by MCP configuration after this
    helper returns. This is defense in depth, not a sandbox: child processes can
    still read files accessible to the current operating-system user.
    """
    env = {key: value for key, value in os.environ.items()
           if key.upper() != "NIJI_API_KEY"
           and not key.upper().endswith(_SECRET_ENV_SUFFIXES)}
    if extra:
        env.update(extra)
    return env
