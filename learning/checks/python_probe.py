"""Run a curriculum-defined Python snippet and check its behavior.

The snippet comes from the curriculum file (repo content), never from the
browser.  Used for experiments like "import the simulator and observe that
the server does not start".
"""

from __future__ import annotations

import re
from typing import Any

from learning.checks.common import run_subprocess
from learning.server.validators import (
    CheckResult,
    ValidatorContext,
    clamp_timeout,
    truncate,
)


def run(args: dict[str, Any], context: ValidatorContext) -> CheckResult:
    snippet = args.get("snippet")
    if not isinstance(snippet, str) or not snippet.strip():
        raise ValueError("python_probe requires a non-empty 'snippet'")
    expected_exit = int(args.get("expect_exit", 0))
    stdout_pattern = args.get("expect_stdout_regex")
    stdout_absent = args.get("forbid_stdout_regex")
    timeout = clamp_timeout(args)

    exit_status, stdout, stderr, duration_ms, timed_out = run_subprocess(
        [context.python, "-c", snippet],
        timeout=timeout,
        cwd=context.repo_root,
    )

    failures: list[str] = []
    if timed_out:
        failures.append(f"snippet did not finish within {timeout:.0f}s")
    elif exit_status != expected_exit:
        failures.append(f"expected exit {expected_exit}, got {exit_status}")
    if isinstance(stdout_pattern, str) and not re.search(stdout_pattern, stdout):
        failures.append(f"stdout did not match /{stdout_pattern}/")
    if isinstance(stdout_absent, str) and re.search(stdout_absent, stdout):
        failures.append(f"stdout unexpectedly matched /{stdout_absent}/")

    passed = not failures
    if passed:
        interpretation = str(args.get("success_note", "Snippet behaved as expected."))
    else:
        interpretation = "Check failed: " + "; ".join(failures)

    return CheckResult(
        name="python_probe",
        passed=passed,
        exit_status=exit_status,
        stdout=truncate(stdout),
        stderr=truncate(stderr),
        duration_ms=duration_ms,
        interpretation=interpretation,
        timed_out=timed_out,
    )
