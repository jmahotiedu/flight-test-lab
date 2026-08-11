"""Check evidence and requirements artifacts.

Validates repo-relative artifacts such as the requirements CSVs: existence,
regex content, CSV rows, and JUnit testcase entries.  Paths are verified to
stay inside the repository.
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import Any

from learning.checks.source_check import _resolve_repo_path
from learning.server.validators import CheckResult, ValidatorContext, truncate


def _junit_outcome(testcases: list[ET.Element]) -> set[str]:
    """Outcomes recorded for the matching testcases: passed/failed/error/skipped."""
    outcomes: set[str] = set()
    for testcase in testcases:
        if testcase.find("failure") is not None:
            outcomes.add("failed")
        elif testcase.find("error") is not None:
            outcomes.add("error")
        elif testcase.find("skipped") is not None:
            outcomes.add("skipped")
        else:
            outcomes.add("passed")
    return outcomes


def _describe(value: Any) -> str:
    rendered = repr(value)
    return rendered if len(rendered) <= 60 else rendered[:57] + "..."


def _commit_exists(repo_root: Path, commit: str) -> bool:
    """True when `commit` names a real commit in this checkout.

    Syntax is not identity: "deadbee" is seven hex characters and describes no
    repository state, so a manifest containing it would be certified as
    reproducible while pointing at nothing.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}"],
            cwd=str(repo_root),
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return True  # no git available: fall back to the syntax check alone
    return result.returncode == 0


def _check_json_fields(
    relative: str,
    document: dict[str, Any],
    spec: dict[str, Any],
    repo_root: Path,
) -> list[str]:
    """Require each named field to be present, correctly typed and filled in.

    Spec values are rules: "string" (non-empty), "object" (non-empty),
    "commit" (40 hex characters), "timestamp" (ISO-8601 parseable), or a regex
    string the value must match.
    """
    failures: list[str] = []
    for field, rule in spec.items():
        if field not in document:
            failures.append(f"{relative} is missing {field!r}")
            continue
        value = document[field]

        if rule == "object":
            if not isinstance(value, dict) or not value:
                failures.append(f"{relative}: {field!r} must be a non-empty object")
            # Only an empty string is a placeholder. `false`, `0` and `null`
            # are legitimate settings — a nominal run genuinely records
            # drop_connection: false and startup_delay_ms: 0 — and rejecting
            # them would fail a learner who wrote down the real configuration.
            # (Note `False == 0` in Python, so a membership test against 0
            # would have swept up every disabled boolean too.)
            elif any(isinstance(v, str) and not v.strip() for v in value.values()):
                failures.append(
                    f"{relative}: {field!r} still has empty placeholder values "
                    f"({_describe(value)}) — fill in the real configuration"
                )
            continue

        if not isinstance(value, str) or not value.strip():
            failures.append(
                f"{relative}: {field!r} is empty — the skeleton has to be "
                f"filled in with the real value, got {_describe(value)}"
            )
            continue

        if rule == "commit":
            if not re.fullmatch(r"[0-9a-fA-F]{7,40}", value.strip()):
                failures.append(
                    f"{relative}: {field!r} is not a git commit id "
                    f"({_describe(value)}); use the output of `git rev-parse HEAD`"
                )
            elif not _commit_exists(repo_root, value.strip()):
                failures.append(
                    f"{relative}: {field!r} ({_describe(value)}) is not a commit "
                    "in this repository — a manifest has to identify the state "
                    "that produced the run, not just look like a hash"
                )
        elif rule == "timestamp":
            # Parseability is not identity: fromisoformat also accepts a bare
            # date and a naive local datetime, neither of which names an
            # unambiguous instant. A run manifest compared across machines
            # needs a time *and* an offset.
            text = value.strip().replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                failures.append(
                    f"{relative}: {field!r} is not an ISO-8601 timestamp "
                    f"({_describe(value)})"
                )
            else:
                if "T" not in text and " " not in text:
                    failures.append(
                        f"{relative}: {field!r} is a date with no time of day "
                        f"({_describe(value)}) — a run happens at an instant"
                    )
                elif parsed.tzinfo is None:
                    failures.append(
                        f"{relative}: {field!r} has no UTC offset "
                        f"({_describe(value)}) — a local time means nothing on "
                        "another machine; use datetime.now(UTC).isoformat()"
                    )
        elif (
            isinstance(rule, str)
            and rule not in ("string", "commit", "timestamp")
            and not re.search(rule, value)
        ):
            failures.append(
                f"{relative}: {field!r} does not match /{rule}/ ({_describe(value)})"
            )
    return failures


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

    # csv_row_matches is csv_row's pattern-matching twin: each column must
    # match a regex *in the same row*, so a requirement id in one row cannot
    # be paired with an evidence path from another.
    csv_row_matches = args.get("csv_row_matches")
    if isinstance(csv_row_matches, dict) and text:
        rows = list(csv.DictReader(StringIO(text)))
        matched_row = any(
            all(
                re.search(str(pattern), str(row.get(column, "") or "").strip())
                for column, pattern in csv_row_matches.items()
            )
            for row in rows
        )
        if not matched_row:
            columns = ", ".join(sorted({key for row in rows for key in row}))
            failures.append(
                f"{relative} has no single row where every column matches "
                f"{csv_row_matches} (columns present: {columns or 'none'})"
            )

    # json_fields checks the *values*, not just that a key name appears
    # somewhere in the text.  Searching for key names lets an empty skeleton
    # pass, which would certify a manifest that cannot reproduce anything.
    json_fields = args.get("json_fields")
    if isinstance(json_fields, dict) and text:
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            failures.append(f"{relative} is not valid JSON ({exc})")
        else:
            if not isinstance(document, dict):
                failures.append(f"{relative} must contain a JSON object")
            else:
                failures.extend(
                    _check_json_fields(
                        relative, document, json_fields, context.repo_root
                    )
                )

    junit_testcase = args.get("junit_testcase")
    if isinstance(junit_testcase, str) and text:
        try:
            tree = ET.parse(target)
        except ET.ParseError:
            failures.append(f"{relative} is not parseable XML")
        else:
            matches = [
                testcase
                for testcase in tree.getroot().iter("testcase")
                if junit_testcase in (testcase.get("name") or "")
            ]
            if not matches:
                failures.append(
                    f"{relative} has no testcase matching {junit_testcase!r}"
                )
            else:
                # The presence of a name is not a result.  A lesson that
                # claims a requirement was verified has to require a *passing*
                # testcase; the controlled-failure lessons ask for "failed"
                # instead, which is equally explicit.
                expected = str(args.get("junit_outcome", "passed"))
                observed = _junit_outcome(matches)
                if expected != "any" and expected not in observed:
                    failures.append(
                        f"{relative}: testcase {junit_testcase!r} is "
                        f"{'/'.join(sorted(observed))}, expected {expected} — a "
                        "recorded name is not a recorded result"
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
