"""Checks for process and evidence state visible during the test session."""

from __future__ import annotations

import pytest

from conftest import RunningDut


@pytest.mark.requirement("REQ-REC-001")
def test_dut_process_is_running_during_session(dut: RunningDut) -> None:
    assert dut.process.poll() is None
    assert dut.log_path.parent.exists()
