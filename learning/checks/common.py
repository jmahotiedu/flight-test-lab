"""Shared subprocess plumbing for validators.

All validators run child processes with argv arrays (never ``shell=True``),
bounded timeouts, and output capture.  On timeout the direct child is killed
and the result reports ``timed_out``.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

DEFAULT_ENV_EXCLUDES = ("EVIDENCE_DIR",)


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
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        try:
            stdout, stderr = process.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    duration_ms = int((time.monotonic() - started) * 1000)
    return process.returncode, stdout or "", stderr or "", duration_ms, timed_out


def reserve_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])
