"""Tests for the DUT's configuration-driven fault injection modes."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import RunningDut, reserve_local_port
from testlab.client import LabClient, LabCommunicationError


def _start_dut(
    evidence_dir: Path, log_name: str, extra_args: list[str]
) -> Iterator[RunningDut]:
    host = "127.0.0.1"
    port = reserve_local_port()
    log_path = evidence_dir / "logs" / log_name
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "simulator.simulator",
            "--host",
            host,
            "--port",
            str(port),
            "--log-file",
            str(log_path),
            *extra_args,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    running = RunningDut(process=process, host=host, port=port, log_path=log_path)
    try:
        yield running
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)


@pytest.fixture()
def delayed_dut(evidence_dir: Path) -> Iterator[RunningDut]:
    yield from _start_dut(
        evidence_dir,
        "dut-fault-delayed.log",
        ["--fault", "delayed_response", "--fault-delay-ms", "350"],
    )


@pytest.fixture()
def dropped_dut(evidence_dir: Path) -> Iterator[RunningDut]:
    yield from _start_dut(
        evidence_dir, "dut-fault-dropped.log", ["--fault", "dropped_connection"]
    )


@pytest.fixture()
def malformed_dut(evidence_dir: Path) -> Iterator[RunningDut]:
    yield from _start_dut(
        evidence_dir, "dut-fault-malformed.log", ["--fault", "malformed_response"]
    )


@pytest.mark.requirement("REQ-FAULT-001")
def test_delayed_response_fault_engages_and_logs(delayed_dut: RunningDut) -> None:
    client = LabClient(delayed_dut.host, delayed_dut.port, response_timeout=3.0)
    client.wait_until_ready(deadline_seconds=5.0)
    response = client.request({"command": "status", "sequence": 1})
    client.close()

    assert response.payload["state"] == "READY"
    assert response.elapsed_seconds >= 0.3

    log_text = delayed_dut.log_path.read_text(encoding="utf-8")
    assert "fault_injected fault=delayed_response" in log_text


@pytest.mark.requirement("REQ-FAULT-001")
def test_dropped_connection_fault_closes_without_response(
    dropped_dut: RunningDut,
) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                (dropped_dut.host, dropped_dut.port), timeout=0.2
            ):
                break
        except OSError:
            time.sleep(0.05)
    else:
        pytest.fail("DUT did not accept connections in time")

    client = LabClient(dropped_dut.host, dropped_dut.port, response_timeout=1.0)
    client.connect()
    with pytest.raises(LabCommunicationError):
        client.request({"command": "status", "sequence": 1})
    client.close()

    log_text = dropped_dut.log_path.read_text(encoding="utf-8")
    assert "fault_injected fault=drop_connection" in log_text


@pytest.mark.requirement("REQ-FAULT-001")
def test_malformed_response_fault_sends_unparseable_bytes(
    malformed_dut: RunningDut,
) -> None:
    # Readiness polling via status requests cannot succeed here: every
    # response is deliberately unparseable. Wait for port acceptance instead.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(
                (malformed_dut.host, malformed_dut.port), timeout=0.2
            ):
                break
        except OSError:
            time.sleep(0.05)
    else:
        pytest.fail("DUT did not accept connections in time")

    client = LabClient(malformed_dut.host, malformed_dut.port, response_timeout=2.0)
    client.connect()
    with pytest.raises(LabCommunicationError, match="invalid JSON"):
        client.request({"command": "status", "sequence": 1})
    client.close()

    log_text = malformed_dut.log_path.read_text(encoding="utf-8")
    assert "fault_injected fault=malformed_response" in log_text


@pytest.mark.requirement("REQ-FAULT-001")
def test_fault_config_file_drives_injection(evidence_dir: Path) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = Path(temp_dir) / "fault.json"
        config_path.write_text(json.dumps({"drop_connection": True}), encoding="utf-8")
        host = "127.0.0.1"
        port = reserve_local_port()
        log_path = evidence_dir / "logs" / "dut-fault-config.log"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "simulator.simulator",
                "--host",
                host,
                "--port",
                str(port),
                "--log-file",
                str(log_path),
                "--fault-config",
                str(config_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5.0
            connected = False
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection((host, port), timeout=0.2):
                        connected = True
                        break
                except OSError:
                    time.sleep(0.05)
            assert connected, "DUT did not accept connections in time"

            client = LabClient(host, port, response_timeout=1.0)
            client.connect()
            with pytest.raises(LabCommunicationError):
                client.request({"command": "status", "sequence": 1})
            client.close()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)

    log_text = log_path.read_text(encoding="utf-8")
    assert "fault_injected fault=drop_connection" in log_text
