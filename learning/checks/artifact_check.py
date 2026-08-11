"""Check evidence and requirements artifacts.

Validates repo-relative artifacts such as the requirements CSVs: existence,
regex content, CSV rows, and JUnit testcase entries.  Paths are verified to
stay inside the repository.
"""

from __future__ import annotations

import csv
import re
import xml.etree.ElementTree as ET
from io import StringIO
from typing import Any

from learning.checks.source_check import _resolve_repo_path
from learning.server.validators import CheckResult, ValidatorContext, truncate


def run(args: dict[str, Any], context: ValidatorContext) -> CheckResult:
    relative = args.get("file")
    if not isinstance(relative, str) or not relative:
        raise ValueError("artifact_check requires a repo-relative 'file'")
    target = _resolve_repo_path(context.repo_root, relative)

    failures: list[str] = []
    text = ""
    if not target.exists():
        failures.append(f"{relative} does not exist")
    else:
        text = target.read_text(encoding="utf-8", errors="replace")

    for pattern in args.get("contains", []):
        if not isinstance(pattern, str):
            continue
        if not re.search(pattern, text, re.MULTILINE):
            failures.append(f"{relative} does not contain a match for /{pattern}/")

    csv_row = args.get("csv_row")
    if isinstance(csv_row, dict) and text:
        rows = list(csv.DictReader(StringIO(text)))
        matched = any(
            all(
                str(row.get(column, "")).strip() == str(value)
                for column, value in csv_row.items()
            )
            for row in rows
        )
        if not matched:
            preview = ", ".join(sorted({key for row in rows for key in row}))
            failures.append(
                f"{relative} has no row matching {csv_row} "
                f"(columns present: {preview or 'none'})"
            )

    junit_testcase = args.get("junit_testcase")
    if isinstance(junit_testcase, str) and text:
        try:
            tree = ET.parse(target)
        except ET.ParseError:
            failures.append(f"{relative} is not parseable XML")
        else:
            names = {
                testcase.get("name") for testcase in tree.getroot().iter("testcase")
            }
            if not any(junit_testcase in (name or "") for name in names):
                failures.append(
                    f"{relative} has no testcase matching {junit_testcase!r}"
                )

    passed = not failures
    if passed:
        interpretation = str(
            args.get("success_note", f"{relative} contains the expected evidence.")
        )
    else:
        interpretation = "Check failed: " + "; ".join(failures)

    return CheckResult(
        name="artifact_check",
        passed=passed,
        exit_status=0 if passed else 1,
        stdout=truncate(text[:1000]) if text else "",
        stderr="",
        duration_ms=0,
        interpretation=interpretation,
    )
