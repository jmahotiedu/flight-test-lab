"""Run pytest against chosen node IDs and check results plus artifacts.

Evidence is redirected into a temporary directory (``EVIDENCE_DIR``) so
validator runs never clobber the learner's real ``evidence/`` tree.  A green
exit code alone is not proof: optional JUnit/stdout expectations confirm the
expected tests actually ran.
"""

from __future__ import annotations

import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from learning.checks.common import run_subprocess
from learning.server.validators import (
    CheckResult,
    ValidatorContext,
    clamp_timeout,
    truncate,
)


def _junit_test_names(junit_path: Path) -> tuple[set[str], set[str], set[str]]:
    """Return (all testcase names, skipped names, assertion-failed names).

    The three are different evidence.  A run where every test skipped still
    exits 0 with the cases present in the report, so treating a name as a pass
    would certify work that never executed.  And a test that *errors* during
    collection or setup also exits nonzero while proving nothing about the
    behaviour — so a "fails first" stage has to distinguish <failure> from
    <error>.
    """
    if not junit_path.exists():
        return set(), set(), set()
    try:
        tree = ET.parse(junit_path)
    except ET.ParseError:
        return set(), set(), set()
    names: set[str] = set()
    skipped: set[str] = set()
    failed: set[str] = set()
    for testcase in tree.getroot().iter("testcase"):
        name = testcase.get("name")
        if not name:
            continue
        names.add(name)
        if testcase.find("skipped") is not None:
            skipped.add(name)
        if testcase.find("failure") is not None:
            failed.add(name)
    return names, skipped, failed


def run(args: dict[str, Any], context: ValidatorContext) -> CheckResult:
    nodeids = args.get("nodeids")
    if (
        not isinstance(nodeids, list)
        or not nodeids
        or not all(isinstance(item, str) for item in nodeids)
    ):
        raise ValueError("pytest_check requires a non-empty 'nodeids' list")
    expect = args.get("expect", "pass")
    if expect not in ("pass", "fail"):
        raise ValueError("pytest_check 'expect' must be 'pass' or 'fail'")
    junit_contains = args.get("junit_contains", [])
    if not isinstance(junit_contains, list):
        raise ValueError("pytest_check 'junit_contains' must be a list")
    # A list means "every one of these must appear". Some evidence spans
    # several lines of pytest output — an assertion's introspection is on one
    # line and the value that differs on another, and pytest truncates the
    # first when the compared objects are large — so one regex cannot always
    # express "this failed as an assertion, about this value".
    stdout_patterns = args.get("expect_stdout_regex")
    if isinstance(stdout_patterns, str):
        stdout_patterns = [stdout_patterns]
    elif stdout_patterns is None:
        stdout_patterns = []
    elif not isinstance(stdout_patterns, list) or not all(
        isinstance(item, str) for item in stdout_patterns
    ):
        raise ValueError(
            "pytest_check 'expect_stdout_regex' must be a string or list of strings"
        )
    require_failure_not_error = bool(args.get("require_assertion_failure", False))
    timeout = clamp_timeout(args)
    extra_pytest_args = args.get("pytest_args", [])
    if not isinstance(extra_pytest_args, list) or not all(
        isinstance(item, str) for item in extra_pytest_args
    ):
        raise ValueError("pytest_check 'pytest_args' must be a list of strings")

    with tempfile.TemporaryDirectory(prefix="ftl-pytest-") as sandbox:
        sandbox_path = Path(sandbox)
        junit_path = sandbox_path / "junit" / "results.xml"
        (sandbox_path / "logs").mkdir(parents=True, exist_ok=True)
        junit_path.parent.mkdir(parents=True, exist_ok=True)

        argv = [
            context.python,
            "-m",
            "pytest",
            "-v",
            f"--junitxml={junit_path}",
            # Isolate the inner run's temp tree and cache from any outer
            # pytest session — on Windows, sharing basetemp makes the outer
            # session's cleanup fail with PermissionError.
            f"--basetemp={sandbox_path / 'basetemp'}",
            "-p",
            "no:cacheprovider",
            *extra_pytest_args,
            *nodeids,
        ]
        exit_status, stdout, stderr, duration_ms, timed_out = run_subprocess(
            argv,
            timeout=timeout,
            cwd=context.repo_root,
            extra_env={"EVIDENCE_DIR": str(sandbox_path)},
        )

        failures: list[str] = []
        if timed_out:
            failures.append(f"pytest did not finish within {timeout:.0f}s")
        elif expect == "pass" and exit_status != 0:
            failures.append(f"expected tests to pass (exit 0), got exit {exit_status}")
        elif expect == "fail" and exit_status == 0:
            failures.append("expected at least one test failure, but pytest exited 0")

        test_names, skipped_names, failed_names = _junit_test_names(junit_path)
        for expected_name in junit_contains:
            if not isinstance(expected_name, str):
                continue
            matching = [name for name in test_names if expected_name in name]
            if not matching:
                failures.append(
                    f"JUnit report does not contain a testcase matching "
                    f"{expected_name!r} (found: {sorted(test_names) or 'none'})"
                )
            elif all(name in skipped_names for name in matching):
                failures.append(
                    f"every test matching {expected_name!r} was skipped, so "
                    "nothing was actually verified"
                )
            elif require_failure_not_error and not any(
                name in failed_names for name in matching
            ):
                # A red stage has to be red for the *right* reason.  A fixture
                # typo or an import error also exits nonzero and still writes
                # the testcase into the report — as a <error>, not a <failure>
                # — which proves the test never observed the behaviour.
                failures.append(
                    f"{expected_name!r} did not fail as a test: it errored "
                    "during collection or setup, which proves nothing about "
                    "the behaviour under test"
                )
        for pattern in stdout_patterns:
            if not re.search(pattern, stdout):
                failures.append(f"pytest output did not match /{pattern}/")

    passed = not failures
    if passed:
        success_note = args.get("success_note")
        if isinstance(success_note, str) and success_note:
            interpretation = success_note
        elif expect == "pass":
            interpretation = "Tests passed and produced the expected evidence."
        else:
            interpretation = (
                "Tests failed as expected — the failure and its evidence were "
                "produced on purpose."
            )
    else:
        interpretation = "Check failed: " + "; ".join(failures)

    return CheckResult(
        name="pytest_check",
        passed=passed,
        exit_status=exit_status,
        stdout=truncate(stdout),
        stderr=truncate(stderr),
        duration_ms=duration_ms,
        interpretation=interpretation,
        timed_out=timed_out,
        details={
            "junit_testcases": sorted(test_names),
            "skipped": sorted(skipped_names),
            "failed": sorted(failed_names),
        },
    )
