"""Git worktree isolation: run an agent task on its own branch and directory."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,60}")


class WorktreeError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreeError(f"git failed: {exc}") from exc
    if proc.returncode != 0:
        raise WorktreeError((proc.stderr or proc.stdout).strip() or "git command failed")
    return proc.stdout


def repo_root(path: Path | str = ".") -> Path:
    return Path(_git(Path(path), "rev-parse", "--show-toplevel").strip())


def worktree_dir(root: Path) -> Path:
    return root.parent / f".{root.name}-niji-worktrees"


def create(name: str, path: Path | str = ".", base: str = "HEAD") -> Path:
    if not _NAME.fullmatch(name or "") or ".." in name or name.endswith(".lock"):
        raise WorktreeError("worktree name may use letters, digits, dot, dash, underscore")
    root = repo_root(path)
    target = worktree_dir(root) / name
    if target.exists():
        raise WorktreeError(f"{target} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-b", f"niji/{name}", str(target), base)
    return target


def list_worktrees(path: Path | str = ".") -> list[dict]:
    root = repo_root(path)
    items, current = [], {}
    for line in _git(root, "worktree", "list", "--porcelain").splitlines() + [""]:
        if not line:
            if current:
                items.append(current)
            current = {}
        elif " " in line:
            key, value = line.split(" ", 1)
            current[key] = value
        else:
            current[line] = True
    return [i for i in items if str(i.get("branch", "")).startswith("refs/heads/niji/")]


def remove(name: str, path: Path | str = ".", force: bool = False) -> None:
    if not _NAME.fullmatch(name or "") or ".." in name:
        raise WorktreeError("invalid worktree name")
    root = repo_root(path)
    target = worktree_dir(root) / name
    args = ["worktree", "remove", str(target)] + (["--force"] if force else [])
    _git(root, *args)
    _git(root, "branch", "-D" if force else "-d", f"niji/{name}")
