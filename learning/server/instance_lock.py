"""Single-instance guard for the learning server.

Two ``python -m learning`` processes each hold their own in-memory copy of the
progress file and each write the whole thing back.  The second writer wins and
the first learner's answers are gone — silently, because both UIs keep showing
the state their own process remembers.  The default port is chosen freely, so
nothing else stops the second instance from starting.

The guard is an OS-level advisory lock on a file beside the progress data.
That choice matters: the kernel drops the lock when the process dies, so a
crashed server leaves nothing to clean up and there is no stale-lock heuristic
to get wrong.  (Day 13 has the learner build the file-existence version of this
and discover exactly that failure mode.)
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class AlreadyRunning(RuntimeError):
    """Another learning server already holds the progress file."""


def _try_lock(handle: int) -> bool:
    """Take an exclusive, non-blocking lock. False when someone else has it."""
    try:
        if sys.platform == "win32":
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


@contextmanager
def single_instance(progress_path: Path) -> Iterator[None]:
    """Hold the lock for the block, or raise AlreadyRunning.

    Guards the *progress file*, not a port, because the progress file is what
    two instances corrupt.  Running two servers on two ports against separate
    progress files stays legal.
    """
    lock_path = progress_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if not _try_lock(handle):
            raise AlreadyRunning(
                f"another learning server is already using {progress_path}.\n"
                "Two servers would each overwrite the other's progress, so this "
                "one will not start.\n"
                "Close the other window, or run this one against its own "
                "progress file."
            )
        try:
            os.truncate(handle, 0)
            os.write(handle, f"{os.getpid()}\n".encode())
            yield
        finally:
            if sys.platform == "win32":
                os.lseek(handle, 0, os.SEEK_SET)
                # Suppressed because the close below releases it regardless.
                with contextlib.suppress(OSError):
                    msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        os.close(handle)
