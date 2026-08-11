"""Validator-behavior tests: timeouts, structured output, sandboxing."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest

from learning.checks.common import reserve_port
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


def test_validator_timeout_kills_the_whole_process_tree() -> None:
    """A timeout must reap grandchildren too, not just the direct child.

    Validators routinely start processes that start processes — pytest_check
    launches pytest which launches a DUT.  Killing only the immediate child
    leaves that DUT running and holding its port after the UI has already
    reported a timeout, which is exactly the orphan the course teaches you to
    avoid.  The grandchild here holds a port; the port becoming bindable again
    is the proof that it died.
    """
    port = reserve_port()
    grandchild = (
        "import socket, time; "
        "s = socket.socket(); "
        "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0); "
        f"s.bind(('127.0.0.1', {port})); s.listen(1); time.sleep(120)"
    )
    snippet = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        "time.sleep(120)\n"
    )

    result = run_validator(
        "python_probe", {"snippet": snippet, "timeout_seconds": 2}, CONTEXT
    )
    assert result.timed_out

    deadline = time.monotonic() + 20
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", port))
            return  # the grandchild released the port, so it is gone
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)
        finally:
            probe.close()
    pytest.fail(f"grandchild still holds port {port} after the timeout: {last_error}")


def test_skipped_tests_do_not_count_as_verified(tmp_path: Path) -> None:
    """A run where the expected test skipped must not pass the lesson check.

    pytest exits 0 when everything skips, and the skipped cases still appear
    in the JUnit report — so a naive check would certify work that never ran
    (the C++ parity tests on a machine with no cpp/build, for instance).
    """
    exercise = tmp_path / "test_skipper.py"
    exercise.write_text(
        "import pytest\n\n\n"
        "def test_needs_hardware():\n"
        "    pytest.skip('no hardware here')\n",
        encoding="utf-8",
    )
    result = run_validator(
        "pytest_check",
        {
            "nodeids": [str(exercise)],
            "expect": "pass",
            "junit_contains": ["test_needs_hardware"],
            "timeout_seconds": 60,
        },
        CONTEXT,
    )
    assert result.exit_status == 0, "pytest exits 0 when everything skips"
    assert not result.passed
    assert "skipped" in result.interpretation


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


MANIFEST_SPEC = {
    "commit": "commit",
    "python_version": r"^\d+\.\d+",
    "platform": "string",
    "dut_config": "object",
    "timestamp": "timestamp",
}


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        (
            {
                "commit": "",
                "python_version": "",
                "platform": "",
                "dut_config": {"host": "", "port": 0},
                "timestamp": "",
            },
            "is empty",
        ),
        (
            {
                "commit": "not-a-sha",
                "python_version": "3.11",
                "platform": "win32",
                "dut_config": {"host": "127.0.0.1", "port": 9000},
                "timestamp": "2026-08-11T00:00:00+00:00",
            },
            "not a git commit id",
        ),
        (
            {
                "commit": "abc1234",
                "python_version": "3.11",
                "platform": "win32",
                "dut_config": {"host": "127.0.0.1", "port": 9000},
                "timestamp": "yesterday",
            },
            "not an ISO-8601 timestamp",
        ),
        (
            {
                "commit": "abc1234",
                "python_version": "3.11",
                "platform": "win32",
                "dut_config": {"host": "127.0.0.1", "port": 0},
                "timestamp": "2026-08-11T00:00:00+00:00",
            },
            "placeholder values",
        ),
    ],
)
def test_artifact_check_reads_json_values_not_key_names(
    tmp_path: Path, manifest: dict[str, object], expected: str
) -> None:
    """A manifest of empty strings must not certify a reproducible run.

    Searching the file text for key names lets the untouched skeleton pass,
    which is the audit failure mode this whole lesson is about.
    """
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    result = run_validator(
        "artifact_check",
        {"file": str(path), "json_fields": MANIFEST_SPEC},
        ValidatorContext(repo_root=tmp_path),
    )
    assert not result.passed
    assert expected in result.interpretation


def test_artifact_check_accepts_a_filled_in_manifest(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "commit": "0123456789abcdef0123456789abcdef01234567",
                "python_version": "3.11.9",
                "platform": "win32",
                "dut_config": {"host": "127.0.0.1", "port": 9000},
                "timestamp": "2026-08-11T04:12:33+00:00",
            }
        ),
        encoding="utf-8",
    )
    result = run_validator(
        "artifact_check",
        {"file": str(path), "json_fields": MANIFEST_SPEC},
        ValidatorContext(repo_root=tmp_path),
    )
    assert result.passed, result.interpretation


def test_requirement_marker_must_be_a_real_decorator(tmp_path: Path) -> None:
    """The ID in a comment or docstring is not a requirement link.

    Matching raw text lets a lesson certify a test with no marker at all,
    which is exactly the dangling-reference problem the marker exists to
    prevent.
    """
    decoy = tmp_path / "test_decoy.py"
    decoy.write_text(
        '"""Covers REQ-PROTO-001 (allegedly)."""\n\n'
        "# REQ-PROTO-001 is handled below\n"
        "def test_thing():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    result = run_validator(
        "source_check",
        {
            "file": str(decoy),
            "must_have_requirement_marker": "REQ-PROTO-001",
        },
        ValidatorContext(repo_root=tmp_path),
    )
    assert not result.passed
    assert "no test function decorated" in result.interpretation

    real = tmp_path / "test_real.py"
    real.write_text(
        "import pytest\n\n\n"
        '@pytest.mark.requirement("REQ-PROTO-001")\n'
        "def test_thing():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    ok = run_validator(
        "source_check",
        {"file": str(real), "must_have_requirement_marker": "REQ-PROTO-001"},
        ValidatorContext(repo_root=tmp_path),
    )
    assert ok.passed, ok.interpretation


def test_requirement_marker_accepts_the_parametrize_idiom(tmp_path: Path) -> None:
    """Day 5 teaches marks= on individual pytest.param entries.

    This is the exact file from that lesson's full-solution hint. A check that
    only looks at function decorators rejects the solution the course tells
    the learner to write.
    """
    taught = tmp_path / "test_build_response.py"
    taught.write_text(
        '"""Pure-unit tests for build_response() error paths."""\n\n'
        "from __future__ import annotations\n\n"
        "import pytest\n\n"
        "from simulator.simulator import build_response\n\n\n"
        "@pytest.mark.parametrize(\n"
        "    ('message', 'expected_error'),\n"
        "    [\n"
        "        pytest.param(['status'], 'INVALID_MESSAGE_TYPE',\n"
        "                     marks=pytest.mark.requirement('REQ-PROTO-001'),\n"
        "                     id='list-input'),\n"
        "    ],\n"
        ")\n"
        "def test_build_response_error_cases(message, expected_error):\n"
        "    response = build_response(message)\n"
        "    assert response['error_code'] == expected_error\n",
        encoding="utf-8",
    )
    result = run_validator(
        "source_check",
        {
            "file": str(taught),
            "must_contain": ["parametrize"],
            "must_have_requirement_marker": "REQ-PROTO-001",
        },
        ValidatorContext(repo_root=tmp_path),
    )
    assert result.passed, result.interpretation


def test_junit_evidence_requires_the_expected_outcome(tmp_path: Path) -> None:
    """A recorded name is not a recorded result."""
    report = tmp_path / "results.xml"
    report.write_text(
        '<?xml version="1.0"?><testsuite name="s" tests="1">'
        '<testcase name="test_thing"><failure message="boom"/></testcase>'
        "</testsuite>",
        encoding="utf-8",
    )
    context = ValidatorContext(repo_root=tmp_path)

    as_pass = run_validator(
        "artifact_check",
        {"file": str(report), "junit_testcase": "test_thing"},
        context,
    )
    assert not as_pass.passed
    assert "expected passed" in as_pass.interpretation

    as_fail = run_validator(
        "artifact_check",
        {
            "file": str(report),
            "junit_testcase": "test_thing",
            "junit_outcome": "failed",
        },
        context,
    )
    assert as_fail.passed, as_fail.interpretation


def test_csv_row_matches_requires_one_row_to_satisfy_every_column(
    tmp_path: Path,
) -> None:
    """A requirement id in one row must not borrow evidence from another."""
    csv_path = tmp_path / "traceability.csv"
    csv_path.write_text(
        "requirement_id,test_case,evidence\n"
        "REQ-A,tests/test_a.py::test_a,evidence/junit/test-results.xml\n"
        "REQ-B,tests/test_b.py::test_b,evidence/logs/dut.log\n",
        encoding="utf-8",
    )
    context = ValidatorContext(repo_root=tmp_path)

    wrong_pairing = run_validator(
        "artifact_check",
        {
            "file": str(csv_path),
            "csv_row_matches": {
                "requirement_id": "^REQ-B$",
                "evidence": r"^evidence/junit/test-results\.xml$",
            },
        },
        context,
    )
    assert not wrong_pairing.passed

    correct = run_validator(
        "artifact_check",
        {
            "file": str(csv_path),
            "csv_row_matches": {
                "requirement_id": "^REQ-A$",
                "test_case": r"^tests/test_a\.py::test_a$",
                "evidence": r"^evidence/junit/test-results\.xml$",
            },
        },
        context,
    )
    assert correct.passed, correct.interpretation


def test_red_stage_rejects_a_collection_error(tmp_path: Path) -> None:
    """ "Fails first" has to mean the assertion ran, not that pytest exited 1.

    A fixture typo or bad import is also nonzero and still records the case in
    the report — as an <error>, which proves nothing about the behaviour.
    """
    broken = tmp_path / "test_broken.py"
    broken.write_text(
        "import pytest\n\n\n"
        "@pytest.fixture\n"
        "def thing():\n"
        "    raise RuntimeError('setup exploded')\n\n\n"
        "def test_ping(thing):\n"
        "    assert thing == 'pong'\n",
        encoding="utf-8",
    )
    result = run_validator(
        "pytest_check",
        {
            "nodeids": [str(broken)],
            "expect": "fail",
            "junit_contains": ["ping"],
            "require_assertion_failure": True,
            "timeout_seconds": 60,
        },
        CONTEXT,
    )
    assert not result.passed
    assert "errored during collection or setup" in result.interpretation


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
