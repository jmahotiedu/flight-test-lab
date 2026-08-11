"""Validator for the native (C++/debugger) lessons.

Like every other validator, the browser cannot reach this with a command of
its own: it sends a lesson id, and the tool name plus arguments come from the
curriculum file on disk.  The tool name is additionally restricted to a fixed
allowlist here, so even a curriculum edit cannot turn this into a general
process launcher.
"""

from __future__ import annotations

import contextlib
import re
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from learning.checks.common import reserve_port, run_subprocess
from learning.server.toolchain import cached_toolchain
from learning.server.validators import (
    CheckResult,
    ValidatorContext,
    clamp_timeout,
    truncate,
)

# The only tools this validator will ever start.  "dut" is the C++ executable
# built from cpp/, resolved inside the repository — never taken from args.
ALLOWED_TOOLS = frozenset({"cmake", "ctest", "gdb", "dut", "gdb-attach"})

DUT_RELATIVE = Path("cpp") / "build" / "bin"


def _fail(name: str, interpretation: str, duration_ms: int = 0) -> CheckResult:
    return CheckResult(
        name=name,
        passed=False,
        exit_status=None,
        stdout="",
        stderr="",
        duration_ms=duration_ms,
        interpretation=interpretation,
    )


def _dut_path(context: ValidatorContext) -> Path | None:
    for candidate in ("dut.exe", "dut"):
        path = context.repo_root / DUT_RELATIVE / candidate
        if path.is_file():
            return path
    return None


def _resolve_tool(tool: str, context: ValidatorContext) -> tuple[str | None, str]:
    """Return (executable, error) for an allowlisted tool name."""
    toolchain = cached_toolchain()
    if tool == "cmake":
        return toolchain.cmake, "cmake was not found on this machine"
    if tool == "ctest":
        return toolchain.ctest, "ctest was not found on this machine"
    if tool in ("gdb", "gdb-attach"):
        return toolchain.gdb, "gdb was not found on this machine"
    if tool == "dut":
        path = _dut_path(context)
        return (
            str(path) if path else None,
            "the C++ DUT is not built yet — run the build step first "
            f"(expected {DUT_RELATIVE.as_posix()}/dut)",
        )
    return None, f"unknown tool {tool!r}"


def _evaluate(
    name: str,
    args: dict[str, Any],
    exit_status: int | None,
    stdout: str,
    stderr: str,
    duration_ms: int,
    timed_out: bool,
    context: ValidatorContext,
) -> CheckResult:
    combined = stdout + "\n" + stderr
    problems: list[str] = []

    if timed_out:
        problems.append("the command did not finish within the time limit")

    expect_exit = args.get("expect_exit", 0)
    if expect_exit is not None and exit_status != expect_exit:
        problems.append(f"expected exit status {expect_exit}, got {exit_status}")

    pattern = args.get("expect_output_regex")
    if isinstance(pattern, str) and not re.search(pattern, combined, re.MULTILINE):
        problems.append(f"output did not match /{pattern}/")

    forbidden = args.get("forbid_output_regex")
    if isinstance(forbidden, str) and re.search(forbidden, combined, re.MULTILINE):
        problems.append(f"output matched /{forbidden}/, which must not appear")

    artifact = args.get("artifact")
    if isinstance(artifact, str):
        # Repository-relative only: an absolute or escaping path is a bug in
        # the lesson definition, not something to silently resolve.
        target = (context.repo_root / artifact).resolve()
        if not target.is_relative_to(context.repo_root.resolve()):
            problems.append(f"artifact path {artifact!r} escapes the repository")
        elif not target.exists():
            problems.append(f"expected artifact {artifact} does not exist")

    passed = not problems
    if passed:
        interpretation = str(args.get("success_note", "Check passed."))
    else:
        interpretation = "Check failed: " + "; ".join(problems)
        # A GDB run full of `?? ()` frames is almost always a build-provenance
        # problem, not a learner mistake: GDB reads DWARF, and an MSVC build
        # ships PDBs it cannot read.  Say so, instead of leaving a bare
        # "output did not match" for the learner to decode.
        if name.startswith("gdb") and "?? ()" in combined:
            interpretation += (
                ". GDB resolved no symbol names (`?? ()` frames), which means "
                "this binary was not built by GCC/Clang — most likely CMake "
                "chose Visual Studio, whose PDB symbols GDB cannot read. "
                "Reconfigure with the GNU toolchain: cmake -S cpp -B cpp/build "
                "-G Ninja -DCMAKE_CXX_COMPILER=g++ && cmake --build cpp/build"
            )
    return CheckResult(
        name=name,
        passed=passed,
        exit_status=exit_status,
        stdout=truncate(stdout),
        stderr=truncate(stderr),
        duration_ms=duration_ms,
        interpretation=interpretation,
        timed_out=timed_out,
    )


def _run_gdb_attach(
    args: dict[str, Any], context: ValidatorContext, timeout: float
) -> CheckResult:
    """Start the built DUT, send it one request, then attach gdb to it.

    This is the only orchestrated mode: the hang exercise needs a live wedged
    process to inspect, which a single command cannot produce.  The DUT is
    always killed in the ``finally`` block, including on timeout — a debugging
    lesson that leaked processes would be a poor advertisement for Day 6.
    """
    gdb, gdb_error = _resolve_tool("gdb", context)
    if gdb is None:
        return _fail("gdb-attach", gdb_error)
    dut = _dut_path(context)
    if dut is None:
        return _fail("gdb-attach", "the C++ DUT is not built yet")

    port = reserve_port()
    dut_args = [str(item) for item in args.get("dut_args", [])]
    gdb_commands = [str(item) for item in args.get("gdb_commands", ["bt"])]
    started = time.monotonic()

    process = subprocess.Popen(
        [str(dut), "--host", "127.0.0.1", "--port", str(port), *dut_args],
        cwd=str(context.repo_root),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    try:
        deadline = time.monotonic() + min(15.0, timeout)
        connected = False
        while time.monotonic() < deadline and not connected:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1.0) as sock:
                    sock.sendall(b'{"command": "status", "sequence": 1}\n')
                    sock.settimeout(float(args.get("poke_timeout_seconds", 1.5)))
                    with contextlib.suppress(TimeoutError, OSError):
                        sock.recv(256)  # a hung DUT never answers; that is the point
                    connected = True
            except OSError:
                time.sleep(0.1)
        if not connected:
            return _fail(
                "gdb-attach",
                "the C++ DUT never accepted a connection, so there was nothing "
                "to attach to",
                int((time.monotonic() - started) * 1000),
            )

        argv = [gdb, "-p", str(process.pid), "-batch"]
        for command in gdb_commands:
            argv += ["-ex", command]
        exit_status, stdout, stderr, _, timed_out = run_subprocess(
            argv, timeout=timeout, cwd=context.repo_root
        )
    finally:
        process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)

    duration_ms = int((time.monotonic() - started) * 1000)
    return _evaluate(
        "gdb-attach",
        args,
        exit_status,
        stdout,
        stderr,
        duration_ms,
        timed_out,
        context,
    )


def run(args: dict[str, Any], context: ValidatorContext) -> CheckResult:
    tool = str(args.get("tool", ""))
    if tool not in ALLOWED_TOOLS:
        return _fail(
            "toolchain_check",
            f"tool {tool!r} is not allowlisted (allowed: "
            f"{', '.join(sorted(ALLOWED_TOOLS))})",
        )

    timeout = clamp_timeout(args)
    if tool == "gdb-attach":
        return _run_gdb_attach(args, context, timeout)

    executable, error = _resolve_tool(tool, context)
    if executable is None:
        return _fail(tool, error)

    argv = [executable, *[str(item) for item in args.get("args", [])]]
    exit_status, stdout, stderr, duration_ms, timed_out = run_subprocess(
        argv, timeout=timeout, cwd=context.repo_root
    )
    return _evaluate(
        tool, args, exit_status, stdout, stderr, duration_ms, timed_out, context
    )
