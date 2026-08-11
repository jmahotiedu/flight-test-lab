"""Curriculum integrity tests: schema, uniqueness, references, availability."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from learning.server.curriculum import (
    Curriculum,
    CurriculumError,
    load_curriculum,
)
from learning.server.validators import validator_names

LEARNING_ROOT = Path(__file__).resolve().parents[2] / "learning"


@pytest.fixture(scope="module")
def curriculum() -> Curriculum:
    return load_curriculum(LEARNING_ROOT, validator_names())


def test_curriculum_loads(curriculum: Curriculum) -> None:
    assert len(curriculum.days) == 14
    assert len(curriculum.lessons) >= 30
    assert curriculum.ordered_lesson_ids


def test_lesson_ids_unique(curriculum: Curriculum) -> None:
    ids = curriculum.ordered_lesson_ids
    assert len(ids) == len(set(ids))


def test_prerequisites_reference_real_lessons(curriculum: Curriculum) -> None:
    for lesson in curriculum.lessons.values():
        for prerequisite in lesson.prerequisites:
            assert prerequisite in curriculum.lessons, (
                f"{lesson.id} has unknown prerequisite {prerequisite}"
            )


def test_prerequisites_point_backwards(curriculum: Curriculum) -> None:
    position = {lid: i for i, lid in enumerate(curriculum.ordered_lesson_ids)}
    for lesson in curriculum.lessons.values():
        for prerequisite in lesson.prerequisites:
            assert position[prerequisite] < position[lesson.id], (
                f"{lesson.id} depends on a later lesson {prerequisite}"
            )


def test_verify_blocks_reference_registered_validators(curriculum: Curriculum) -> None:
    known = validator_names()
    for lesson in curriculum.lessons.values():
        for block in lesson.blocks:
            if block["type"] == "verify":
                assert block["validator"] in known


def test_unavailable_lessons_explain_themselves(curriculum: Curriculum) -> None:
    """Whatever this machine cannot offer must say why, in actionable terms."""
    for lesson in curriculum.lessons.values():
        if lesson.status != "unavailable":
            continue
        reason = lesson.unavailable_reason
        assert len(reason.strip()) > 40, f"{lesson.id} gives no usable reason"
        assert "install" in reason.lower() or "build" in reason.lower(), (
            f"{lesson.id} does not tell the learner how to unlock it: {reason!r}"
        )


def test_native_lessons_declare_their_requirements(curriculum: Curriculum) -> None:
    """Days 11-12 must gate on detection, not on a hard-coded status.

    A lesson that needs a compiler has to say so; otherwise it would render as
    available on a machine that cannot run its validators.
    """
    native = [
        lesson for lesson in curriculum.lessons.values() if lesson.day in (11, 12)
    ]
    assert native, "days 11-12 have no lessons"
    for lesson in native:
        assert "cpp-build" in lesson.requires, f"{lesson.id} omits the cpp-build gate"
    debugging = [lesson for lesson in native if lesson.day == 12]
    for lesson in debugging:
        assert "gdb" in lesson.requires, f"{lesson.id} needs gdb but does not say so"


def test_lessons_without_requirements_are_always_available(
    curriculum: Curriculum,
) -> None:
    """Pure-Python lessons must never be gated by a native toolchain."""
    for lesson in curriculum.lessons.values():
        if not lesson.requires:
            assert lesson.status == "available", (
                f"{lesson.id} is unavailable but declares no requirements"
            )


def test_available_lessons_have_structure(curriculum: Curriculum) -> None:
    for lesson in curriculum.lessons.values():
        if lesson.status != "available":
            continue
        types = [block["type"] for block in lesson.blocks]
        assert "learn" in types, f"{lesson.id} has no learn block"
        assert "do" in types, f"{lesson.id} has no do block"
        assert types.count("explain") >= 1, f"{lesson.id} has no explain block"
        assert lesson.concepts, f"{lesson.id} teaches no concept"
        assert lesson.hints or lesson.day in (1,), f"{lesson.id} has no hints"


def test_every_lesson_gates_on_real_work(curriculum: Curriculum) -> None:
    """Each available lesson must require at least one verification artifact:
    a mandatory validator, a quiz, or an explain answer."""
    for lesson in curriculum.lessons.values():
        if lesson.status != "available":
            continue
        gates = (
            len(lesson.mandatory_validators())
            + len(lesson.required_quiz_ids())
            + len(lesson.required_explain_ids())
        )
        assert gates > 0, f"{lesson.id} can be completed by clicking through"


def test_no_lesson_authors_a_timeout_the_runner_would_shorten(
    curriculum: Curriculum,
) -> None:
    """A clamped timeout is a lie: the lesson allows time the runner refuses.

    A build or full parity run on a slow machine would then be killed early
    and the mandatory check becomes unpassable through no fault of the
    learner's.
    """
    from learning.server.validators import MAX_TIMEOUT_SECONDS

    for lesson in curriculum.lessons.values():
        for block in lesson.blocks:
            if block["type"] != "verify":
                continue
            authored = block.get("args", {}).get("timeout_seconds")
            if authored is None:
                continue
            assert float(authored) <= MAX_TIMEOUT_SECONDS, (
                f"{lesson.id}:{block['id']} asks for {authored}s but the runner "
                f"caps at {MAX_TIMEOUT_SECONDS}s"
            )


def test_interview_questions_cover_core_concepts(curriculum: Curriculum) -> None:
    assert len(curriculum.interview) >= 20
    covered = {concept for q in curriculum.interview for concept in q.concepts}
    for required in ("python-runtime", "timing", "process-management", "pytest"):
        assert required in covered


@pytest.mark.parametrize(
    ("prerequisites", "expected"),
    [
        (["l2"], "comes later in the program"),
        (["l1"], "lists itself as a prerequisite"),
        (["nope"], "unknown prerequisite"),
    ],
)
def test_unreachable_prerequisites_fail_at_load(
    tmp_path: Path, prerequisites: list[str], expected: str
) -> None:
    """The loader, not just the test suite, must reject a dead-end chain.

    A forward or self-referencing prerequisite leaves the lesson locked
    forever; someone launching the server without running pytest deserves the
    same loud failure.
    """
    root = tmp_path / "learning"
    (root / "curriculum" / "modules").mkdir(parents=True)
    (root / "curriculum" / "curriculum.json").write_text(
        '{"concepts": ["pytest"], "days":'
        ' [{"day": 1, "title": "t", "module": "day01.json"}]}',
        encoding="utf-8",
    )
    lessons = [
        {
            "id": "l1",
            "title": "one",
            "objective": "o",
            "estimated_minutes": 5,
            "prerequisites": prerequisites,
            "blocks": [{"type": "learn", "id": "b", "text": "x"}],
        },
        {
            "id": "l2",
            "title": "two",
            "objective": "o",
            "estimated_minutes": 5,
            "blocks": [{"type": "learn", "id": "b", "text": "x"}],
        },
    ]
    (root / "curriculum" / "modules" / "day01.json").write_text(
        json.dumps({"lessons": lessons}), encoding="utf-8"
    )
    (root / "curriculum" / "interview.json").write_text(
        '{"questions": []}', encoding="utf-8"
    )
    with pytest.raises(CurriculumError, match=expected):
        load_curriculum(root, validator_names())


def test_malformed_curriculum_fails_loudly(tmp_path: Path) -> None:
    bad_root = tmp_path / "learning"
    (bad_root / "curriculum" / "modules").mkdir(parents=True)
    (bad_root / "curriculum" / "curriculum.json").write_text(
        '{"concepts": ["x"], "days":'
        ' [{"day": 1, "title": "t", "module": "day01.json"}]}',
        encoding="utf-8",
    )
    (bad_root / "curriculum" / "modules" / "day01.json").write_text(
        '{"lessons": [{"id": "l1", "title": "t", "objective": "o",'
        ' "estimated_minutes": 5, "blocks": [{"type": "verify", "id": "v",'
        ' "validator": "does_not_exist"}]}]}',
        encoding="utf-8",
    )
    (bad_root / "curriculum" / "interview.json").write_text(
        '{"questions": []}', encoding="utf-8"
    )
    with pytest.raises(CurriculumError, match="unknown validator"):
        load_curriculum(bad_root, validator_names())
