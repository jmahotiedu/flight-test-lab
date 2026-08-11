"""Launch the real DUT from the repo and verify protocol behavior.

This is the workhorse validator: it starts ``python -m simulator.simulator``
on a reserved loopback port (with any curriculum-defined fault arguments),
speaks line-delimited JSON to it, and checks actual responses — including
deliberate timeouts, closed connections, and malformed input.  The child
process is always reaped in ``finally``.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from learning.checks.common import (
    CREATION_FLAGS,
    START_NEW_SESSION,
    kill_process_tree,
    register_child,
    reserve_port,
    unregister_child,
)
from learning.server.validators import (
    CheckResult,
    ValidatorContext,
    clamp_timeout,
    truncate,
)


def _wait_for_port(host: str, port: int, deadline_seconds: float) -> bool:
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _matches(expect: dict[str, Any], payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return [f"expected a JSON object, got {type(payload).__name__}"]
    misses = []
    for key, value in expect.items():
        if payload.get(key) != value:
            misses.append(f"expected {key}={value!r}, got {payload.get(key)!r}")
    return misses


def _run_steps(
    connection: socket.socket, steps: list[dict[str, Any]], response_timeout: float
) -> tuple[list[str], str]:
    """Execute protocol steps; return (failures, transcript)."""
    failures: list[str] = []
    transcript: list[str] = []
    connection.settimeout(response_timeout)
    reader = connection.makefile("r", encoding="utf-8", newline="\n")
    try:
        for index, step in enumerate(steps):
            label = f"step {index + 1}"
            send_obj = step.get("send")
            send_raw = step.get("send_raw")
            if send_obj is not None:
                wire = json.dumps(send_obj, sort_keys=True) + "\n"
            elif isinstance(send_raw, str):
                wire = send_raw if send_raw.endswith("\n") else send_raw + "\n"
            else:
                wire = ""
            if wire:
                try:
                    connection.sendall(wire.encode("utf-8"))
                    transcript.append(f">> {wire.rstrip()}")
                except OSError as exc:
                    if step.get("expect_send_error"):
                        transcript.append(f">> send failed as expected ({exc})")
                        continue
                    failures.append(f"{label}: send failed: {exc}")
                    break

            started = time.monotonic()
            try:
                line = reader.readline()
            except (TimeoutError, OSError):
                elapsed_ms = int((time.monotonic() - started) * 1000)
                if step.get("expect_timeout"):
                    transcript.append(f"<< (timeout after {elapsed_ms} ms, expected)")
                    continue
                failures.append(f"{label}: timed out waiting for a response")
                break
            elapsed_ms = int((time.monotonic() - started) * 1000)

            if line == "":
                if step.get("expect_closed"):
                    transcript.append("<< (connection closed, expected)")
                    continue
                failures.append(f"{label}: connection closed unexpectedly")
                break
            transcript.append(f"<< {line.rstrip()} ({elapsed_ms} ms)")

            if step.get("expect_timeout") or step.get("expect_closed"):
                failures.append(f"{label}: expected no response, but received one")
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                if step.get("expect_invalid_json"):
                    continue
                failures.append(f"{label}: response was not valid JSON: {line!r}")
                continue
            if step.get("expect_invalid_json"):
                # The inverse assertion matters as much as the first one: if
                # the malformed-response fault silently stopped working, the
                # DUT would answer with a perfectly valid object and the check
                # would pass having proved the opposite of its intent.
                failures.append(
                    f"{label}: expected an unparseable response, but the DUT "
                    f"returned valid JSON: {line.rstrip()!r}"
                )
                continue
            expect = step.get("expect")
            if isinstance(expect, dict):
                failures.extend(f"{label}: {m}" for m in _matches(expect, payload))
            max_ms = step.get("max_elapsed_ms")
            if isinstance(max_ms, (int, float)) and elapsed_ms > max_ms:
                failures.append(
                    f"{label}: response took {elapsed_ms} ms, "
                    f"over the {max_ms} ms budget"
                )
            min_ms = step.get("min_elapsed_ms")
            if isinstance(min_ms, (int, float)) and elapsed_ms < min_ms:
                failures.append(
                    f"{label}: response took {elapsed_ms} ms, expected at least "
                    f"{min_ms} ms (fault did not engage?)"
                )
    finally:
        reader.close()
    return failures, "\n".join(transcript)


def _dut_argv(
    which: str, context: ValidatorContext, host: str, port: int, log_path: Path
) -> list[str]:
    """argv for the requested DUT implementation.

    ``"python"`` (the default) runs the simulator module; ``"cpp"`` runs the
    binary built from cpp/, which speaks the identical protocol.  Anything
    else is a bug in the lesson definition, not a request to run it.
    """
    common = ["--host", host, "--port", str(port), "--log-file", str(log_path)]
    if which == "cpp":
        for name in ("dut.exe", "dut"):
            binary = context.repo_root / "cpp" / "build" / "bin" / name
            if binary.is_file():
                return [str(binary), *common]
        raise FileNotFoundError(
            "the C++ DUT is not built — run: cmake -S cpp -B cpp/build && "
            "cmake --build cpp/build"
        )
    if which != "python":
        raise ValueError(
            f"behavior_probe 'dut' must be 'python' or 'cpp', got {which!r}"
        )
    return [context.python, "-m", "simulator.simulator", *common]


def run(args: dict[str, Any], context: ValidatorContext) -> CheckResult:
    dut_args = args.get("dut_args", [])
    if not isinstance(dut_args, list) or not all(isinstance(a, str) for a in dut_args):
        raise ValueError("behavior_probe 'dut_args' must be a list of strings")
    which_dut = str(args.get("dut", "python"))
    steps = args.get("steps", [])
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        raise ValueError("behavior_probe 'steps' must be a list of objects")
    response_timeout = float(args.get("response_timeout", 1.5))
    startup_timeout = float(args.get("startup_timeout", 5.0))
    wait_ready = bool(args.get("wait_ready", True))
    log_expect = args.get("log_expect_regex")
    overall_timeout = clamp_timeout(args)

    host = "127.0.0.1"
    port = reserve_port()
    started = time.monotonic()
    failures: list[str] = []
    transcript = ""
    stdout_tail = ""
    exit_status: int | None = None
    timed_out = False

    with tempfile.TemporaryDirectory(prefix="ftl-dut-") as sandbox:
        log_path = Path(sandbox) / "dut.log"
        try:
            argv = [*_dut_argv(which_dut, context, host, port, log_path), *dut_args]
        except FileNotFoundError as exc:
            return CheckResult(
                name="behavior_probe",
                passed=False,
                exit_status=None,
                stdout="",
                stderr="",
                duration_ms=int((time.monotonic() - started) * 1000),
                interpretation=f"Check failed: {exc}",
            )
        process = subprocess.Popen(
            argv,
            cwd=str(context.repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATION_FLAGS,
            start_new_session=START_NEW_SESSION,
        )
        # Tracked so Ctrl+C during this probe reaps the DUT: the handler thread
        # is a daemon and this child has its own process group, so nothing else
        # would stop it holding its port after the server exits.
        register_child(process)
        try:
            ready = _wait_for_port(host, port, startup_timeout) if wait_ready else True
            if not ready:
                failures.append(
                    f"DUT did not accept connections within {startup_timeout:.1f}s"
                )
            elif process.poll() is not None:
                failures.append(f"DUT exited early with code {process.returncode}")
            elif steps:
                with socket.create_connection(
                    (host, port), timeout=response_timeout
                ) as connection:
                    failures, transcript = _run_steps(
                        connection, steps, response_timeout
                    )
            if isinstance(log_expect, str):
                import re

                time.sleep(0.2)  # allow the file handler to flush
                log_text = (
                    log_path.read_text(encoding="utf-8", errors="replace")
                    if log_path.exists()
                    else ""
                )
                if not re.search(log_expect, log_text):
                    failures.append(f"DUT log did not match /{log_expect}/")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    kill_process_tree(process)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(timeout=3.0)
            unregister_child(process)
            exit_status = process.returncode
            try:
                if process.stdout is not None:
                    stdout_tail = process.stdout.read() or ""
            except OSError:
                stdout_tail = ""

        duration_ms = int((time.monotonic() - started) * 1000)
        timed_out = duration_ms > overall_timeout * 1000
        if timed_out:
            failures.append(f"probe exceeded the {overall_timeout:.0f}s budget")

    expect_early_exit = args.get("expect_dut_exit")
    if isinstance(expect_early_exit, int) and exit_status != expect_early_exit:
        failures.append(
            f"expected DUT exit code {expect_early_exit}, got {exit_status}"
        )

    passed = not failures
    if passed:
        interpretation = str(
            args.get("success_note", "DUT behaved exactly as expected.")
        )
    else:
        interpretation = "Check failed: " + "; ".join(failures)

    return CheckResult(
        name="behavior_probe",
        passed=passed,
        exit_status=exit_status,
        stdout=truncate(transcript),
        stderr=truncate(stdout_tail),
        duration_ms=duration_ms,
        interpretation=interpretation,
        timed_out=timed_out,
    )
