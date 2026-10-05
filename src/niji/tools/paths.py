"""Resolve relative tool paths against the active project workspace."""
from __future__ import annotations

import os
from pathlib import Path


def workspace_path(value: str | os.PathLike | None, ctx: dict = None,
                   default: str = ".") -> Path:
    """Expand a tool path, anchoring relative paths at the calling Agent workspace."""
    raw = default if value is None or str(value) == "" else value
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    agent = (ctx or {}).get("agent")
    workspace = getattr(agent, "workspace", None) if agent is not None else None
    base = Path(workspace).expanduser() if workspace else Path.cwd()
    return base / path


def workspace_cwd(value: str | os.PathLike | None, ctx: dict = None,
                  default: str = ".") -> str:
    """Return a normalized cwd for subprocess-based tools."""
    return str(workspace_path(value, ctx, default).resolve())
