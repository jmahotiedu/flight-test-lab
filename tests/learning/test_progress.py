"""Progress-store tests: persistence, atomicity, corruption, completion gating."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from learning.server.curriculum import load_curriculum
from learning.server.progress import ProgressStore
from learning.server.validators import validator_names

LEARNING_ROOT = Path(__file__).resolve().parents[2] / "learning"


def _lesson(lesson_id: str = "d1-import-no-side-effects"):
    curriculum = load_curriculum(LEARNING_ROOT, validator_names())
    return curriculum.lessons[lesson_id]


def test_progress_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"
    store = ProgressStore(path)
    store.mark_started("d1-import-no-side-effects")
    store.record_validation("d1-import-no-side-effects", "verify", True)
    store.record_hint("d1-import-no-side-effects")

    reloaded = ProgressStore(path)
    snapshot = reloaded.snapshot()
    record = snapshot["lessons"]["d1-import-no-side-effects"]
    assert record["status"] == "in_progress"
    assert record["validations"]["verify"]["passed"] is True
    assert record["hints_used"] == 1
    assert snapshot["last_lesson_id"] == "d1-import-no-side-effects"


def test_corrupt_progress_is_backed_up_and_reset(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"
    path.write_text("{not json", encoding="utf-8")
    store = ProgressStore(path)
    assert store.snapshot()["lessons"] == {}
    assert (tmp_path / "progress.corrupt.json").exists()


@pytest.mark.parametrize(
    "payload",
    [
        '{"version": 1, "lessons": []}',
        '{"version": 1, "lessons": {"d1": "not-a-record"}}',
        '{"version": 1, "concepts": 7}',
        '{"version": 1, "interview": "nope"}',
        '{"version": 1, "last_lesson_id": 42}',
        '{"version": 99, "lessons": {}}',
    ],
)
def test_structurally_invalid_state_is_backed_up_and_reset(
    tmp_path: Path, payload: str
) -> None:
    """Parsing is not validation.

    Each of these files is valid JSON, so the old load path accepted them and
    the damage only surfaced later as an AttributeError inside a request
    handler. They must take the documented back-up-and-reset path instead.
    """
    path = tmp_path / "progress.json"
    path.write_text(payload, encoding="utf-8")

    store = ProgressStore(path)
    snapshot = store.snapshot()

    assert snapshot["lessons"] == {}
    assert snapshot["concepts"] == {}
    assert snapshot["interview"] == {}
    assert path.with_suffix(".corrupt.json").exists(), "damaged state was not preserved"


@pytest.mark.parametrize(
    "payload",
    [
        '{"version": 1, "concepts": {"timing": {"correct": "bad"}}}',
        '{"version": 1, "lessons": {"l1": {"attempts": "many"}}}',
        '{"version": 1, "lessons": {"l1": {"steps_done": "not-a-list"}}}',
        '{"version": 1, "lessons": {"l1": {"validations": {"v": "nope"}}}}',
        '{"version": 1, "interview": {"q1": {"correct": null, "incorrect": []}}}',
        # "false" is a non-empty string, so a truthiness test reads it as a
        # pass: a damaged file would satisfy a mandatory validator and certify
        # the lesson instead of taking the recovery path.
        '{"version": 1, "lessons": {"l": {"validations": {"v": {"passed": "false"}}}}}',
        '{"version": 1, "lessons": {"l1": {"validations": {"v": {"passed": 1}}}}}',
        '{"version": 1, "lessons": {"l1": {"validations": {"v": {"at": 17}}}}}',
    ],
)
def test_records_with_wrong_field_types_are_reset(tmp_path: Path, payload: str) -> None:
    """Right shape, wrong field types must still take the recovery path.

    These files pass an outer type check and then raise much later — inside a
    request handler, when the value is used as an int or a list — which is a
    broken dashboard rather than the documented back-up-and-reset.
    """
    path = tmp_path / "progress.json"
    path.write_text(payload, encoding="utf-8")

    store = ProgressStore(path)

    assert store.snapshot()["lessons"] == {}
    assert store.snapshot()["concepts"] == {}
    assert path.with_suffix(".corrupt.json").exists()


def test_a_failing_recheck_revokes_completion(tmp_path: Path) -> None:
    """Re-running a mandatory check that now fails must un-complete the lesson.

    lesson_completion() already reports the missing gate, but the roadmap,
    /api/state and prerequisite unlocking read the stored status — so leaving
    it "complete" would keep certifying work that has since regressed.
    """
    lesson = _lesson()
    store = ProgressStore(tmp_path / "progress.json")
    for block in lesson.mandatory_validators():
        store.record_validation(lesson.id, block["id"], True)
    for block_id in lesson.required_quiz_ids():
        store.record_quiz(lesson.id, block_id, True, lesson.concepts)
    for block_id in lesson.required_explain_ids():
        store.record_explain(lesson.id, block_id, True, lesson.concepts)
    complete, _ = store.mark_complete(lesson)
    assert complete
    assert store.snapshot()["lessons"][lesson.id]["status"] == "complete"

    failing = lesson.mandatory_validators()[0]["id"]
    store.record_validation(lesson.id, failing, False)

    record = store.snapshot()["lessons"][lesson.id]
    assert record["status"] == "in_progress"
    assert record["completed_at"] is None
    still_complete, missing = store.lesson_completion(lesson)
    assert not still_complete and missing


def test_completion_requires_mandatory_validation(tmp_path: Path) -> None:
    store = ProgressStore(tmp_path / "progress.json")
    lesson = _lesson()
    store.mark_started(lesson.id)
    complete, missing = store.mark_complete(lesson)
    assert not complete
    assert any("validation" in item for item in missing)


def test_completion_requires_quiz_and_explain(tmp_path: Path) -> None:
    store = ProgressStore(tmp_path / "progress.json")
    lesson = _lesson("d1-argparse-logging")  # has quiz AND explain AND validator
    store.mark_started(lesson.id)
    for block in lesson.mandatory_validators():
        store.record_validation(lesson.id, block["id"], True)
    complete, missing = store.mark_complete(lesson)
    assert not complete
    assert any("quiz" in item for item in missing)
    assert any("explain" in item for item in missing)


def test_failed_validation_blocks_completion(tmp_path: Path) -> None:
    store = ProgressStore(tmp_path / "progress.json")
    lesson = _lesson()
    store.record_validation(lesson.id, "verify", False)
    store.record_explain(lesson.id, "explain", True, lesson.concepts)
    complete, missing = store.mark_complete(lesson)
    assert not complete
    assert any("validation" in item for item in missing)


def test_full_completion_flow(tmp_path: Path) -> None:
    store = ProgressStore(tmp_path / "progress.json")
    lesson = _lesson()
    store.mark_started(lesson.id)
    store.record_validation(lesson.id, "verify", True)
    store.record_explain(lesson.id, "explain", True, lesson.concepts)
    complete, missing = store.mark_complete(lesson)
    assert complete, missing
    assert store.lesson_status(lesson) == "complete"


def test_resume_skips_completed_and_unavailable(tmp_path: Path) -> None:
    curriculum = load_curriculum(LEARNING_ROOT, validator_names())
    store = ProgressStore(tmp_path / "progress.json")
    first = curriculum.lessons[curriculum.ordered_lesson_ids[0]]
    store.mark_started(first.id)
    store.record_validation(first.id, "verify", True)
    store.record_explain(first.id, "explain", True, first.concepts)
    store.mark_complete(first)

    resume = store.resume_lesson_id(curriculum)
    assert resume == curriculum.ordered_lesson_ids[1]
    assert curriculum.lessons[resume].status == "available"


def test_concept_mastery_tracks_weakness(tmp_path: Path) -> None:
    store = ProgressStore(tmp_path / "progress.json")
    store.record_quiz("l1", "q1", True, ("timing",))
    store.record_quiz("l1", "q2", False, ("timing",))
    store.record_quiz("l1", "q3", False, ("timing",))
    mastery = store.concept_mastery(("timing", "networking"))
    assert mastery["timing"]["correct"] == 1
    assert mastery["timing"]["incorrect"] == 2
    assert mastery["timing"]["weak"] is True
    assert mastery["networking"]["score"] is None


def test_interview_weights_prefer_weak_concepts(tmp_path: Path) -> None:
    store = ProgressStore(tmp_path / "progress.json")
    store.record_quiz("l1", "q1", False, ("timing",))
    store.record_interview("iq-strong", True)
    weights = store.interview_weights(
        {"iq-weak": ("timing",), "iq-strong": ("logging",)}
    )
    assert weights["iq-weak"] > weights["iq-strong"]


def test_answering_correctly_stops_a_question_dominating_the_pool(
    tmp_path: Path,
) -> None:
    """Correct answers must decay a question's weight, not raise it.

    Interview mode promises weak concepts come back more often. If every
    correct answer nudged the weight up, a question answered right ten times
    would eventually outrank questions never seen at all — the opposite of
    what the mode is for.
    """
    store = ProgressStore(tmp_path / "progress.json")
    questions = {"iq-known": ("timing",), "iq-unseen": ("timing",)}

    baseline = store.interview_weights(questions)["iq-known"]
    for _ in range(5):
        store.record_interview("iq-known", True, ("timing",))
    after = store.interview_weights(questions)

    assert after["iq-known"] < baseline
    assert after["iq-known"] < after["iq-unseen"]
    assert after["iq-known"] > 0, "a mastered question must stay in the pool"


def test_missed_interview_questions_resurface(tmp_path: Path) -> None:
    store = ProgressStore(tmp_path / "progress.json")
    questions = {"iq-missed": ("timing",), "iq-unseen": ("timing",)}
    store.record_interview("iq-missed", False, ("timing",))
    weights = store.interview_weights(questions)
    assert weights["iq-missed"] > weights["iq-unseen"]


def test_interview_answers_move_concept_mastery(tmp_path: Path) -> None:
    """Interview mode advertises weak-area tracking; answers must feed it."""
    store = ProgressStore(tmp_path / "progress.json")
    store.record_interview("iq-1", False, ("timing",))
    store.record_interview("iq-2", False, ("timing",))
    mastery = store.concept_mastery(("timing",))
    assert mastery["timing"]["incorrect"] == 2
    assert mastery["timing"]["weak"] is True


def test_a_miss_resurfaces_a_previously_mastered_question(tmp_path: Path) -> None:
    """Decay must follow the current streak, not the lifetime total.

    Otherwise six correct answers bury the question so deeply that a later
    miss cannot lift it back out — the model would be least likely to ask
    exactly what you just got wrong.
    """
    store = ProgressStore(tmp_path / "progress.json")
    questions = {"iq-known": ("timing",), "iq-unseen": ("timing",)}
    for _ in range(6):
        store.record_interview("iq-known", True, ("timing",))
    buried = store.interview_weights(questions)["iq-known"]

    store.record_interview("iq-known", False, ("timing",))
    after_miss = store.interview_weights(questions)

    assert after_miss["iq-known"] > buried
    assert after_miss["iq-known"] > after_miss["iq-unseen"], (
        "a question just answered wrong must outrank one never seen"
    )


def test_expected_red_demonstrations_do_not_penalise_mastery(tmp_path: Path) -> None:
    """A non-mandatory check that is meant to fail is not a learner mistake."""
    store = ProgressStore(tmp_path / "progress.json")
    store.record_validation(
        "d14-capstone", "v-symptom", False, mandatory=False, concepts=("timing",)
    )
    assert store.concept_mastery(("timing",))["timing"]["score"] is None

    store.record_validation(
        "d14-capstone", "v-real", False, mandatory=True, concepts=("timing",)
    )
    assert store.concept_mastery(("timing",))["timing"]["incorrect"] == 1


def test_a_broken_prerequisite_invalidates_completed_dependents(
    tmp_path: Path,
) -> None:
    """Failing A must stop C completing through an already-complete B.

    Prerequisite checks read stored statuses, so without evaluating the chain
    against current gate outcomes a single revocation leaves every downstream
    lesson certified on a foundation that no longer holds.
    """
    curriculum = load_curriculum(LEARNING_ROOT, validator_names())
    chain = ["d1-import-no-side-effects", "d1-main-entrypoint", "d1-argparse-logging"]
    store = ProgressStore(tmp_path / "progress.json")

    for lesson_id in chain:
        lesson = curriculum.lessons[lesson_id]
        for block in lesson.mandatory_validators():
            store.record_validation(lesson.id, block["id"], True)
        for block_id in lesson.required_quiz_ids():
            store.record_quiz(lesson.id, block_id, True, lesson.concepts)
        for block_id in lesson.required_explain_ids():
            store.record_explain(lesson.id, block_id, True, lesson.concepts)
        complete, missing = store.mark_complete(lesson, curriculum)
        assert complete, missing

    first = curriculum.lessons[chain[0]]
    store.record_validation(first.id, first.mandatory_validators()[0]["id"], False)

    last = curriculum.lessons[chain[-1]]
    complete, missing = store.lesson_completion(last, curriculum)
    assert not complete
    assert any("prerequisite" in reason for reason in missing)


def test_a_non_boolean_pass_cannot_certify_a_lesson(tmp_path: Path) -> None:
    """Only a real boolean counts as a passed mandatory validation.

    The loader rejects the file, and the gate is the second lock on the same
    door: if a damaged record ever reached it, `"false"` must not read as a
    pass just because a non-empty string is truthy.
    """
    curriculum = load_curriculum(LEARNING_ROOT, validator_names())
    lesson = curriculum.lessons["d1-import-no-side-effects"]
    store = ProgressStore(tmp_path / "progress.json")
    for block_id in lesson.required_quiz_ids():
        store.record_quiz(lesson.id, block_id, True, lesson.concepts)
    for block_id in lesson.required_explain_ids():
        store.record_explain(lesson.id, block_id, True, lesson.concepts)
    for block in lesson.mandatory_validators():
        store.record_validation(lesson.id, block["id"], True)
    assert store.lesson_completion(lesson, curriculum)[0]

    # Reach past the API the way a hand-edited file would.
    snapshot = store.snapshot()
    for outcome in snapshot["lessons"][lesson.id]["validations"].values():
        outcome["passed"] = "false"
    store._state = snapshot  # noqa: SLF001 - simulating a damaged record

    complete, missing = store.lesson_completion(lesson, curriculum)
    assert not complete
    assert any("mandatory validation" in reason for reason in missing)


def test_a_bad_encoding_takes_the_reset_path(tmp_path: Path) -> None:
    """A truncated multi-byte sequence is corruption, not a crash.

    UnicodeDecodeError is neither an OSError nor a JSONDecodeError, so the
    shape a crash mid-write leaves behind used to take down the server at
    startup instead of being backed up and reset.
    """
    path = tmp_path / "progress.json"
    path.write_bytes(b'{"version": 1, "lessons": {"l1": {"status": "compl\xff')

    store = ProgressStore(path)

    assert store.snapshot()["lessons"] == {}
    assert path.with_suffix(".corrupt.json").exists()


def test_navigation_and_counts_follow_the_recursive_gate(tmp_path: Path) -> None:
    """The roadmap, the counters and the gate must agree.

    Completing A then B and then breaking A leaves B stored "complete".
    Reading the stored status showed C unlocked and counted B as done, while
    the recursive gate refused to complete C and blamed the prerequisite the
    roadmap had just ticked green.
    """
    curriculum = load_curriculum(LEARNING_ROOT, validator_names())
    chain = ["d1-import-no-side-effects", "d1-main-entrypoint", "d1-argparse-logging"]
    store = ProgressStore(tmp_path / "progress.json")

    for lesson_id in chain[:2]:
        lesson = curriculum.lessons[lesson_id]
        for block in lesson.mandatory_validators():
            store.record_validation(lesson.id, block["id"], True)
        for block_id in lesson.required_quiz_ids():
            store.record_quiz(lesson.id, block_id, True, lesson.concepts)
        for block_id in lesson.required_explain_ids():
            store.record_explain(lesson.id, block_id, True, lesson.concepts)
        assert store.mark_complete(lesson, curriculum)[0]

    assert store.completion_map(curriculum)[chain[1]] is True

    first = curriculum.lessons[chain[0]]
    store.record_validation(first.id, first.mandatory_validators()[0]["id"], False)

    complete = store.completion_map(curriculum)
    # B is still stored "complete" — that is exactly the drift being guarded.
    assert store.snapshot()["lessons"][chain[1]]["status"] == "complete"
    assert complete[chain[0]] is False
    assert complete[chain[1]] is False, "B cannot stand on a broken A"

    last = curriculum.lessons[chain[2]]
    assert not all(complete.get(p, False) for p in last.prerequisites), (
        "the roadmap would show C unlocked while /api/complete refuses it"
    )
    assert store.resume_lesson_id(curriculum) == chain[0]
    assert sum(1 for done in complete.values() if done) == 0


def test_passing_every_gate_is_not_the_same_as_finishing(tmp_path: Path) -> None:
    """Completion is the learner's act, not a side effect of the last check.

    Someone who runs the final validator and then closes the tab has not said
    "done". Treating that as complete unlocked the next lesson and routed them
    there, while mark_complete still refused it as a prerequisite — the mirror
    of the drift that made navigation read the recursive gate in the first
    place, and just as contradictory.
    """
    curriculum = load_curriculum(LEARNING_ROOT, validator_names())
    first, second = "d1-import-no-side-effects", "d1-main-entrypoint"
    store = ProgressStore(tmp_path / "progress.json")

    lesson = curriculum.lessons[first]
    store.mark_started(lesson.id)
    for block in lesson.mandatory_validators():
        store.record_validation(lesson.id, block["id"], True)
    for block_id in lesson.required_quiz_ids():
        store.record_quiz(lesson.id, block_id, True, lesson.concepts)
    for block_id in lesson.required_explain_ids():
        store.record_explain(lesson.id, block_id, True, lesson.concepts)

    # Every gate passes; Continue was never pressed.
    assert store.lesson_completion(lesson, curriculum)[0]
    assert store.snapshot()["lessons"][first]["status"] == "in_progress"

    complete = store.completion_map(curriculum)
    assert complete[first] is False
    dependent = curriculum.lessons[second]
    assert not all(complete.get(p, False) for p in dependent.prerequisites)
    assert store.resume_lesson_id(curriculum) == first

    assert store.mark_complete(lesson, curriculum)[0]
    assert store.completion_map(curriculum)[first] is True
    assert store.resume_lesson_id(curriculum) == second


def test_progress_file_is_written_atomically(tmp_path: Path) -> None:
    path = tmp_path / "progress.json"
    store = ProgressStore(path)
    store.mark_started("x")
    assert not (tmp_path / "progress.tmp").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["lessons"]["x"]["status"] == "in_progress"
