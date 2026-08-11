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


REPO_ROOT = Path(__file__).resolve().parents[1]


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--dut",
        action="store",
        default=os.environ.get("FTL_DUT", "python"),
        choices=("python", "cpp"),
        help=(
            "Which device-under-test implementation to exercise. Both speak the "
            "same protocol; 'cpp' requires cpp/build to be built first."
        ),
    )


def cpp_dut_path() -> Path | None:
    """The built C++ DUT, or None when it has not been built on this machine."""
    for name in ("dut.exe", "dut"):
        candidate = REPO_ROOT / "cpp" / "build" / "bin" / name
        if candidate.is_file():
            return candidate
    return None


def dut_command(implementation: str, host: str, port: int, log_path: Path) -> list[str]:
    """argv for either DUT — the only place the two implementations differ."""
    if implementation == "cpp":
        binary = cpp_dut_path()
        if binary is None:
            pytest.skip(
                "the C++ DUT is not built (cmake -S cpp -B cpp/build && "
                "cmake --build cpp/build)"
            )
        return [
            str(binary),
            "--host",
            host,
            "--port",
            str(port),
            "--log-file",
            str(log_path),
        ]
    return [
        sys.executable,
        "-m",
        "simulator.simulator",
        "--host",
        host,
        "--port",
        str(port),
        "--log-file",
        str(log_path),
    ]


@pytest.fixture(scope="session")
def evidence_dir() -> Path:
    root = Path(os.environ.get("EVIDENCE_DIR", "evidence")).resolve()
    (root / "logs").mkdir(parents=True, exist_ok=True)
    (root / "junit").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture(scope="session")
def dut_implementation(request: pytest.FixtureRequest) -> str:
    """'python' or 'cpp' — chosen by --dut / FTL_DUT, defaulting to python."""
    return str(request.config.getoption("--dut"))


@pytest.fixture(scope="session")
def dut(evidence_dir: Path, dut_implementation: str) -> Iterator[RunningDut]:
    host = "127.0.0.1"
    port = reserve_local_port()
    log_path = evidence_dir / "logs" / "dut.log"

    process = subprocess.Popen(
        dut_command(dut_implementation, host, port, log_path),
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
