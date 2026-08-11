"""The single-instance guard, exercised across real processes.

Two `python -m learning` servers each hold their own copy of the progress file
and write the whole thing back, so the second to save silently discards the
first's answers. An in-process test cannot show that; these spawn.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from learning.server.instance_lock import AlreadyRunning, single_instance

REPO_ROOT = Path(__file__).resolve().parents[2]

HOLDER = textwrap.dedent(
    """
    import pathlib, sys, time
    from learning.server.instance_lock import single_instance

    with single_instance(pathlib.Path(sys.argv[1])):
        print("HELD", flush=True)
        time.sleep(float(sys.argv[2]))
    print("RELEASED", flush=True)
    """
)


@pytest.fixture()
def holder_script(tmp_path: Path) -> Path:
    script = tmp_path / "holder.py"
    script.write_text(HOLDER, encoding="utf-8")
    return script


def test_a_second_instance_is_refused(holder_script: Path, tmp_path: Path) -> None:
    progress = tmp_path / ".progress.json"
    first = subprocess.Popen(
        [sys.executable, str(holder_script), str(progress), "30"],
        stdout=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
    )
    try:
        assert first.stdout is not None
        assert first.stdout.readline().strip() == "HELD"
        second = subprocess.run(
            [sys.executable, str(holder_script), str(progress), "0"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(REPO_ROOT),
            check=False,
        )
        assert second.returncode != 0
        assert "AlreadyRunning" in second.stderr
    finally:
        first.kill()
        first.wait(timeout=30)


def test_the_lock_does_not_survive_the_process_that_held_it(
    holder_script: Path, tmp_path: Path
) -> None:
    """An OS lock, not a lock file, so a crash leaves nothing to clean up.

    The file-existence version of this — which Day 13 has the learner build —
    would leave the bench claimed forever after a kill -9, and the recovery
    heuristic for that is exactly what this design avoids needing.
    """
    progress = tmp_path / ".progress.json"
    first = subprocess.Popen(
        [sys.executable, str(holder_script), str(progress), "30"],
        stdout=subprocess.PIPE,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert first.stdout is not None
    assert first.stdout.readline().strip() == "HELD"
    first.kill()  # no clean exit, no chance to release anything
    first.wait(timeout=30)

    after = subprocess.run(
        [sys.executable, str(holder_script), str(progress), "0"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(REPO_ROOT),
        check=False,
    )
    assert after.returncode == 0, after.stderr
    assert "HELD" in after.stdout


def test_the_guard_releases_on_exit(tmp_path: Path) -> None:
    progress = tmp_path / ".progress.json"
    with single_instance(progress):
        pass
    with single_instance(progress):
        pass  # reacquiring in the same process must work


def test_separate_progress_files_do_not_block_each_other(tmp_path: Path) -> None:
    """The guard protects the progress file, not the program.

    Two servers on two ports against two progress files are a legitimate
    setup, and locking the program rather than the shared state would forbid
    it for no reason.
    """
    with single_instance(tmp_path / "a.json"), single_instance(tmp_path / "b.json"):
        pass


def _reacquire(progress: Path) -> None:
    with single_instance(progress):
        pass


def test_the_guard_raises_the_documented_error(tmp_path: Path) -> None:
    progress = tmp_path / ".progress.json"
    with single_instance(progress), pytest.raises(AlreadyRunning, match="already"):
        _reacquire(progress)
