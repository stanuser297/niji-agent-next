"""Private, crash-tolerant local run history for the Niji web UI."""
from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path

_MAX_RECORD_BYTES = 256_000
_MAX_RECORDS = 100
MAX_JOB_EVENTS = 300
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_STATUS = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ACTIVE_STATES = {"running", "pause_requested", "paused"}
_FIELDS = (
    "id", "status", "response", "error", "streamed", "progress", "progress_detail",
    "activity", "events", "plan_only", "original_message", "cancel_requested", "plan",
    "plan_label", "session_id", "plan_approved", "created", "updated", "automation_id",
    "execution_job_id", "workspace_path",
)


class RunStore:
    """Store bounded job snapshots in private per-run JSON files.

    Writes use an atomic replace so a crash cannot leave half a JSON record. Active
    records are marked interrupted on load and are never replayed automatically.
    """

    def __init__(self, directory: str | Path, *, max_records: int = _MAX_RECORDS):
        self.directory = Path(directory).expanduser()
        self.max_records = max(1, min(int(max_records), 1000))
        self._lock = threading.RLock()
        if self.directory.is_symlink():
            raise OSError("Run history directory must not be a symlink")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.directory.chmod(0o700)

    @staticmethod
    def _valid_timestamp(value) -> bool:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(float(value)) and value >= 0
        except (OverflowError, ValueError):
            return False

    @staticmethod
    def _record(job: dict, *, update_timestamp: bool = True) -> dict:
        record = {key: job[key] for key in _FIELDS if key in job}
        if not isinstance(record.get("id"), str) or not _JOB_ID.fullmatch(record["id"]):
            raise ValueError("Invalid run id")
        if not isinstance(record.get("status"), str) or not _STATUS.fullmatch(record["status"]):
            raise ValueError("Invalid run status")
        if update_timestamp or not RunStore._valid_timestamp(record.get("updated")):
            record["updated"] = time.time()
        else:
            record["updated"] = float(record["updated"])
        if not RunStore._valid_timestamp(record.get("created")):
            record["created"] = record["updated"]
        else:
            record["created"] = float(record["created"])
        raw_events = record.get("events", [])
        record["events"] = [event for event in raw_events if isinstance(event, dict)][-MAX_JOB_EVENTS:] \
            if isinstance(raw_events, list) else []
        if isinstance(record.get("activity"), dict):
            record["activity"] = {key: value for key, value in record["activity"].items()
                                   if isinstance(key, str) and isinstance(value, (str, int, float, bool, type(None)))}
        if isinstance(record.get("streamed"), str):
            record["streamed"] = record["streamed"][-40_000:]
        if isinstance(record.get("response"), str):
            record["response"] = record["response"][:40_000]
        if isinstance(record.get("original_message"), str):
            record["original_message"] = record["original_message"][:30_000]
        if isinstance(record.get("error"), str):
            record["error"] = record["error"][:2_000]
        return json.loads(json.dumps(record, ensure_ascii=False, separators=(",", ":"), allow_nan=False))

    def _path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _JOB_ID.fullmatch(job_id):
            raise ValueError("Invalid run id")
        return self.directory / f"{job_id}.json"

    def save(self, job: dict) -> dict:
        if not isinstance(job, dict):
            raise ValueError("Run record must be an object")
        record = self._record(job)
        payload = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > _MAX_RECORD_BYTES:
            # Keep the durable checkpoint useful without allowing a giant prompt or plan
            # to exhaust disk. The in-memory run remains untouched.
            record["original_message"] = str(record.get("original_message", ""))[:8_000]
            record["response"] = str(record.get("response", ""))[:12_000]
            record["streamed"] = str(record.get("streamed", ""))[-12_000:]
            record["events"] = list(record.get("events", []))[-40:]
            payload = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(payload) > _MAX_RECORD_BYTES:
            raise ValueError("Run record exceeds the safe storage limit")

        path = self._path(record["id"])
        with self._lock:
            if path.is_symlink():
                raise OSError("Run record must not be a symlink")
            fd, temp_name = tempfile.mkstemp(prefix=".run-", suffix=".tmp", dir=self.directory)
            try:
                if os.name == "posix":
                    os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, path)
                if os.name == "posix":
                    path.chmod(0o600)
                    # Persist the directory entry as well as the file contents.
                    dir_fd = os.open(self.directory, os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                self._prune()
            except Exception:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise
        return record

    def load_recent(self) -> list[dict]:
        with self._lock:
            try:
                paths = [p for p in self.directory.glob("*.json")
                         if not p.is_symlink() and p.is_file()]
            except OSError:
                return []
            paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            records = []
            for path in paths[:self.max_records]:
                fd = None
                try:
                    if not _JOB_ID.fullmatch(path.stem):
                        continue
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    fd = os.open(path, flags)
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RECORD_BYTES:
                        continue
                    if os.name == "posix":
                        if info.st_uid != os.getuid():
                            continue
                        # Old versions or manually copied files may have broader modes.
                        # Tighten permissions before reading private run contents.
                        os.fchmod(fd, 0o600)
                    with os.fdopen(fd, "r", encoding="utf-8") as stream:
                        fd = None
                        raw = stream.read(_MAX_RECORD_BYTES + 1)
                    if len(raw.encode("utf-8")) > _MAX_RECORD_BYTES:
                        continue
                    record = json.loads(raw)
                    if (not isinstance(record, dict) or record.get("id") != path.stem
                            or not isinstance(record.get("status"), str)):
                        continue
                    record = self._record(record, update_timestamp=False)
                    if record["status"] in _ACTIVE_STATES:
                        record["status"] = "interrupted"
                        record["cancel_requested"] = False
                        record["progress"] = "Interrupted"
                        record["progress_detail"] = (
                            "The previous UI process ended. No action was replayed; inspect the run before retrying."
                        )
                        record.setdefault("events", []).append({
                            "level": "INTERRUPTED", "message": "Run interrupted by application restart",
                            "time": time.strftime("%H:%M:%S"),
                        })
                        record["events"] = record["events"][-MAX_JOB_EVENTS:]
                        try:
                            record = self.save(record)
                        except (OSError, ValueError, TypeError):
                            pass
                    records.append(record)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    continue
                finally:
                    if fd is not None:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
            records.sort(key=lambda item: float(item.get("updated", item.get("created", 0)) or 0),
                         reverse=True)
            return records

    def _prune(self) -> None:
        try:
            paths = [p for p in self.directory.glob("*.json") if not p.is_symlink() and p.is_file()]
            paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            for old in paths[self.max_records:]:
                old.unlink(missing_ok=True)
        except OSError:
            # Pruning must not invalidate a successfully committed job snapshot.
            pass
