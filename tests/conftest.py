"""Shared pytest fixtures for launching and controlling the synthetic DUT."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from testlab.client import LabClient


@dataclass(frozen=True, slots=True)
class RunningDut:
    process: subprocess.Popen[str]
    host: str
    port: int
    log_path: Path


def reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


@pytest.fixture(scope="session")
def evidence_dir() -> Path:
    root = Path(os.environ.get("EVIDENCE_DIR", "evidence")).resolve()
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "junit").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture(scope="session")
def dut(evidence_dir: Path) -> Iterator[RunningDut]:
    host = "127.0.0.1"
    port = reserve_local_port()
    log_path = evidence_dir / "logs" / "dut.log"

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
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    running_dut = RunningDut(process=process, host=host, port=port, log_path=log_path)

    readiness_client = LabClient(host, port)
    try:
        readiness_client.wait_until_ready(deadline_seconds=5.0)
    except Exception:
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)
        raise
    finally:
        readiness_client.close()

    try:
        yield running_dut
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5.0)

        if process.poll() is None:
            pytest.fail("DUT process remained alive after forced cleanup")


@pytest.fixture()
def lab_client(dut: RunningDut) -> Iterator[LabClient]:
    if dut.process.poll() is not None:
        pytest.fail(f"DUT exited before test start with code {dut.process.returncode}")

    client = LabClient(dut.host, dut.port, response_timeout=0.5)
    client.connect()
    try:
        yield client
    finally:
        client.close()
