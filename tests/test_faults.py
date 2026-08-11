"""Tests for the DUT's configuration-driven fault injection modes."""

from __future__ import annotations

import contextlib
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
    # The DUT opens its log in append mode, so a line written by a previous
    # run would still satisfy these assertions. A regression that stopped
    # emitting fault_injected would then stay green on stale evidence — the
    # failure mode this whole suite exists to catch. Start each run empty.
    log_path.unlink(missing_ok=True)
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
        log_path.unlink(missing_ok=True)  # never assert on a previous run's line
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


@pytest.mark.requirement("REQ-FAULT-001")
@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"drop_connection": "false"}, "must be true or false"),
        ({"malformed_response": 0}, "must be true or false"),
        ({"response_delay_ms": "400"}, "must be an integer"),
        ({"response_delay_ms": True}, "must be an integer"),
        ({"startup_delay_ms": -5}, "must not be negative"),
        ({"exit_after_requests": 1.5}, "must be an integer"),
    ],
)
def test_fault_config_rejects_wrong_types(
    tmp_path: Path, config: dict[str, object], expected: str
) -> None:
    """A mistyped switch must fail loudly, not coerce.

    `bool("false")` is True, so a hand-written config that says "false" would
    silently enable the fault it meant to disable — and the resulting run
    would look like a product defect rather than a configuration error.
    """
    from simulator.simulator import load_fault_config

    path = tmp_path / "fault.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(SystemExit, match=expected):
        load_fault_config(path)


@pytest.mark.requirement("REQ-FAULT-001")
def test_fault_config_with_a_bad_encoding_is_a_cli_error(tmp_path: Path) -> None:
    """An unreadable config is a usage error, not a traceback.

    UnicodeDecodeError is a ValueError, so it is caught by neither the OSError
    nor the JSONDecodeError arm — a config saved in the wrong encoding used to
    end the run with a stack trace while every other malformed file got a
    one-line diagnostic.
    """
    from simulator.simulator import load_fault_config

    path = tmp_path / "fault.json"
    path.write_bytes(b'{"drop_connection": true, "note": "\xff\xfe"}')
    with pytest.raises(SystemExit, match="--fault-config: cannot read"):
        load_fault_config(path)


@pytest.mark.requirement("REQ-FAULT-001")
def test_valid_fault_config_still_loads(tmp_path: Path) -> None:
    from simulator.simulator import load_fault_config

    path = tmp_path / "fault.json"
    path.write_text(
        json.dumps(
            {
                "response_delay_ms": 400,
                "drop_connection": False,
                "malformed_response": True,
                "startup_delay_ms": 0,
                "exit_after_requests": None,
            }
        ),
        encoding="utf-8",
    )
    config = load_fault_config(path)
    assert config.response_delay_ms == 400
    assert config.drop_connection is False
    assert config.malformed_response is True
    assert config.exit_after_requests is None


@pytest.mark.requirement("REQ-FAULT-001")
def test_startup_delay_fault_delays_readiness(evidence_dir: Path) -> None:
    """The DUT must not accept connections until the injected delay elapses.

    This is the fault that mimics slow hardware coming up, and it is the one a
    readiness poll with too short a deadline reports as a dead device.
    """
    delay_ms = 900
    started = time.monotonic()
    dut_iter = _start_dut(
        evidence_dir,
        "dut-fault-startup.log",
        ["--fault", "startup_delay", "--fault-delay-ms", str(delay_ms)],
    )
    dut = next(dut_iter)
    try:
        # Deliberately *not* asserting that the port is closed right now: this
        # process can be descheduled for longer than the delay on a loaded
        # host, and a correctly-delayed DUT would then already be listening.
        # The measured time-to-ready below proves the delay without depending
        # on when this thread happens to run.
        client = LabClient(dut.host, dut.port)
        try:
            client.wait_until_ready(deadline_seconds=15.0)
        finally:
            client.close()
        elapsed = time.monotonic() - started
        assert elapsed >= delay_ms / 1000, (
            f"DUT became ready after {elapsed:.2f}s, before the "
            f"{delay_ms} ms startup delay had elapsed"
        )
    finally:
        with contextlib.suppress(StopIteration):
            next(dut_iter)

    log_text = dut.log_path.read_text(encoding="utf-8")
    assert f"fault_injected fault=startup_delay delay_ms={delay_ms}" in log_text


@pytest.mark.requirement("REQ-FAULT-001")
def test_process_termination_fault_exits_after_the_configured_requests(
    evidence_dir: Path,
) -> None:
    """The DUT must die mid-session, which is what a harness has to survive.

    A DUT that vanishes is a different failure from one that answers wrongly:
    the client sees a closed connection, and the evidence has to show the
    process exited rather than the test being flaky.
    """
    dut_iter = _start_dut(
        evidence_dir,
        "dut-fault-termination.log",
        ["--fault", "process_termination"],
    )
    dut = next(dut_iter)
    try:
        # Readiness cannot be probed with a status request here: the preset
        # exits on the *first* request, so the probe itself would kill the DUT.
        # Wait for the port to accept instead, exactly as the malformed-response
        # test does — a readiness check has to survive the fault it precedes.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((dut.host, dut.port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            pytest.fail("DUT did not accept connections in time")

        client = LabClient(dut.host, dut.port, response_timeout=2.0)
        client.connect()
        with pytest.raises(LabCommunicationError):
            client.request({"command": "status", "sequence": 1})
        client.close()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and dut.process.poll() is None:
            time.sleep(0.05)
        assert dut.process.poll() is not None, "DUT should have terminated itself"
        assert dut.process.returncode != 0, "termination fault must be a failure exit"
    finally:
        with contextlib.suppress(StopIteration):
            next(dut_iter)

    log_text = dut.log_path.read_text(encoding="utf-8")
    assert "fault_injected fault=process_termination" in log_text
