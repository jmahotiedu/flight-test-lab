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
    # Same widening as ProgressStore._load: an undecodable progress file is
    # an absent one here, never a crash inside a check.
    except (OSError, ValueError, RecursionError):
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

    # Interview mode is available from Day 1, so a lifetime total says nothing
    # about whether *this* drill happened: a learner who had answered five
    # questions earlier in the course passed the moment they opened the lesson.
    # mark_started snapshots the count on first entry; measure from there.
    since_lesson = args.get("since_lesson")
    baseline: int | None = 0
    if isinstance(since_lesson, str):
        lessons = state.get("lessons")
        record = lessons.get(since_lesson) if isinstance(lessons, dict) else None
        stored = record.get("interview_baseline") if isinstance(record, dict) else None
        # No baseline means the lesson was never opened — which can only happen
        # by calling the API directly. "Answers since a moment that never
        # happened" is not a number, and defaulting it to zero would hand back
        # the lifetime total this argument exists to stop counting.
        baseline = stored if isinstance(stored, int) else None
    during = answered - (baseline or 0)

    passed = baseline is not None and during >= minimum
    if baseline is None:
        return CheckResult(
            name="progress_check",
            passed=False,
            exit_status=1,
            stdout=f"interview answers: {answered} total",
            stderr="",
            duration_ms=0,
            interpretation=(
                f"Check failed: {since_lesson} has not been started, so there "
                "is no point to measure the drill from. Open the lesson, then "
                "do the drill."
            ),
            details={"answers": answered, "distinct_questions": distinct},
        )
    if passed:
        interpretation = str(
            args.get(
                "success_note",
                f"{during} interview answers recorded across {distinct} "
                "questions — the drill actually happened.",
            )
        )
    else:
        interpretation = (
            f"Check failed: {during} interview answer(s) since you opened this "
            f"lesson, {minimum} required (answers before it do not count). "
            "Open Interview mode and submit answers — this lesson's objective "
            "is the drill itself, so it cannot be completed by answering the "
            "quiz here."
        )

    return CheckResult(
        name="progress_check",
        passed=passed,
        exit_status=0 if passed else 1,
        stdout=(
            f"interview answers: {answered} total across {distinct} questions, "
            f"{during} since this lesson began"
        ),
        stderr="",
        duration_ms=0,
        interpretation=interpretation,
        details={
            "answers": answered,
            "answers_during_lesson": during,
            "distinct_questions": distinct,
        },
    )
