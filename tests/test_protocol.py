"""Robustness tests for invalid protocol input."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import RunningDut
from testlab.client import LabClient

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.requirement("REQ-PROTO-001")
def test_unknown_command_is_rejected_without_stopping_dut(
    lab_client: LabClient,
) -> None:
    rejected = lab_client.request({"command": "launch", "sequence": 3})
    assert rejected.payload["status"] == "error"
    assert rejected.payload["error_code"] == "UNSUPPORTED_COMMAND"
    assert rejected.payload["sequence"] == 3

    follow_up = lab_client.request({"command": "status", "sequence": 4})
    assert follow_up.payload["state"] == "READY"


@pytest.mark.requirement("REQ-PROTO-002")
def test_malformed_json_is_rejected_without_stopping_dut(dut: RunningDut) -> None:
    with socket.create_connection((dut.host, dut.port), timeout=1.0) as connection:
        connection.settimeout(1.0)
        connection.sendall(b'{"command": "status"\n')

        reader = connection.makefile("r", encoding="utf-8", newline="\n")
        try:
            response = json.loads(reader.readline())
            assert response == {
                "status": "error",
                "error_code": "INVALID_JSON",
            }

            connection.sendall(b'{"command": "status", "sequence": 5}\n')
            follow_up = json.loads(reader.readline())
            assert follow_up["state"] == "READY"
        finally:
            reader.close()


@pytest.mark.requirement("REQ-PROTO-002")
def test_an_oversized_integer_is_rejected_without_stopping_dut(
    dut: RunningDut,
) -> None:
    """A syntactically valid number can still be undecodable.

    CPython caps integer-string conversion (4300 digits by default) and raises
    ValueError, which is not a JSONDecodeError — so this request used to unwind
    the connection thread rather than being answered. The follow-up matters
    more than the rejection: a DUT that dies on one bad line takes the rest of
    the session with it.
    """
    oversized = ('{"command": "status", "sequence": ' + "1" * 5000 + "}").encode()
    with socket.create_connection((dut.host, dut.port), timeout=5.0) as connection:
        connection.settimeout(5.0)
        connection.sendall(oversized + b"\n")

        reader = connection.makefile("r", encoding="utf-8", newline="\n")
        try:
            assert json.loads(reader.readline()) == {
                "status": "error",
                "error_code": "INVALID_JSON",
            }

            connection.sendall(b'{"command": "status", "sequence": 7}\n')
            assert json.loads(reader.readline())["state"] == "READY"
        finally:
            reader.close()


@pytest.mark.requirement("REQ-PROTO-002")
def test_an_integer_at_the_digit_limit_is_still_echoed(dut: RunningDut) -> None:
    """The bound has to be a bound, not a ban on large numbers.

    Python echoes arbitrary-precision integers, and the parity suite depends on
    that; rejecting everything long would have been an easier fix and a wrong
    one.
    """
    digits = "1" * 4300
    request = ('{"command": "status", "sequence": ' + digits + "}").encode()
    with socket.create_connection((dut.host, dut.port), timeout=5.0) as connection:
        connection.settimeout(5.0)
        connection.sendall(request + b"\n")
        reader = connection.makefile("r", encoding="utf-8", newline="\n")
        try:
            response = json.loads(reader.readline())
        finally:
            reader.close()
    assert response["state"] == "READY"
    assert str(response["sequence"]) == digits


@pytest.mark.requirement("REQ-PROTO-001")
def test_missing_command_is_rejected(lab_client: LabClient) -> None:
    response = lab_client.request({"sequence": 6})
    assert response.payload == {
        "status": "error",
        "error_code": "MISSING_COMMAND",
        "sequence": 6,
    }


@pytest.mark.requirement("REQ-PROTO-002")
def test_the_digit_bound_does_not_depend_on_the_interpreter() -> None:
    """The protocol decides what a request means, not the environment.

    PYTHONINTMAXSTRDIGITS=1000 made this DUT reject a 1001-digit request the
    C++ DUT accepts — and, in the other direction, unable to serialise a
    number it had just parsed, since int-to-str is capped too. The module
    raises the interpreter's limit to the protocol's on import.
    """
    digits = "1" * 4300
    probe = (
        "import json, sys;"
        "from simulator.simulator import decode_request, build_response;"
        f'm, ok = decode_request(\'{{"command": "status", "sequence": {digits}}}\');'
        "print(ok, json.dumps(build_response(m))[:0] == '',"
        " str(build_response(m)['sequence']) == '" + digits + "')"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PYTHONINTMAXSTRDIGITS": "640"},
    )
    assert result.returncode == 0, result.stderr[-500:]
    assert result.stdout.split() == ["True", "True", "True"], result.stdout
