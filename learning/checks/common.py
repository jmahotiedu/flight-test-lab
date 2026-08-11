"""Shared subprocess plumbing for validators.

All validators run child processes with argv arrays (never ``shell=True``),
bounded timeouts, and output capture.  On timeout the direct child is killed
and the result reports ``timed_out``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

DEFAULT_ENV_EXCLUDES = ("EVIDENCE_DIR",)


# Put each child in its own process group (POSIX) or job-control group
# (Windows) so it can be killed as a whole.  Validators start processes that
# start processes: pytest launches a DUT, a probe launches the simulator.
# Killing only the direct child on timeout leaves those grandchildren running
# — holding ports and writing logs — after the UI has reported a timeout.
if sys.platform == "win32":
    CREATION_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP
    START_NEW_SESSION = False
else:
    CREATION_FLAGS = 0
    START_NEW_SESSION = True


# Every validator subprocess currently running, so shutdown can reap them.
# Handler threads are daemons and their children live in their own process
# group, so Ctrl+C would otherwise let the learning server exit while a pytest
# run, a CMake build or a DUT it started keeps going — holding ports and
# writing logs after the UI said it stopped.
_ACTIVE: set[subprocess.Popen[str]] = set()
_ACTIVE_LOCK = threading.Lock()


def register_child(process: subprocess.Popen[str]) -> None:
    """Track a child started outside run_subprocess so shutdown can reap it.

    behavior_probe launches the DUT itself (it has to speak to it), so without
    this the process would be invisible to terminate_active_validators and
    could outlive the server that started it.
    """
    with _ACTIVE_LOCK:
        _ACTIVE.add(process)


def unregister_child(process: subprocess.Popen[str]) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(process)


def terminate_active_validators() -> int:
    """Kill every running validator process tree. Returns how many were live."""
    with _ACTIVE_LOCK:
        processes = list(_ACTIVE)
    reaped = 0
    for process in processes:
        if process.poll() is None:
            kill_process_tree(process)
            reaped += 1
    return reaped


def kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Terminate a child and everything it started."""
    if process.poll() is not None:
        return
    if sys.platform == "win32":
        # taskkill /T walks the child tree that CREATE_NEW_PROCESS_GROUP kept
        # attached to this pid; process.kill() alone would orphan it.
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=15,
                check=False,
            )
    else:
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    if process.poll() is None:
        with contextlib.suppress(OSError):
            process.kill()


def run_subprocess(
    argv: list[str],
    *,
    timeout: float,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
) -> tuple[int | None, str, str, int, bool]:
    """Run ``argv``; return (exit_status, stdout, stderr, duration_ms, timed_out)."""
    env = os.environ.copy()
    for key in DEFAULT_ENV_EXCLUDES:
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)

    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATION_FLAGS,
        start_new_session=START_NEW_SESSION,
    )
    with _ACTIVE_LOCK:
        _ACTIVE.add(process)
    timed_out = False
    try:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            kill_process_tree(process)
            try:
                stdout, stderr = process.communicate(timeout=5.0)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(process)
    duration_ms = int((time.monotonic() - started) * 1000)
    return process.returncode, stdout or "", stderr or "", duration_ms, timed_out


def reserve_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])
