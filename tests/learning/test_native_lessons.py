"""Run the Day 11-12 lesson validators for real.

Every other test in this directory checks the platform around the curriculum.
These run the native lessons' own mandatory checks — the CMake build, CTest,
cross-DUT parity, a GDB backtrace and a GDB attach to a deadlocked process —
because a lesson whose Verify button cannot pass is worse than no lesson.

They skip on a machine without the toolchain, and they are the only coverage
of the GDB-attach path on Linux, where a sibling attach is denied unless the
DUT opts in via prctl(PR_SET_PTRACER). That opt-in cannot be verified on
Windows, so CI is where it gets proven.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from learning.server.curriculum import Lesson, load_curriculum
from learning.server.toolchain import cached_toolchain
from learning.server.validators import (
    ValidatorContext,
    run_validator,
    validator_names,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTEXT = ValidatorContext(repo_root=REPO_ROOT)


def _native_lessons() -> list[Lesson]:
    curriculum = load_curriculum(REPO_ROOT / "learning", validator_names())
    return [
        lesson
        for lesson in curriculum.lessons.values()
        if lesson.day in (11, 12) and lesson.status == "available"
    ]


def _verify_blocks() -> list[tuple[str, dict[str, Any]]]:
    blocks: list[tuple[str, dict[str, Any]]] = []
    for lesson in _native_lessons():
        for block in lesson.blocks:
            if block["type"] == "verify" and block.get("mandatory"):
                blocks.append((f"{lesson.id}:{block['id']}", block))
    return blocks


def _cpp_dut_built() -> bool:
    return any(
        (REPO_ROOT / "cpp" / "build" / "bin" / name).is_file()
        for name in ("dut.exe", "dut")
    )


NATIVE_BLOCKS = _verify_blocks()


@pytest.mark.skipif(
    not cached_toolchain().can_build_cpp,
    reason="no C++ toolchain: the native lessons are unavailable here",
)
@pytest.mark.skipif(
    not _cpp_dut_built(),
    reason="cpp/build is not built; run cmake -S cpp -B cpp/build first",
)
# d11-port-your-commands asserts the post-port state: its checks fail from the
# moment Day 2 adds `ping` to the Python DUT until that lesson ports it. Same
# marker as the parity test itself, so gates that run before Day 11 can exclude
# the whole set rather than each half of it.
@pytest.mark.parametrize(
    "block",
    [
        pytest.param(
            block,
            marks=pytest.mark.port_parity
            if name.startswith("d11-port-your-commands:")
            else (),
        )
        for name, block in NATIVE_BLOCKS
    ],
    ids=[name for name, _ in NATIVE_BLOCKS] or ["none"],
)
def test_native_lesson_validator_passes(block: dict[str, Any]) -> None:
    """Each mandatory Day 11-12 check must actually pass on this machine."""
    result = run_validator(block["validator"], block.get("args", {}), CONTEXT)
    assert result.passed, (
        f"{block['id']} ({block['validator']}) failed: {result.interpretation}\n"
        f"--- stdout ---\n{result.stdout[-1500:]}\n"
        f"--- stderr ---\n{result.stderr[-1500:]}"
    )
