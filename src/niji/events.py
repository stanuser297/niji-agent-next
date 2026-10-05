"""Persisted, typed, append-only event log shared by the CLI and the local UI.

Each session writes one JSON-lines file under ~/.niji/events/. Every record has
a schema version, a per-session sequence number, a type and a small data dict.
Secret-looking values are redacted before anything touches disk.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from .config import CONFIG_DIR

SCHEMA_VERSION = 1
EVENTS_DIR = CONFIG_DIR / "events"
MAX_FIELD_CHARS = 2000

EVENT_TYPES = frozenset({
    "session.start", "activity", "tool.start", "tool.done", "tool.denied",
    "approval.requested", "approval.decided", "file.change", "run.cancel",
    "run.pause", "run.resume", "worktree.created", "worktree.removed",
})

_SECRET_PATTERNS = [
    re.compile(r"\b(sk|gsk|nvapi|xai|ghp|gho|github_pat)[-_][A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)\b(bearer|token|api[_-]?key|password|secret)\b(\s*[=:]\s*|\s+)[^\s\"',;]{6,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]
_SECRET_KEYS = re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)")


def redact(value, _key=""):
    """Return a JSON-safe copy of value with secret-looking content masked."""
    if _key and _SECRET_KEYS.search(_key) and isinstance(value, str):
        return "[redacted]"
    if isinstance(value, str):
        for pattern in _SECRET_PATTERNS:
            value = pattern.sub(lambda m: m.group(0)[:4] + "[redacted]", value)
        return value if len(value) <= MAX_FIELD_CHARS else value[:MAX_FIELD_CHARS] + "...[truncated]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value[:100]]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value))


class EventLog:
    def __init__(self, session_id: str, directory: Path | str | None = None):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", session_id or ""):
            raise ValueError("invalid session id")
        self.session_id = session_id
        self.directory = Path(directory) if directory else EVENTS_DIR
        self.path = self.directory / f"{session_id}.jsonl"
        self._lock = threading.Lock()
        self._seq = self._last_seq()

    def _last_seq(self) -> int:
        last = 0
        try:
            for event in self.read():
                last = max(last, int(event.get("seq", 0)))
        except OSError:
            pass
        return last

    def emit(self, type_: str, **data) -> dict | None:
        """Append one event. Never raises: logging must not break a run."""
        if type_ not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {type_}")
        try:
            with self._lock:
                self._seq += 1
                record = {"v": SCHEMA_VERSION, "seq": self._seq, "ts": round(time.time(), 3),
                          "session": self.session_id, "type": type_, "data": redact(data)}
                self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, separators=(",", ":")) + "\n")
                return record
        except OSError:
            return None

    def read(self, since_seq: int = 0, types=None) -> list[dict]:
        """Replay events in order, skipping corrupt lines and old sequence numbers."""
        out = []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return out
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get("seq", 0) <= since_seq:
                continue
            if types and event.get("type") not in types:
                continue
            out.append(event)
        return out


def list_sessions(directory: Path | str | None = None) -> list[str]:
    base = Path(directory) if directory else EVENTS_DIR
    if not base.is_dir():
        return []
    files = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.stem for p in files]


def format_event(event: dict) -> str:
    stamp = time.strftime("%H:%M:%S", time.localtime(event.get("ts", 0)))
    data = event.get("data") or {}
    detail = " ".join(f"{k}={json.dumps(v, ensure_ascii=False)[:80]}" for k, v in data.items())
    return f"{event.get('seq', 0):>4} {stamp} {event.get('type', '?'):<19} {detail}"
