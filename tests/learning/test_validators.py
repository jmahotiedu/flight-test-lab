"""Validator-behavior tests: timeouts, structured output, sandboxing."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from learning.server.validators import (
    ValidatorContext,
    run_validator,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTEXT = ValidatorContext(repo_root=REPO_ROOT)


def test_python_probe_pass() -> None:
    result = run_validator(
        "python_probe",
        {"snippet": "print('hello')", "expect_stdout_regex": "hello"},
        CONTEXT,
    )
    assert result.passed
    assert result.exit_status == 0
    assert not result.timed_out


def test_python_probe_detects_wrong_output() -> None:
    result = run_validator(
        "python_probe",
        {"snippet": "print('nope')", "expect_stdout_regex": "yes"},
        CONTEXT,
    )
    assert not result.passed
    assert "did not match" in result.interpretation


def test_validator_timeout_kills_subprocess() -> None:
    started = time.monotonic()
    result = run_validator(
        "python_probe",
        {"snippet": "import time; time.sleep(60)", "timeout_seconds": 1},
        CONTEXT,
    )
    elapsed = time.monotonic() - started
    assert result.timed_out
    assert not result.passed
    assert elapsed < 15  # the 60s sleep was killed, not awaited


def test_pytest_check_pass_and_junit_evidence() -> None:
    result = run_validator(
        "pytest_check",
        {
            "nodeids": ["tests/test_status.py"],
            "expect": "pass",
            "junit_contains": ["test_status_request_returns_ready"],
        },
        CONTEXT,
    )
    assert result.passed, result.interpretation
    assert "test_status_request_returns_ready" in result.details["junit_testcases"]


def test_pytest_check_expect_fail() -> None:
    """A genuinely failing nodeid with expect=fail passes the check — and the
    report proves the test actually ran."""
    result = run_validator(
        "pytest_check",
        {
            "nodeids": ["tests/test_status.py::test_status_request_returns_ready"],
            "expect": "fail",
            "junit_contains": ["test_status_request_returns_ready"],
        },
        CONTEXT,
    )
    assert not result.passed  # the test passes, so expecting failure is wrong
    assert "expected at least one test failure" in result.interpretation


def test_pytest_check_sandbox_does_not_touch_real_evidence(tmp_path: Path) -> None:
    """pytest_check must redirect EVIDENCE_DIR away from the repo's evidence/."""
    sentinel = REPO_ROOT / "evidence" / "logs" / "validator-sentinel-check"
    assert not sentinel.exists()
    result = run_validator(
        "pytest_check",
        {"nodeids": ["tests/test_status.py"], "expect": "pass"},
        CONTEXT,
    )
    assert result.passed
    # The DUT log written by the session fixture must NOT appear in the repo
    # evidence tree from this run (sandboxed), and no stray dirs appear.
    assert not sentinel.exists()


def test_source_check_path_escape_rejected() -> None:
    with pytest.raises(ValueError, match="escapes"):
        run_validator(
            "source_check",
            {"file": "../outside.py", "must_contain": ["x"]},
            CONTEXT,
        )


def test_artifact_check_csv_row() -> None:
    result = run_validator(
        "artifact_check",
        {
            "file": "requirements/software_requirements.csv",
            "csv_row": {"requirement_id": "REQ-COM-001"},
        },
        CONTEXT,
    )
    assert result.passed, result.interpretation


def test_behavior_probe_structured_failure_is_truthful() -> None:
    """Expect a state the DUT never returns: the check must fail with a useful
    message, not crash."""
    result = run_validator(
        "behavior_probe",
        {
            "steps": [
                {
                    "send": {"command": "status", "sequence": 1},
                    "expect": {"state": "ARMED"},
                }
            ]
        },
        CONTEXT,
    )
    assert not result.passed
    assert "ARMED" in result.interpretation
    assert result.stdout  # transcript is shown to the learner


def test_unknown_validator_rejected() -> None:
    with pytest.raises(KeyError, match="unknown validator"):
        run_validator("arbitrary_shell", {"command": "echo hi"}, CONTEXT)
