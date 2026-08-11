"""Validator-behavior tests: timeouts, structured output, sandboxing."""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
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
                "dut_config": {"host": "", "port": 9000},
                "timestamp": "2026-08-11T00:00:00+00:00",
            },
            "placeholder values",
        ),
        (
            {
                "commit": "abc1234",
                "python_version": "3.11",
                "platform": "win32",
                "dut_config": {"host": "127.0.0.1", "port": 9000},
                "timestamp": "2026-08-11",
            },
            "no time of day",
        ),
        (
            {
                "commit": "abc1234",
                "python_version": "3.11",
                "platform": "win32",
                "dut_config": {"host": "127.0.0.1", "port": 9000},
                "timestamp": "2026-08-11T04:12:33",
            },
            "no UTC offset",
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


def _manifest(commit: str) -> str:
    return json.dumps(
        {
            "commit": commit,
            "python_version": "3.11.9",
            "platform": "win32",
            "dut_config": {"host": "127.0.0.1", "port": 9000},
            "timestamp": "2026-08-11T04:12:33+00:00",
        }
    )


@pytest.fixture()
def manifest_in_repo() -> Iterator[Path]:
    """A scratch manifest inside the repo.

    The commit rule shells out to git in the repository root, and artifact
    paths must stay inside the repo, so this cannot live in tmp_path.
    """
    target = REPO_ROOT / "evidence" / "exercises" / "manifest-under-test.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        yield target
    finally:
        target.unlink(missing_ok=True)


def test_artifact_check_accepts_a_filled_in_manifest(manifest_in_repo: Path) -> None:
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    manifest_in_repo.write_text(_manifest(head), encoding="utf-8")
    result = run_validator(
        "artifact_check",
        {
            "file": str(manifest_in_repo.relative_to(REPO_ROOT).as_posix()),
            "json_fields": MANIFEST_SPEC,
        },
        CONTEXT,
    )
    assert result.passed, result.interpretation


def test_disabled_settings_are_not_placeholders(manifest_in_repo: Path) -> None:
    """A nominal run records drop_connection: false and startup_delay_ms: 0.

    Those are real settings, not an unfilled skeleton — and because False == 0
    in Python, a membership test against 0 would have rejected every disabled
    boolean a learner correctly wrote down.
    """
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    manifest_in_repo.write_text(
        json.dumps(
            {
                "commit": head,
                "python_version": "3.11.9",
                "platform": "win32",
                "dut_config": {
                    "host": "127.0.0.1",
                    "port": 9000,
                    "drop_connection": False,
                    "startup_delay_ms": 0,
                    "exit_after_requests": None,
                },
                "timestamp": "2026-08-11T04:12:33+00:00",
            }
        ),
        encoding="utf-8",
    )
    result = run_validator(
        "artifact_check",
        {
            "file": str(manifest_in_repo.relative_to(REPO_ROOT).as_posix()),
            "json_fields": MANIFEST_SPEC,
        },
        CONTEXT,
    )
    assert result.passed, result.interpretation


def test_manifest_commit_must_exist_in_the_repository(manifest_in_repo: Path) -> None:
    """A plausible-looking hash is not a repository state.

    "deadbeef" is valid hex and identifies nothing, so accepting it would
    certify a manifest that cannot reproduce the run it describes.
    """
    manifest_in_repo.write_text(
        _manifest("deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"), encoding="utf-8"
    )
    result = run_validator(
        "artifact_check",
        {
            "file": str(manifest_in_repo.relative_to(REPO_ROOT).as_posix()),
            "json_fields": MANIFEST_SPEC,
        },
        CONTEXT,
    )
    assert not result.passed
    assert "not a commit in this repository" in result.interpretation


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


@pytest.mark.parametrize("validator", ["artifact_check", "source_check"])
def test_an_unreadable_artifact_is_a_failed_check_not_a_crash(
    validator: str, tmp_path: Path
) -> None:
    """exists() is not is_file().

    A learner who creates the expected path as a directory made the check
    fail; letting the OSError escape takes the /api/validate request down
    without a response and leaves the page on "Running…" instead.
    """
    (tmp_path / "artifact.json").mkdir()
    args: dict[str, object] = {"file": "artifact.json"}
    args |= (
        {"contains": ["anything"]}
        if validator == "artifact_check"
        else {"must_contain": ["anything"]}
    )

    result = run_validator(validator, args, ValidatorContext(repo_root=tmp_path))

    assert not result.passed
    assert "is not a file" in result.interpretation


def _process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        listing = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        return str(pid) in listing
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_the_clean_lifecycle_probe_reaps_when_its_middle_raises() -> None:
    """A failing validator must not also leak the DUT it spawned.

    run_subprocess kills the process group on timeout, not on an ordinary
    nonzero exit, so the snippet is the only thing that knows the pid. This
    runs the *shipped* Day 6 snippet with readiness forced to raise — what a
    learner-broken LabClient does — and checks the DUT is gone anyway.
    """
    from learning.server.curriculum import load_curriculum
    from learning.server.validators import validator_names

    curriculum = load_curriculum(REPO_ROOT / "learning", validator_names())
    lesson = curriculum.lessons["d6-broken-cleanup-sandbox"]
    block = next(b for b in lesson.blocks if b["id"] == "v-clean")

    snippet = block["args"]["snippet"]
    assert "finally:" in snippet
    # Announce the pid, then break the same call a broken client would break.
    snippet = snippet.replace(
        "try:\n", "print('PID=%d' % p.pid, flush=True)\ntry:\n", 1
    )
    snippet = snippet.replace(
        "    c.wait_until_ready(deadline_seconds=5.0)",
        "    raise RuntimeError('readiness failed')",
        1,
    )
    assert "RuntimeError" in snippet

    result = run_validator(
        "python_probe",
        {"snippet": snippet, "expect_stdout_regex": "PID=", "timeout_seconds": 30},
        CONTEXT,
    )
    match = re.search(r"PID=(\d+)", result.stdout)
    assert match, result.stdout
    assert not result.passed, "the injected failure must be reported, not hidden"
    assert not _process_alive(int(match.group(1))), (
        "the DUT outlived the failing validator"
    )


RACY_LOCK = '''"""A check-then-write lock that mentions the right primitive."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


class BenchLock:
    def __init__(self, path: str, owner: str) -> None:
        self.path = Path(path)
        self.owner = owner
        self._held = False

    def acquire(self) -> bool:
        # TODO: ought to use os.O_EXCL here; open(p, "x") would work too
        if self.path.exists():
            return False
        self.path.write_text(
            json.dumps(
                {"owner": self.owner, "timestamp": datetime.now(UTC).isoformat()}
            ),
            encoding="utf-8",
        )
        self._held = True
        return True

    def release(self) -> None:
        if self._held and self.path.exists():
            os.unlink(self.path)
            self._held = False

    def is_locked(self) -> bool:
        return self.path.exists()
'''

EXCLUSIVE_LOCK = RACY_LOCK.replace(
    """        # TODO: ought to use os.O_EXCL here; open(p, "x") would work too
        if self.path.exists():
            return False
        self.path.write_text(
            json.dumps(
                {"owner": self.owner, "timestamp": datetime.now(UTC).isoformat()}
            ),
            encoding="utf-8",
        )""",
    """        try:
            handle = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(
                {"owner": self.owner, "timestamp": datetime.now(UTC).isoformat()},
                stream,
            )""",
)

DAY13_SHAPE_GATE = {
    "must_contain": ["class BenchLock", "-> bool", "-> None"],
    "must_create_exclusively": "acquire",
}


@pytest.fixture()
def bench_module() -> Iterator[Path]:
    """A scratch module inside the repo (source paths may not escape it)."""
    target = REPO_ROOT / "src" / "testlab" / "bench_under_test.py"
    try:
        yield target
    finally:
        target.unlink(missing_ok=True)


def _shape_gate(bench_module: Path) -> dict[str, object]:
    return {
        "file": str(bench_module.relative_to(REPO_ROOT).as_posix()),
        **DAY13_SHAPE_GATE,
    }


@pytest.mark.parametrize(
    ("label", "source", "expected"),
    [("racy", RACY_LOCK, False), ("exclusive", EXCLUSIVE_LOCK, True)],
)
def test_exclusive_create_is_read_from_the_ast_not_the_text(
    bench_module: Path, label: str, source: str, expected: bool
) -> None:
    """O_EXCL in a comment is not an exclusive create.

    Both sources contain the identifiers a regex would look for, and both
    behave identically when probed sequentially — the race window is invisible
    without contention, which is the whole reason this property is checked in
    the parsed source rather than the file text.
    """
    bench_module.write_text(source, encoding="utf-8")
    result = run_validator("source_check", _shape_gate(bench_module), CONTEXT)
    assert result.passed is expected, result.interpretation
    if not expected:
        assert "exclusive-create" in result.interpretation


ACQUIRE_BODIES = {
    "open-x-mode": (
        "        try:\n"
        "            handle = open(self.path, 'x', encoding='utf-8')\n"
        "        except FileExistsError:\n"
        "            return False\n"
        "        handle.write('{}')\n"
        "        handle.close()\n"
        "        return True"
    ),
    "touch-exist-ok": (
        "        try:\n"
        "            self.path.touch(exist_ok=False)\n"
        "        except FileExistsError:\n"
        "            return False\n"
        "        return True"
    ),
    "mkdir": (
        "        try:\n"
        "            self.path.mkdir()\n"
        "        except FileExistsError:\n"
        "            return False\n"
        "        return True"
    ),
}


@pytest.mark.parametrize("idiom", sorted(ACQUIRE_BODIES))
def test_other_exclusive_create_idioms_are_accepted(
    bench_module: Path, idiom: str
) -> None:
    """The gate must not force one spelling of "create or fail"."""
    bench_module.write_text(
        "from __future__ import annotations\n"
        "from pathlib import Path\n\n\n"
        "class BenchLock:\n"
        "    def __init__(self, path: str, owner: str) -> None:\n"
        "        self.path = Path(path)\n"
        "        self._held = False\n\n"
        "    def acquire(self) -> bool:\n" + ACQUIRE_BODIES[idiom] + "\n\n"
        "    def release(self) -> None:\n"
        "        self._held = False\n",
        encoding="utf-8",
    )
    result = run_validator("source_check", _shape_gate(bench_module), CONTEXT)
    assert result.passed, result.interpretation


SEQUENCE_ECHO_TEST = """import pytest

{decorator}def test_sequence_echo_on_status_response(lab_client):
    response = lab_client.request({{"command": "status", "sequence": 42}})
    assert response["sequence"] == 42
"""


@pytest.fixture()
def sequence_echo_module() -> Iterator[Path]:
    target = REPO_ROOT / "tests" / "test_sequence_echo_under_test.py"
    try:
        yield target
    finally:
        target.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("label", "decorator", "expected"),
    [
        ("comment only", "# verifies REQ-LEARN-001\n", False),
        ("wrong id", '@pytest.mark.requirement("REQ-REC-001")\n', False),
        ("marked", '@pytest.mark.requirement("REQ-LEARN-001")\n', True),
    ],
)
def test_the_traceability_lesson_requires_a_real_marker(
    sequence_echo_module: Path, label: str, decorator: str, expected: bool
) -> None:
    """Day 7's chain is only as good as its load-bearing link.

    --strict-markers rejects an unknown marker but never requires one to
    exist, so a learner could supply the requirement row, the traceability row
    and a passing test and still ship a test that declares nothing.
    """
    sequence_echo_module.write_text(
        SEQUENCE_ECHO_TEST.format(decorator=decorator), encoding="utf-8"
    )
    result = run_validator(
        "source_check",
        {
            "file": str(sequence_echo_module.relative_to(REPO_ROOT).as_posix()),
            "must_have_requirement_marker": "REQ-LEARN-001",
        },
        CONTEXT,
    )
    assert result.passed is expected, result.interpretation


DUT_CONFIG_SPEC = {"dut_config": {"object": {"host": "string", "port": "port"}}}


@pytest.mark.parametrize(
    ("label", "config", "expected"),
    [
        # Every one of these is non-empty with no blank strings — and records
        # nothing a run could be reproduced from.
        ("placeholder key", {"placeholder": None}, "is missing 'host'"),
        ("null host", {"host": None, "port": 9000}, "'dut_config.host' is empty"),
        ("skeleton port", {"host": "127.0.0.1", "port": 0}, "not a usable port"),
        ("string port", {"host": "127.0.0.1", "port": "9000"}, "must be a port number"),
        ("missing port", {"host": "127.0.0.1"}, "is missing 'port'"),
        ("blank host", {"host": "  ", "port": 9000}, "empty placeholder values"),
    ],
)
def test_dut_config_keys_are_validated_individually(
    tmp_path: Path, label: str, config: dict[str, object], expected: str
) -> None:
    """A non-empty object is not a configuration.

    The port case matters most: the taught skeleton's placeholder is the
    number 0, which looks filled in to any is-it-empty test and names no
    listener.
    """
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"dut_config": config}), encoding="utf-8")
    result = run_validator(
        "artifact_check",
        {"file": str(path), "json_fields": DUT_CONFIG_SPEC},
        ValidatorContext(repo_root=tmp_path),
    )
    assert not result.passed
    assert expected in result.interpretation


def test_a_real_dut_config_still_passes(tmp_path: Path) -> None:
    """Unnamed keys stay optional and may be null.

    exit_after_requests: null is a real setting meaning "no such fault", not
    an unfilled blank — rejecting it would fail a learner who wrote down the
    actual configuration.
    """
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "dut_config": {
                    "host": "127.0.0.1",
                    "port": 9000,
                    "log_file": "evidence/logs/dut.log",
                    "exit_after_requests": None,
                    "drop_connection": False,
                    "startup_delay_ms": 0,
                }
            }
        ),
        encoding="utf-8",
    )
    result = run_validator(
        "artifact_check",
        {"file": str(path), "json_fields": DUT_CONFIG_SPEC},
        ValidatorContext(repo_root=tmp_path),
    )
    assert result.passed, result.interpretation
