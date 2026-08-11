"""Detection of the native toolchain the C++ modules depend on.

Days 11 and 12 need a compiler, CMake/CTest and a debugger.  Rather than
hard-coding whether those exist, the curriculum declares what a lesson
``requires`` and this module answers whether the machine can deliver it.  A
lesson the machine cannot verify is shown as unavailable *with the reason*,
never as content that quietly fails when you press Verify.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Searched after PATH, so a normally-installed toolchain always wins.  These
# are the locations installers actually use on Windows; on Linux and macOS the
# PATH lookup finds everything and this list stays unused.
_FALLBACK_DIRS = (
    Path(r"C:\dev\mingw64\bin"),
    Path(r"C:\mingw64\bin"),
    Path(r"C:\msys64\mingw64\bin"),
    Path(r"C:\msys64\ucrt64\bin"),
    Path(r"C:\Program Files\LLVM\bin"),
    Path(r"C:\Program Files\CMake\bin"),
)

# Environment overrides, for a toolchain in an unusual place.
_ENV_OVERRIDES = {
    "cmake": "FTL_CMAKE",
    "ctest": "FTL_CTEST",
    "cxx": "FTL_CXX",
    "gdb": "FTL_GDB",
    "ninja": "FTL_NINJA",
}

_CXX_CANDIDATES = ("g++", "clang++", "c++")


def _find(name: str) -> str | None:
    """Locate one executable: PATH first, then the venv, then known dirs."""
    found = shutil.which(name)
    if found:
        return found
    # cmake and ctest are commonly installed into the active virtualenv by
    # `pip install cmake`, which does not put Scripts/ on PATH by itself.
    venv_candidate = Path(sys.executable).parent / name
    for suffix in ("", ".exe"):
        candidate = venv_candidate.with_name(venv_candidate.name + suffix)
        if candidate.is_file():
            return str(candidate)
    for directory in _FALLBACK_DIRS:
        for suffix in ("", ".exe"):
            candidate = directory / (name + suffix)
            if candidate.is_file():
                return str(candidate)
    return None


@dataclass(frozen=True, slots=True)
class Toolchain:
    """Absolute paths to the native tools that were found, or None."""

    cmake: str | None = None
    ctest: str | None = None
    cxx: str | None = None
    gdb: str | None = None
    # Optional: a single-config generator. Without it CMake falls back to its
    # platform default, which on Windows is Visual Studio — a build GDB cannot
    # read symbols from.
    ninja: str | None = None

    @property
    def can_build_cpp(self) -> bool:
        return bool(self.cmake and self.ctest and self.cxx)

    @property
    def can_debug(self) -> bool:
        return bool(self.gdb)

    def satisfies(self, requirement: str) -> bool:
        if requirement == "cpp-build":
            return self.can_build_cpp
        if requirement == "gdb":
            return self.can_debug
        return False

    def missing_for(self, requirement: str) -> list[str]:
        """Which tools are absent — the detail the UI shows the learner."""
        if requirement == "cpp-build":
            return [
                name
                for name, path in (
                    ("cmake", self.cmake),
                    ("ctest", self.ctest),
                    ("a C++ compiler (g++/clang++)", self.cxx),
                )
                if not path
            ]
        if requirement == "gdb":
            return [] if self.gdb else ["gdb"]
        return [requirement]


def _detect_cxx() -> str | None:
    override = os.environ.get(_ENV_OVERRIDES["cxx"])
    if override and Path(override).is_file():
        return override
    for candidate in _CXX_CANDIDATES:
        found = _find(candidate)
        if found:
            return found
    return None


def detect_toolchain() -> Toolchain:
    """Fresh detection — no caching, so installing a compiler takes effect."""

    def resolve(name: str) -> str | None:
        override = os.environ.get(_ENV_OVERRIDES[name])
        if override and Path(override).is_file():
            return override
        return _find(name)

    return Toolchain(
        cmake=resolve("cmake"),
        ctest=resolve("ctest"),
        cxx=_detect_cxx(),
        gdb=resolve("gdb"),
        ninja=resolve("ninja"),
    )


@lru_cache(maxsize=1)
def cached_toolchain() -> Toolchain:
    """Detection result for one server run.

    Cached because the curriculum is validated on every load and probing the
    filesystem repeatedly is wasted work; restart the server after installing
    a toolchain (which the unavailable-lesson text tells the learner to do).
    """
    return detect_toolchain()


def requirement_reason(requirement: str, toolchain: Toolchain) -> str:
    """Human-readable explanation of why a requirement is not met."""
    missing = ", ".join(toolchain.missing_for(requirement))
    if requirement == "cpp-build":
        return (
            f"This module needs a C++ toolchain and none was found: {missing} "
            "is not on PATH. Install a compiler plus CMake (on Windows: "
            "`winget install BrechtSanders.WinLibs.POSIX.UCRT` and "
            "`pip install cmake ninja`; on Debian/Ubuntu: "
            "`apt install build-essential cmake gdb`), then restart "
            "`python -m learning` and this module unlocks itself."
        )
    if requirement == "gdb":
        return (
            "This module needs the GNU debugger and gdb is not on PATH. "
            "Install it (Windows: the WinLibs MinGW-w64 package ships gdb; "
            "Debian/Ubuntu: `apt install gdb`), then restart "
            "`python -m learning`."
        )
    return f"Unsatisfied requirement: {requirement} ({missing})."
