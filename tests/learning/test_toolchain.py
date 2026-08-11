"""Toolchain detection, requirement-gated availability, and native-validator safety.

The important property here is honesty in both directions: on a machine with a
compiler the C++ lessons must be offered, and on a machine without one they
must be withheld *with a reason* rather than offered and then failing when the
learner presses Verify.  Both directions are tested by injecting a Toolchain,
so neither depends on what happens to be installed on the machine running the
suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from learning.checks import toolchain_check
from learning.server.curriculum import load_curriculum
from learning.server.toolchain import (
    Toolchain,
    detect_toolchain,
    requirement_reason,
)
from learning.server.validators import ValidatorContext, validator_names

REPO_ROOT = Path(__file__).resolve().parents[2]
LEARNING_ROOT = REPO_ROOT / "learning"

FULL = Toolchain(cmake="cmake", ctest="ctest", cxx="g++", gdb="gdb")
NO_TOOLCHAIN = Toolchain()
NO_DEBUGGER = Toolchain(cmake="cmake", ctest="ctest", cxx="g++", gdb=None)


def test_toolchain_capabilities() -> None:
    assert FULL.can_build_cpp and FULL.can_debug
    assert not NO_TOOLCHAIN.can_build_cpp and not NO_TOOLCHAIN.can_debug
    assert NO_DEBUGGER.can_build_cpp and not NO_DEBUGGER.can_debug
    assert FULL.satisfies("cpp-build") and FULL.satisfies("gdb")
    assert not FULL.satisfies("nonsense-requirement")


def test_missing_tools_are_named() -> None:
    missing = NO_TOOLCHAIN.missing_for("cpp-build")
    assert "cmake" in missing and "ctest" in missing
    assert NO_DEBUGGER.missing_for("gdb") == ["gdb"]
    assert NO_DEBUGGER.missing_for("cpp-build") == []


def test_requirement_reason_is_actionable() -> None:
    reason = requirement_reason("cpp-build", NO_TOOLCHAIN)
    assert "cmake" in reason and "install" in reason.lower()
    assert "python -m learning" in reason  # tells them how to pick it up


def test_detect_toolchain_returns_absolute_paths_or_none() -> None:
    detected = detect_toolchain()
    for tool in (detected.cmake, detected.ctest, detected.cxx, detected.gdb):
        if tool is not None:
            assert Path(tool).is_file(), f"{tool} was reported but does not exist"


def test_native_lessons_are_offered_when_the_toolchain_exists() -> None:
    curriculum = load_curriculum(LEARNING_ROOT, validator_names(), toolchain=FULL)
    native = [
        lesson for lesson in curriculum.lessons.values() if lesson.day in (11, 12)
    ]
    assert native
    for lesson in native:
        assert lesson.status == "available", f"{lesson.id} withheld despite a toolchain"
        assert lesson.blocks, f"{lesson.id} is available but has no content"


def test_native_lessons_are_withheld_with_a_reason_without_a_toolchain() -> None:
    curriculum = load_curriculum(
        LEARNING_ROOT, validator_names(), toolchain=NO_TOOLCHAIN
    )
    native = [
        lesson for lesson in curriculum.lessons.values() if lesson.day in (11, 12)
    ]
    assert native
    for lesson in native:
        assert lesson.status == "unavailable", f"{lesson.id} offered with no compiler"
        assert "cmake" in lesson.unavailable_reason


def test_debugging_lessons_need_gdb_specifically() -> None:
    """A compiler without a debugger unlocks Day 11 but not Day 12."""
    curriculum = load_curriculum(
        LEARNING_ROOT, validator_names(), toolchain=NO_DEBUGGER
    )
    day11 = [lesson for lesson in curriculum.lessons.values() if lesson.day == 11]
    day12 = [lesson for lesson in curriculum.lessons.values() if lesson.day == 12]
    assert all(lesson.status == "available" for lesson in day11)
    assert all(lesson.status == "unavailable" for lesson in day12)
    assert all("gdb" in lesson.unavailable_reason for lesson in day12)


def test_python_lessons_unaffected_by_a_missing_toolchain() -> None:
    curriculum = load_curriculum(
        LEARNING_ROOT, validator_names(), toolchain=NO_TOOLCHAIN
    )
    for lesson in curriculum.lessons.values():
        if lesson.day <= 10 or lesson.day >= 13:
            assert lesson.status == "available", (
                f"{lesson.id} should not need a compiler"
            )


def test_unknown_requires_entry_is_rejected(tmp_path: Path) -> None:
    """A typo in `requires` must fail the load, not silently gate a lesson."""
    root = tmp_path / "learning"
    (root / "curriculum" / "modules").mkdir(parents=True)
    (root / "curriculum" / "curriculum.json").write_text(
        '{"concepts": ["cpp-integration"], "days":'
        ' [{"day": 1, "title": "t", "module": "day01.json"}]}',
        encoding="utf-8",
    )
    (root / "curriculum" / "modules" / "day01.json").write_text(
        '{"lessons": [{"id": "l1", "title": "t", "objective": "o",'
        ' "estimated_minutes": 5, "requires": ["cpp-buidl"],'
        ' "blocks": [{"type": "learn", "id": "b", "text": "x"}]}]}',
        encoding="utf-8",
    )
    (root / "curriculum" / "interview.json").write_text(
        '{"questions": []}', encoding="utf-8"
    )
    with pytest.raises(Exception, match="cpp-buidl"):
        load_curriculum(root, validator_names(), toolchain=FULL)


# -- validator safety ------------------------------------------------------


def test_toolchain_check_rejects_tools_outside_the_allowlist() -> None:
    result = toolchain_check.run(
        {"tool": "powershell", "args": ["-Command", "echo pwned"]},
        ValidatorContext(repo_root=REPO_ROOT),
    )
    assert not result.passed
    assert "not allowlisted" in result.interpretation
    assert result.exit_status is None  # nothing was executed


def test_toolchain_check_rejects_an_artifact_path_escaping_the_repo() -> None:
    result = toolchain_check.run(
        {
            "tool": "cmake",
            "args": ["--version"],
            "artifact": "../../../../Windows/System32/drivers/etc/hosts",
        },
        ValidatorContext(repo_root=REPO_ROOT),
    )
    assert not result.passed
    assert "escapes the repository" in result.interpretation


def test_gdb_without_symbols_is_diagnosed_not_just_reported() -> None:
    """`?? ()` frames mean a non-DWARF build — say so, and how to fix it.

    Without this the learner sees only "output did not match /.../" for what is
    a build-provenance problem (CMake picked MSVC), not a mistake of theirs.
    """
    result = toolchain_check._evaluate(
        "gdb",
        {"expect_output_regex": "trigger_crash"},
        0,
        "#0  0x00007ff607f72032 in ?? ()\n#1  0x0000000000000000 in ?? ()",
        "",
        120,
        False,
        ValidatorContext(repo_root=REPO_ROOT),
    )
    assert not result.passed
    assert "GCC/Clang" in result.interpretation
    assert "-DCMAKE_CXX_COMPILER=g++" in result.interpretation


def test_ptrace_denial_is_diagnosed() -> None:
    """Yama's default ptrace_scope blocks sibling attach; say what to do."""
    result = toolchain_check._evaluate(
        "gdb-attach",
        {"expect_output_regex": "trigger_hang"},
        0,
        "",
        "ptrace: Operation not permitted.",
        90,
        False,
        ValidatorContext(repo_root=REPO_ROOT),
    )
    assert not result.passed
    assert "ptrace_scope" in result.interpretation


def test_symbolic_gdb_failures_are_not_misdiagnosed() -> None:
    """A real assertion failure must not be blamed on the toolchain."""
    result = toolchain_check._evaluate(
        "gdb",
        {"expect_output_regex": "trigger_hang"},
        0,
        "#0  dut::trigger_crash () at cpp/src/server.cpp:52",
        "",
        120,
        False,
        ValidatorContext(repo_root=REPO_ROOT),
    )
    assert not result.passed
    assert "GCC/Clang" not in result.interpretation


def test_toolchain_check_reports_a_missing_tool_truthfully() -> None:
    """With no gdb, the gdb lessons' validator must say so, not claim success."""
    context = ValidatorContext(repo_root=REPO_ROOT)
    original = toolchain_check.cached_toolchain
    toolchain_check.cached_toolchain = lambda: NO_DEBUGGER  # type: ignore[assignment]
    try:
        result = toolchain_check.run({"tool": "gdb", "args": ["--version"]}, context)
    finally:
        toolchain_check.cached_toolchain = original  # type: ignore[assignment]
    assert not result.passed
    assert "gdb was not found" in result.interpretation
