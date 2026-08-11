"""Verify recorded learner activity that leaves no artifact on disk.

Almost every check in this system inspects the repository, because almost
every exercise changes it.  Interview mode does not: answering questions
updates progress state and nothing else.  Without a check that can read that
state, a lesson whose objective is "answer at least five questions" can only
*ask*, and the completion gate would certify the objective on the strength of
a quiz answer and a sentence containing one keyword.

Read-only by construction: this validator loads the progress file and counts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from learning.server.validators import CheckResult, ValidatorContext


def _load_state(repo_root: Path) -> dict[str, Any]:
    path = repo_root / "learning" / ".progress.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def run(args: dict[str, Any], context: ValidatorContext) -> CheckResult:
    minimum = int(args.get("min_interview_answers", 1))
    state = _load_state(context.repo_root)
    interview = state.get("interview")
    records = interview.values() if isinstance(interview, dict) else []

    answered = 0
    distinct = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        correct = record.get("correct", 0)
        incorrect = record.get("incorrect", 0)
        total = (correct if isinstance(correct, int) else 0) + (
            incorrect if isinstance(incorrect, int) else 0
        )
        if total:
            distinct += 1
            answered += total

    passed = answered >= minimum
    if passed:
        interpretation = str(
            args.get(
                "success_note",
                f"{answered} interview answers recorded across {distinct} "
                "questions — the drill actually happened.",
            )
        )
    else:
        interpretation = (
            f"Check failed: {answered} interview answer(s) recorded, "
            f"{minimum} required. Open Interview mode and submit answers — "
            "this lesson's objective is the drill itself, so it cannot be "
            "completed by answering the quiz here."
        )

    return CheckResult(
        name="progress_check",
        passed=passed,
        exit_status=0 if passed else 1,
        stdout=f"interview answers: {answered} across {distinct} questions",
        stderr="",
        duration_ms=0,
        interpretation=interpretation,
        details={"answers": answered, "distinct_questions": distinct},
    )
