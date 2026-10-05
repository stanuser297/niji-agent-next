"""Bounded, owner-only task plans stored separately from chat transcripts."""
from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
from pathlib import Path

from .config import SESSION_DIR

MAX_PLAN_ITEMS = 60
MAX_PLAN_TEXT = 500
MAX_ACCEPTANCE_CRITERIA = 400
MAX_STEP_EVIDENCE = 800
_STATUSES = {"pending", "in_progress", "completed", "blocked"}
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def normalize_plan(items) -> list[dict]:
    """Validate steps and their dependency graph; return a bounded UI-safe plan."""
    if not isinstance(items, list) or len(items) > MAX_PLAN_ITEMS:
        raise ValueError(f"A plan must contain at most {MAX_PLAN_ITEMS} steps")
    result = []
    active = 0
    ids = set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError("Each plan step must be an object")
        content = item.get("content")
        status = item.get("status", "pending")
        active_form = item.get("activeForm", "")
        acceptance_criteria = item.get("acceptance_criteria", "")
        evidence = item.get("evidence", "")
        if not isinstance(acceptance_criteria, str) or len(acceptance_criteria) > MAX_ACCEPTANCE_CRITERIA:
            raise ValueError(f"acceptance_criteria must be text up to {MAX_ACCEPTANCE_CRITERIA} characters")
        if not isinstance(evidence, str) or len(evidence) > MAX_STEP_EVIDENCE:
            raise ValueError(f"evidence must be text up to {MAX_STEP_EVIDENCE} characters")
        if "id" not in item:
            step_id = f"step-{index}"
        elif isinstance(item["id"], str):
            step_id = item["id"].strip()
        else:
            raise ValueError("Step id must be a string")
        depends = item.get("depends_on", [])
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Every plan step needs a description")
        if not isinstance(status, str) or status not in _STATUSES:
            raise ValueError("Plan step status must be pending, in_progress, completed, or blocked")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", step_id):
            raise ValueError("Step ids may contain only letters, numbers, underscores, and hyphens")
        if step_id in ids:
            raise ValueError(f"Duplicate plan step id: {step_id}")
        ids.add(step_id)
        if not isinstance(depends, list) or len(depends) > MAX_PLAN_ITEMS:
            raise ValueError("depends_on must be an array of plan step ids")
        dependencies = []
        for dependency in depends:
            if not isinstance(dependency, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", dependency.strip()):
                raise ValueError("Every dependency must be a valid plan step id")
            dependency = dependency.strip()
            if dependency in dependencies:
                raise ValueError(f"Duplicate dependency on step {dependency}")
            dependencies.append(dependency)
        if not isinstance(active_form, str):
            active_form = ""
        if status == "in_progress":
            active += 1
        normalized = {
            "id": step_id,
            "content": content.strip()[:MAX_PLAN_TEXT],
            "status": status,
            "activeForm": active_form.strip()[:160],
        }
        if acceptance_criteria.strip():
            normalized["acceptance_criteria"] = acceptance_criteria.strip()
        if evidence.strip():
            normalized["evidence"] = evidence.strip()
        if dependencies:
            normalized["depends_on"] = dependencies
        result.append(normalized)
    if active > 1:
        raise ValueError("Only one plan step may be in progress")

    by_id = {item["id"]: item for item in result}
    positions = {item["id"]: index for index, item in enumerate(result)}
    for item in result:
        for dependency in item.get("depends_on", []):
            if dependency not in by_id:
                raise ValueError(f"Unknown dependency {dependency} for step {item['id']}")
            if dependency == item["id"]:
                raise ValueError(f"Step {item['id']} cannot depend on itself")
            if positions[dependency] > positions[item["id"]]:
                raise ValueError(f"Prerequisite {dependency} must appear before step {item['id']}")
            if item["status"] in {"in_progress", "completed"} and by_id[dependency]["status"] != "completed":
                raise ValueError(f"Step {item['id']} cannot be active or complete before {dependency} is completed")

    visiting = set()
    visited = set()
    def visit(step_id):
        if step_id in visiting:
            raise ValueError("Plan dependencies must not contain a cycle")
        if step_id in visited:
            return
        visiting.add(step_id)
        for dependency in by_id[step_id].get("depends_on", []):
            visit(dependency)
        visiting.remove(step_id)
        visited.add(step_id)
    for step_id in by_id:
        visit(step_id)
    return result


def has_concrete_step_evidence(value) -> bool:
    """Return whether evidence is substantive enough for an approved-step report."""
    if not isinstance(value, str):
        return False
    evidence = value.strip()
    if len(evidence) < 8:
        return False
    return evidence.casefold().strip(" .!?") not in {
        "ok", "done", "complete", "completed", "verified", "passed", "success"}


def validate_approved_plan_progress(candidate, approved, previous) -> list[dict]:
    """Validate that checklist updates execute only the exact approved plan in order."""
    approved_plan = normalize_plan(approved)
    current = normalize_plan(previous)
    updated = normalize_plan(candidate)
    if len(approved_plan) != len(updated) or len(current) != len(updated):
        raise ValueError("The checklist must keep the approved number of steps")
    for expected, before, after in zip(approved_plan, current, updated):
        for field in ("id", "content", "depends_on", "acceptance_criteria"):
            default = [] if field == "depends_on" else ""
            if (expected.get(field, default) != after.get(field, default)
                    or expected.get(field, default) != before.get(field, default)):
                raise ValueError("Approved step IDs, descriptions, order, criteria, and prerequisites cannot be changed")
        old_status, new_status = before["status"], after["status"]
        if old_status == "completed" and after.get("evidence", "") != before.get("evidence", ""):
            raise ValueError("Evidence for a completed approved step cannot be changed")
        allowed = {
            "pending": {"pending", "in_progress", "blocked"},
            "in_progress": {"in_progress", "completed", "blocked"},
            "completed": {"completed"},
            # A blocked step can be reset once its blocker is resolved, then resumed.
            "blocked": {"blocked", "pending"},
        }
        if new_status not in allowed[old_status]:
            raise ValueError(f"Invalid approved-plan step transition: {old_status} to {new_status}")
        if new_status in {"in_progress", "blocked"}:
            index = next(i for i, step in enumerate(updated) if step["id"] == after["id"])
            if any(step["status"] != "completed" for step in updated[:index]):
                raise ValueError("Complete earlier approved steps before starting or blocking this step")
            if any(next(item for item in updated if item["id"] == dep)["status"] != "completed"
                   for dep in after.get("depends_on", [])):
                raise ValueError("Complete every prerequisite before starting or blocking this step")
        if new_status == "completed" and old_status not in {"in_progress", "completed"}:
            raise ValueError("Mark a step in progress before marking it complete")
        if new_status == "completed" and not has_concrete_step_evidence(after.get("evidence", "")):
            raise ValueError("Add concise, concrete evidence before marking an approved step complete")
    return updated


def _path(session_id: str, root: str | Path | None = None) -> Path:
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ValueError("Invalid session id for plan storage")
    return Path(root or SESSION_DIR) / "plans" / f"{session_id}.json"


def _ensure_safe_directory(path: Path, *, create: bool) -> Path:
    """Check every existing path component without resolving through symlinks."""
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                raise OSError(f"Plan storage directory does not exist: {current}")
            try:
                current.mkdir(mode=0o700)
            except FileExistsError:
                pass
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError(f"Refusing unsafe plan storage path component: {current}")
    return absolute


def _open_directory_chain(path: Path, *, create: bool) -> int:
    """Open a directory without following any symlink in its path (POSIX)."""
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    current_fd = os.open(absolute.anchor, flags)
    try:
        for component in absolute.parts[1:]:
            try:
                next_fd = os.open(component, flags | nofollow, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                next_fd = os.open(component, flags | nofollow, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _open_child_directory(parent_fd: int, name: str, *, create: bool) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            raise
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        return os.open(name, flags, dir_fd=parent_fd)


def _supports_safe_dir_fd() -> bool:
    return (os.name == "posix" and hasattr(os, "O_NOFOLLOW")
            and os.open in getattr(os, "supports_dir_fd", set())
            and os.mkdir in getattr(os, "supports_dir_fd", set())
            and os.rename in getattr(os, "supports_dir_fd", set()))


def save_plan(session_id: str, items, root: str | Path | None = None) -> list[dict[str, str]]:
    """Atomically save one session's plan with owner-only filesystem permissions."""
    plan = normalize_plan(items)
    path = _path(session_id, root)
    payload = {"version": 1, "session_id": session_id, "items": plan}
    if _supports_safe_dir_fd():
        session_fd = _open_directory_chain(path.parent.parent, create=True)
        plans_fd = None
        temp_name = f".{session_id}-{secrets.token_hex(8)}.tmp"
        try:
            os.fchmod(session_fd, 0o700)
            plans_fd = _open_child_directory(session_fd, "plans", create=True)
            os.fchmod(plans_fd, 0o700)
            fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=plans_fd)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            # rename is an atomic replacement on POSIX and operates relative to
            # the already-open, no-follow directory descriptor.
            os.rename(temp_name, path.name, src_dir_fd=plans_fd, dst_dir_fd=plans_fd)
            temp_name = ""
        finally:
            if temp_name and plans_fd is not None:
                try:
                    os.unlink(temp_name, dir_fd=plans_fd)
                except FileNotFoundError:
                    pass
            if plans_fd is not None:
                os.close(plans_fd)
            os.close(session_fd)
    else:
        session_dir = _ensure_safe_directory(path.parent.parent, create=True)
        plans_dir = _ensure_safe_directory(path.parent, create=True)
        try:
            session_dir.chmod(0o700)
            plans_dir.chmod(0o700)
        except OSError:
            pass
        fd, temp_name = tempfile.mkstemp(prefix=f".{session_id}-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
    return plan


def _migrate_legacy_plan_ids(items) -> list[dict]:
    """Migrate old persisted IDs to safe stable IDs without dropping plan content."""
    if not isinstance(items, list):
        raise ValueError("Saved plan items must be an array")
    migrated = []
    old_to_new = {}
    ambiguous = set()
    used = set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError("Saved plan step must be an object")
        raw_id = item.get("id")
        old_id = raw_id if isinstance(raw_id, str) and raw_id else f"step-{index}"
        if isinstance(raw_id, str) and raw_id == raw_id.strip() and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", raw_id) and raw_id not in used:
            new_id = raw_id
        else:
            new_id = f"legacy-step-{index}"
            suffix = 1
            while new_id in used:
                new_id = f"legacy-step-{index}-{suffix}"
                suffix += 1
        used.add(new_id)
        if old_id in old_to_new:
            old_to_new.pop(old_id, None)
            ambiguous.add(old_id)
        elif old_id not in ambiguous:
            old_to_new[old_id] = new_id
        migrated.append({**item, "id": new_id})
    for item in migrated:
        dependencies = item.get("depends_on", [])
        if not isinstance(dependencies, list):
            raise ValueError("Legacy plan dependencies must be an array")
        remapped = []
        for dependency in dependencies:
            if not isinstance(dependency, str) or dependency in ambiguous or dependency not in old_to_new:
                raise ValueError("Legacy plan dependency cannot be safely migrated")
            remapped.append(old_to_new[dependency])
        item["depends_on"] = remapped
    return migrated


def load_plan(session_id: str, root: str | Path | None = None) -> list[dict]:
    """Load a valid local plan and safely migrate legacy step IDs when possible."""
    try:
        path = _path(session_id, root)
        if _supports_safe_dir_fd():
            session_fd = _open_directory_chain(path.parent.parent, create=False)
            try:
                os.fchmod(session_fd, 0o700)
                plans_fd = _open_child_directory(session_fd, "plans", create=False)
                try:
                    os.fchmod(plans_fd, 0o700)
                    file_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                      dir_fd=plans_fd)
                    with os.fdopen(file_fd, "r", encoding="utf-8") as handle:
                        info = os.fstat(handle.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size > 64_000:
                            return []
                        if stat.S_IMODE(info.st_mode) & 0o077:
                            os.fchmod(handle.fileno(), 0o600)
                        if stat.S_IMODE(os.fstat(handle.fileno()).st_mode) & 0o077:
                            return []
                        payload = json.load(handle)
                finally:
                    os.close(plans_fd)
            finally:
                os.close(session_fd)
        else:
            session_dir = _ensure_safe_directory(path.parent.parent, create=False)
            plans_dir = _ensure_safe_directory(path.parent, create=False)
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64_000:
                return []
            if os.name == "posix":
                for directory in (session_dir, plans_dir):
                    if stat.S_IMODE(directory.stat().st_mode) & 0o077:
                        directory.chmod(0o700)
                    if stat.S_IMODE(directory.stat().st_mode) & 0o077:
                        return []
                if stat.S_IMODE(path.stat().st_mode) & 0o077:
                    path.chmod(0o600)
                if stat.S_IMODE(path.stat().st_mode) & 0o077:
                    return []
            payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("session_id") != session_id:
            return []
        items = payload.get("items", [])
        try:
            return normalize_plan(items)
        except ValueError:
            return normalize_plan(_migrate_legacy_plan_ids(items))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return []


def extract_plan_steps(markdown: str) -> list[dict[str, str]]:
    """Extract numbered/bulleted steps from a tool-free plan response for UI display."""
    if not isinstance(markdown, str):
        return []
    markdown = markdown.replace("\\n", "\n")
    lines = markdown.splitlines()
    started = False
    first_nonempty_seen = False
    items = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        heading = stripped.lstrip("#* ").rstrip(":* ").lower()
        is_section = (stripped.startswith("#") or
                      (stripped.endswith(":") and len(stripped) < 100))
        if not started and ("step" in heading or heading in {"plan", "approach", "proposed plan"}):
            started = True
            first_nonempty_seen = True
            continue
        match = re.match(r"^(?:\d{1,2}[.)]|[-*])\s+(.+)$", stripped)
        # Accept a standalone ordered/bulleted plan only when it starts the response.
        # Do not mistake a numbered list embedded in unrelated prose for a task plan.
        if not started and not first_nonempty_seen and match:
            started = True
        first_nonempty_seen = True
        if started and is_section and any(
                key in heading for key in ("risk", "assumption", "verification", "check", "note")):
            break
        if match and started:
            value = re.sub(r"\s+", " ", match.group(1)).strip()
            if value:
                items.append({"id": f"step-{len(items) + 1}", "content": value,
                              "status": "pending", "activeForm": ""})
                if len(items) >= MAX_PLAN_ITEMS:
                    break
        elif started and items and stripped and not stripped.startswith("#"):
            # Continue a wrapped description without swallowing another section.
            items[-1]["content"] = (items[-1]["content"] + " " + stripped)[:MAX_PLAN_TEXT]
    try:
        return normalize_plan(items)
    except ValueError:
        return []
