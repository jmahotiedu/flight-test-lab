"""Parity tests for the C++ DUT (REQ-CPP-001).

The point of a second implementation is that the harness should not care which
one it is talking to.  These tests prove that by driving both DUTs with the
same bytes and comparing the bytes that come back — not by comparing decoded
dicts, which would hide a formatting difference that a stricter client, or a
log-diffing tool, would trip over.

Every test here skips (never fails) when cpp/build has not been built, so the
suite stays green on a machine without a C++ toolchain.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import cpp_dut_path, reserve_local_port
from testlab.client import LabClient

REPO_ROOT = Path(__file__).resolve().parents[1]

# Requests chosen to hit every branch of build_response, plus the shapes that
# tempt an implementation into diverging: absent vs. explicitly-null command,
# a non-integer sequence, a nested object, and malformed input.
PARITY_REQUESTS = (
    '{"command": "status", "sequence": 1}',
    '{"command": "launch", "sequence": 7}',
    '{"sequence": 2}',
    '{"command": null, "sequence": 2}',
    '{"command": "status"}',
    '{"command": 5, "sequence": 3}',
    '{"command": "status", "sequence": "abc"}',
    '{"command": "status", "sequence": 1, "extra": {"a": [1, 2]}}',
    "{not json",
    "",
    "[1, 2, 3]",
    # Malformed numbers: a scanner that is merely permissive (or that leans on
    # strtod, which stops at the first bad character and reports success)
    # accepts these while Python rejects the whole document.
    '{"command": "status", "sequence": 1+2}',
    '{"command": "status", "sequence": 1.2.3}',
    '{"command": "status", "sequence": 1e}',
    '{"command": "status", "sequence": 01}',
    '{"command": "status", "sequence": .5}',
    '{"command": "status", "sequence": 1.}',
    '{"command": "status", "sequence": +1}',
    # Well-formed numbers that must survive intact, including the ones whose
    # *formatting* differs between a naive C++ serialiser and Python's float
    # repr (1.5e3 must print as 1500.0, not 1500), overflow/underflow, and
    # integers beyond int64 — Python has bignums and echoes them exactly.
    '{"command": "status", "sequence": -12}',
    '{"command": "status", "sequence": 1.5e3}',
    '{"command": "status", "sequence": 0}',
    '{"command": "status", "sequence": 1.0}',
    '{"command": "status", "sequence": -0.0}',
    '{"command": "status", "sequence": 0.1}',
    '{"command": "status", "sequence": 3.141592653589793}',
    '{"command": "status", "sequence": 1E3}',
    '{"command": "status", "sequence": 1e400}',
    '{"command": "status", "sequence": 1e-400}',
    '{"command": "status", "sequence": 9223372036854775807}',
    '{"command": "status", "sequence": 9223372036854775808}',
    '{"command": "status", "sequence": -99999999999999999999}',
    # Structural edge cases.
    '{"command": "status", "sequence": 1,}',
    '{"command": "status" "sequence": 1}',
    '{"command": "sta\\u0074us", "sequence": 1}',
)

# Commands the course has the learner add to the Python DUT (Day 2 adds ping,
# Day 9 adds counter).  They are not part of the shipped protocol, so on an
# untouched checkout both DUTs reject them identically.
COURSE_ADDED_COMMANDS = ("ping", "counter")


def _wait_ready(host: str, port: int, deadline_seconds: float = 10.0) -> None:
    client = LabClient(host, port)
    try:
        client.wait_until_ready(deadline_seconds=deadline_seconds)
    finally:
        client.close()


def _start(argv: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


def _ask_raw(port: int, request: str, timeout: float = 5.0) -> str:
    """Send one raw line and return the raw reply line, bytes unmodified."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request.encode("utf-8") + b"\n")
        return sock.makefile("rb").readline().decode("utf-8").rstrip("\n")


@pytest.fixture(scope="module")
def cpp_dut(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    binary = cpp_dut_path()
    if binary is None:
        pytest.skip(
            "C++ DUT not built: cmake -S cpp -B cpp/build && cmake --build cpp/build"
        )
    port = reserve_local_port()
    log_path = tmp_path_factory.mktemp("cpp-dut") / "dut.log"
    process = _start(
        [
            str(binary),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-file",
            str(log_path),
        ]
    )
    try:
        _wait_ready("127.0.0.1", port)
        yield port
    finally:
        _stop(process)


@pytest.fixture(scope="module")
def python_dut(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    port = reserve_local_port()
    log_path = tmp_path_factory.mktemp("py-dut") / "dut.log"
    process = _start(
        [
            sys.executable,
            "-m",
            "simulator.simulator",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-file",
            str(log_path),
        ]
    )
    try:
        _wait_ready("127.0.0.1", port)
        yield port
    finally:
        _stop(process)


@pytest.mark.requirement("REQ-CPP-001")
@pytest.mark.parametrize("request_line", PARITY_REQUESTS)
def test_cpp_dut_is_byte_identical_to_python(
    request_line: str, cpp_dut: int, python_dut: int
) -> None:
    """Both implementations must answer identically, byte for byte."""
    assert _ask_raw(cpp_dut, request_line) == _ask_raw(python_dut, request_line)


@pytest.mark.requirement("REQ-CPP-001")
@pytest.mark.parametrize("command", COURSE_ADDED_COMMANDS)
def test_course_added_commands_do_not_diverge(
    command: str, cpp_dut: int, python_dut: int
) -> None:
    """If the course had you extend one DUT, the other must follow.

    Day 2 adds `ping` to the Python DUT and Day 9 adds `counter`.  Nothing
    stops a learner from doing that and then certifying "parity" against a C++
    DUT that never grew those branches, so the divergence is checked directly.
    On an untouched checkout both reject the command identically and this
    passes; once one side implements it, this fails until the other does too.
    """
    request = json.dumps({"command": command, "sequence": 1}, sort_keys=True)
    python_reply = _ask_raw(python_dut, request)
    cpp_reply = _ask_raw(cpp_dut, request)
    assert python_reply == cpp_reply, (
        f"the two DUTs disagree about {command!r}: the course adds it to the "
        f"Python DUT, so it has to be ported to cpp/ as well.\n"
        f"  python: {python_reply}\n  cpp:    {cpp_reply}"
    )


@pytest.mark.requirement("REQ-CPP-001")
def test_cpp_dut_answers_status_within_deadline(cpp_dut: int) -> None:
    """REQ-COM-002's timing budget must hold for the native DUT too."""
    with LabClient("127.0.0.1", cpp_dut, response_timeout=0.5) as client:
        response = client.request({"command": "status", "sequence": 1})
    assert response.payload["state"] == "READY"
    assert response.elapsed_seconds <= 0.250


@pytest.mark.requirement("REQ-CPP-001")
def test_cpp_dut_survives_malformed_input(cpp_dut: int) -> None:
    """A bad line must not take the connection, or the process, down."""
    with LabClient("127.0.0.1", cpp_dut, response_timeout=0.5) as client:
        assert (
            client.request({"command": "status", "sequence": 1}).payload["status"]
            == "ok"
        )
    assert json.loads(_ask_raw(cpp_dut, "{not json"))["error_code"] == "INVALID_JSON"
    with LabClient("127.0.0.1", cpp_dut, response_timeout=0.5) as client:
        assert (
            client.request({"command": "status", "sequence": 2}).payload["status"]
            == "ok"
        )


@pytest.mark.requirement("REQ-CPP-001")
def test_cpp_dut_handles_concurrent_clients(cpp_dut: int) -> None:
    """One thread per connection: several clients are served at once."""
    clients = [LabClient("127.0.0.1", cpp_dut, response_timeout=1.0) for _ in range(5)]
    try:
        for client in clients:
            client.connect()
        for index, client in enumerate(clients):
            response = client.request({"command": "status", "sequence": index})
            assert response.payload["sequence"] == index
    finally:
        for client in clients:
            client.close()


@pytest.mark.requirement("REQ-CPP-001")
def test_cpp_dut_frames_on_newlines_not_packets(cpp_dut: int) -> None:
    """Two requests in one write must produce two replies: TCP is a stream."""
    with socket.create_connection(("127.0.0.1", cpp_dut), timeout=5.0) as sock:
        sock.settimeout(5.0)
        sock.sendall(
            b'{"command": "status", "sequence": 1}\n'
            b'{"command": "status", "sequence": 2}\n'
        )
        reader = sock.makefile("rb")
        first = json.loads(reader.readline())
        second = json.loads(reader.readline())
    assert (first["sequence"], second["sequence"]) == (1, 2)


@pytest.mark.requirement("REQ-CPP-001")
def test_cpp_dut_writes_the_same_evidence_lines(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The native log must carry the lines the evidence checks look for."""
    binary = cpp_dut_path()
    if binary is None:
        pytest.skip("C++ DUT not built")
    port = reserve_local_port()
    log_path = tmp_path_factory.mktemp("cpp-log") / "dut.log"
    process = _start(
        [
            str(binary),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-file",
            str(log_path),
        ]
    )
    try:
        _wait_ready("127.0.0.1", port)
        with LabClient("127.0.0.1", port, response_timeout=0.5) as client:
            client.request({"command": "status", "sequence": 1})
        deadline = time.monotonic() + 5.0
        text = ""
        while time.monotonic() < deadline:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            if "response peer=" in text:
                break
            time.sleep(0.05)
    finally:
        _stop(process)

    assert f"dut_ready host=127.0.0.1 port={port}" in text
    assert "client_connected peer=" in text
    assert "request peer=" in text
    assert "response peer=" in text
    # Same field layout as the Python DUT: an evidence parser written for one
    # must work on the other.
    assert " level=INFO message=" in text
