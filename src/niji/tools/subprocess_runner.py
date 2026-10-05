"""Bounded, cancellable subprocess runner shared by CLI and browser tool paths."""
from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time


def _stop_process(proc: subprocess.Popen, force: bool = False) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif force:
            proc.kill()
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill() if force else proc.terminate()
        except OSError:
            pass


def run_process(command, *, cwd=None, timeout=120, env=None, shell=False,
                ctx=None, tool_name="bash"):
    """Return (exit_code, combined_output, timed_out, cancelled).

    Output is drained continuously to avoid pipe deadlocks. Long commands emit
    concise periodic activity events; the output itself is returned at finish.
    """
    timeout = max(1, min(int(timeout), 120))
    agent = (ctx or {}).get("agent")
    proc = subprocess.Popen(
        command, shell=shell, cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        bufsize=1, env=env, start_new_session=(os.name == "posix"),
    )
    chunks = []
    output_size = 0
    lines: queue.Queue = queue.Queue(maxsize=512)
    sentinel = object()

    def read_output():
        try:
            for line in proc.stdout:
                try:
                    lines.put_nowait(line)
                except queue.Full:
                    try:
                        lines.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        lines.put_nowait(line)
                    except queue.Full:
                        pass
        finally:
            if proc.stdout is not None:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
            lines.put(sentinel)

    reader = threading.Thread(target=read_output, name="niji-process-output", daemon=True)
    reader.start()
    deadline = time.monotonic() + timeout
    last_progress = time.monotonic()
    output_done = False
    timed_out = cancelled = False
    terminate_at = None
    forced_kill = False
    leader_exit_at = None
    while proc.poll() is None or not output_done or not lines.empty():
        now = time.monotonic()
        if proc.poll() is not None and leader_exit_at is None:
            leader_exit_at = now
        cancel_event = getattr(agent, "_cancel_event", None)
        if cancel_event is not None and cancel_event.is_set() and not cancelled:
            cancelled = True
            terminate_at = now
            _stop_process(proc)
        if now >= deadline and proc.poll() is None and not timed_out:
            timed_out = True
            terminate_at = now
            _stop_process(proc)
        # Kill any surviving descendants if cancellation/timeout leaves the
        # original shell exited but its child still holds the output pipe open.
        if (not forced_kill and terminate_at is not None and now - terminate_at >= 1.2):
            _stop_process(proc, force=True)
            forced_kill = True
        if (not forced_kill and leader_exit_at is not None and not output_done
                and now - leader_exit_at >= 2.5):
            _stop_process(proc, force=True)
            forced_kill = True
        try:
            item = lines.get(timeout=0.2)
            if item is sentinel:
                output_done = True
            else:
                output_size += len(item)
                chunks.append(item)
                # Keep memory bounded while retaining both beginning and end.
                if output_size > 24000:
                    joined = "".join(chunks)
                    chunks = [joined[:10000], "\n… [process output truncated] …\n", joined[-10000:]]
                    output_size = sum(map(len, chunks))
                if (now - last_progress >= 3.0 and agent is not None
                        and callable(getattr(agent, "_record_activity", None))):
                    agent._record_activity(
                        "TOOL_PROGRESS",
                        f"Tool call: {tool_name} · still running ({int(now - (deadline - timeout))}s)",
                    )
                    last_progress = now
        except queue.Empty:
            if proc.poll() is None and now - last_progress >= 8.0 and agent is not None:
                agent._record_activity(
                    "TOOL_PROGRESS",
                    f"Tool call: {tool_name} · still running ({int(now - (deadline - timeout))}s)",
                )
                last_progress = now
    try:
        code = proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _stop_process(proc, force=True)
        code = proc.wait()
    reader.join()
    output = "".join(chunks)
    return code, output, timed_out, cancelled
