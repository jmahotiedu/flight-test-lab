"""Nominal and timing tests for the DUT status command."""

from __future__ import annotations

import pytest

from testlab.client import LabClient


@pytest.mark.requirement("REQ-COM-001")
def test_status_request_returns_ready(lab_client: LabClient) -> None:
    response = lab_client.request({"command": "status", "sequence": 1})
    assert response.payload == {
        "status": "ok",
        "state": "READY",
        "sequence": 1,
    }


@pytest.mark.requirement("REQ-COM-002")
def test_status_response_meets_250_ms_deadline(lab_client: LabClient) -> None:
    response = lab_client.request({"command": "status", "sequence": 2})
    assert response.elapsed_seconds <= 0.250
