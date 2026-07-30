"""Robustness tests for invalid protocol input."""

from __future__ import annotations

import json
import socket

import pytest

from conftest import RunningDut
from testlab.client import LabClient


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


@pytest.mark.requirement("REQ-PROTO-001")
def test_missing_command_is_rejected(lab_client: LabClient) -> None:
    response = lab_client.request({"sequence": 6})
    assert response.payload == {
        "status": "error",
        "error_code": "MISSING_COMMAND",
        "sequence": 6,
    }
