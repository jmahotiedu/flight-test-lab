"""Learner progress persistence.

State lives in ``learning/.progress.json`` (gitignored).  Writes are atomic
(temp file + ``os.replace``) and a corrupt file is backed up and reset rather
than crashing the server.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from learning.server.curriculum import Curriculum, Lesson

STATE_VERSION = 1

# A mastered question stays in the pool for occasional review rather than
# disappearing, so interview mode never runs out of material.
MIN_INTERVIEW_WEIGHT = 0.05


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "last_lesson_id": None,
        "lessons": {},
        "concepts": {},
        "interview": {},
    }


def _record_is_valid(section: str, record: dict[str, Any]) -> bool:
    """Check the fields a section's records must hold, and their types.

    Missing keys are tolerated (they are filled from the template on use);
    wrong *types* are not, because those surface much later as an exception
    inside a request handler instead of the documented back-up-and-reset.
    """
    integer_fields: tuple[str, ...]
    list_fields: tuple[str, ...] = ()
    if section == "lessons":
        integer_fields = ("attempts", "hints_used", "quiz_attempts")
        list_fields = ("steps_done", "quiz_correct", "explain_done")
    elif section == "concepts":
        integer_fields = ("correct", "incorrect")
    elif section == "interview":
        # streak is read with int() when weighting questions, so a non-integer
        # here raises ValueError on the first /api/interview instead of taking
        # the documented reset path.
        integer_fields = ("correct", "incorrect", "streak")
    else:
        return True

    for field in integer_fields:
        value = record.get(field)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            return False
    for field in list_fields:
        value = record.get(field)
        if value is not None and (
            not isinstance(value, list)
            or not all(isinstance(item, str) for item in value)
        ):
            return False
    if section == "lessons":
        status = record.get("status")
        if status is not None and not isinstance(status, str):
            return False
        validations = record.get("validations")
        if validations is not None:
            if not isinstance(validations, dict):
                return False
            for outcome in validations.values():
                # "passed" is the field a mandatory gate is read from, and it
                # is read for truth. {"passed": "false"} is a non-empty string,
                # so a damaged file would certify the lesson instead of taking
                # the documented backup-and-reset path.
                if not isinstance(outcome, dict):
                    return False
                verdict = outcome.get("passed")
                if verdict is not None and not isinstance(verdict, bool):
                    return False
                at = outcome.get("at")
                if at is not None and not isinstance(at, str):
                    return False
    return True


class ProgressStore:
    """Thread-safe, atomic learner-progress store."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._state = self._load()

    def _backup_locked(self) -> None:
        backup = self._path.with_suffix(".corrupt.json")
        with contextlib.suppress(OSError):
            shutil.copy2(self._path, backup)

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return _empty_state()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        # UnicodeDecodeError is neither an OSError nor a JSONDecodeError, so a
        # progress file with a truncated multi-byte sequence — the shape a
        # crash mid-write leaves behind — would otherwise take down the server
        # at import instead of taking the documented reset path.
        except (json.JSONDecodeError, OSError, UnicodeError):
            self._backup_locked()
            return _empty_state()
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            self._backup_locked()
            return _empty_state()
        # Valid JSON is not the same as a valid state file.  Something like
        # {"version": 1, "lessons": []} parses fine and then fails much later
        # with an AttributeError deep in a request handler; check the shape of
        # every section here so a damaged file takes the documented
        # back-up-and-reset path instead of breaking the dashboard.
        for key, default in _empty_state().items():
            value = data.setdefault(key, default)
            if key in ("version", "last_lesson_id"):
                continue
            if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, dict) for k, v in value.items()
            ):
                self._backup_locked()
                return _empty_state()
            # The record *fields* have to be checked too, not just that each
            # record is a dict: {"concepts": {"x": {"correct": "bad"}}} is
            # shaped correctly and still raises the first time /api/state
            # converts that value to int, which is a crash rather than the
            # documented reset.
            if not all(_record_is_valid(key, record) for record in value.values()):
                self._backup_locked()
                return _empty_state()
        last_lesson = data.get("last_lesson_id")
        if last_lesson is not None and not isinstance(last_lesson, str):
            self._backup_locked()
            return _empty_state()
        return data

    def _save_locked(self) -> None:
        temp = self._path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(self._state, indent=2, sort_keys=True), encoding="utf-8"
        )
        temp.replace(self._path)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = json.loads(json.dumps(self._state))
            return result

    @staticmethod
    def _empty_lesson_record() -> dict[str, Any]:
        return {
            "status": "not_started",
            "steps_done": [],
            "validations": {},
            "quiz_correct": [],
            "quiz_attempts": 0,
            "explain_done": [],
            "attempts": 0,
            "hints_used": 0,
            "started_at": None,
            "completed_at": None,
        }

    def _lesson_record_locked(self, lesson_id: str) -> dict[str, Any]:
        lessons: dict[str, dict[str, Any]] = self._state["lessons"]
        record = lessons.setdefault(lesson_id, self._empty_lesson_record())
        # Fill any absent keys from the template. The loader tolerates missing
        # fields (they are optional in a persisted file), so a record like {}
        # would otherwise reach mark_started and raise KeyError on
        # "started_at" — a crash instead of the documented recovery.
        for key, default in self._empty_lesson_record().items():
            record.setdefault(key, default)
        return record

    def mark_started(self, lesson_id: str) -> None:
        with self._lock:
            record = self._lesson_record_locked(lesson_id)
            if record["started_at"] is None:
                record["started_at"] = _now()
            if record["status"] == "not_started":
                record["status"] = "in_progress"
            record["attempts"] += 1
            self._state["last_lesson_id"] = lesson_id
            self._save_locked()

    def record_step(self, lesson_id: str, block_id: str) -> None:
        with self._lock:
            record = self._lesson_record_locked(lesson_id)
            if block_id not in record["steps_done"]:
                record["steps_done"].append(block_id)
            self._state["last_lesson_id"] = lesson_id
            self._save_locked()

    def record_validation(
        self,
        lesson_id: str,
        block_id: str,
        passed: bool,
        *,
        mandatory: bool = True,
        concepts: tuple[str, ...] = (),
    ) -> None:
        with self._lock:
            record = self._lesson_record_locked(lesson_id)
            record["validations"][block_id] = {"passed": passed, "at": _now()}
            # A mandatory validator's outcome is evidence about a concept,
            # exactly as a quiz answer is. Non-mandatory ones are excluded:
            # some are demonstrations that are *supposed* to go red (Day 14's
            # symptom probe), and counting those as misses would penalise the
            # learner for following the instructions.
            if concepts and mandatory:
                self._bump_concepts_locked(concepts, passed)
            # Only a *mandatory* check can un-complete a lesson. Some lessons
            # ship a demonstration check that is meant to go red (Day 14's
            # symptom probe); re-running one of those after finishing the
            # lesson must not revoke the certification, or the roadmap would
            # contradict lesson_completion().
            if not passed and mandatory and record.get("status") == "complete":
                # Re-running a check that now fails un-completes the lesson.
                # lesson_completion() already reports the missing gate, but the
                # roadmap, /api/state and prerequisite unlocking read the
                # stored status — so leaving it "complete" would keep
                # certifying work that has since regressed.
                record["status"] = "in_progress"
                record["completed_at"] = None
            self._state["last_lesson_id"] = lesson_id
            self._save_locked()

    def record_quiz(
        self, lesson_id: str, block_id: str, correct: bool, concepts: tuple[str, ...]
    ) -> None:
        with self._lock:
            record = self._lesson_record_locked(lesson_id)
            record["quiz_attempts"] += 1
            if correct and block_id not in record["quiz_correct"]:
                record["quiz_correct"].append(block_id)
            self._bump_concepts_locked(concepts, correct)
            self._state["last_lesson_id"] = lesson_id
            self._save_locked()

    def record_explain(
        self, lesson_id: str, block_id: str, passed: bool, concepts: tuple[str, ...]
    ) -> None:
        with self._lock:
            record = self._lesson_record_locked(lesson_id)
            if passed and block_id not in record["explain_done"]:
                record["explain_done"].append(block_id)
            self._bump_concepts_locked(concepts, passed)
            self._state["last_lesson_id"] = lesson_id
            self._save_locked()

    def record_hint(self, lesson_id: str) -> None:
        with self._lock:
            record = self._lesson_record_locked(lesson_id)
            record["hints_used"] += 1
            self._save_locked()

    def record_interview(
        self, question_id: str, correct: bool, concepts: tuple[str, ...] = ()
    ) -> None:
        """Record an interview answer and credit its concepts.

        The answer feeds concept mastery for the same reason a lesson quiz
        does: interview mode advertises that it tracks weak areas, and it can
        only do that if answering there moves the same numbers.
        """
        with self._lock:
            entry = self._state["interview"].setdefault(
                question_id, {"correct": 0, "incorrect": 0, "streak": 0}
            )
            entry["correct" if correct else "incorrect"] += 1
            entry["streak"] = int(entry.get("streak", 0)) + 1 if correct else 0
            if concepts:
                self._bump_concepts_locked(concepts, correct)
            self._save_locked()

    def _bump_concepts_locked(self, concepts: tuple[str, ...], correct: bool) -> None:
        for concept in concepts:
            entry = self._state["concepts"].setdefault(
                concept, {"correct": 0, "incorrect": 0, "last_seen": None}
            )
            entry["correct" if correct else "incorrect"] += 1
            entry["last_seen"] = _now()

    def _lesson_is_complete_locked(
        self,
        lesson_id: str,
        curriculum: Curriculum | None,
        seen: set[str],
    ) -> bool:
        """Is this lesson genuinely complete, all the way up the chain?

        Two conditions, and both are load-bearing:

        * The learner marked it complete.  Passing the last gate is not the
          same act as finishing — a learner who runs the final check and then
          closes the tab has not said "done", and treating that as complete
          would unlock the next lesson while ``mark_complete`` still refuses
          it as a prerequisite.
        * Its gates still hold, recursively.  Completing A then B and then
          breaking A's mandatory check must not leave C completable through a
          B that is only nominally complete.

        This is the single definition of "complete"; prerequisites, roadmap
        locks, navigation and the counters all ask it, so they cannot drift
        apart in either direction.
        """
        record = self._state["lessons"].get(lesson_id)
        if record is None or record.get("status") != "complete":
            return False
        if curriculum is None or lesson_id in seen:
            return True
        lesson = curriculum.lessons.get(lesson_id)
        if lesson is None:
            return True
        seen.add(lesson_id)
        complete, _ = self._lesson_completion_locked(lesson, curriculum, seen)
        return complete

    def lesson_completion(
        self, lesson: Lesson, curriculum: Curriculum | None = None
    ) -> tuple[bool, list[str]]:
        """Return (complete, list-of-missing-requirements) for a lesson."""
        with self._lock:
            return self._lesson_completion_locked(lesson, curriculum, set())

    def _lesson_completion_locked(
        self,
        lesson: Lesson,
        curriculum: Curriculum | None = None,
        seen: set[str] | None = None,
    ) -> tuple[bool, list[str]]:
        """Gate evaluation, with the caller already holding the lock.

        mark_complete needs to evaluate the gates and write the status without
        releasing the lock in between: otherwise a concurrent /api/validate
        recording a mandatory failure lands in the gap, sees status
        "in_progress" so skips revocation, and completion then writes
        "complete" over a currently-failing lesson.
        """
        if lesson.status == "unavailable":
            return False, ["lesson is unavailable"]
        record = self._state["lessons"].get(lesson.id)
        if record is None:
            missing = ["lesson not started"]
            return False, missing
        missing = []
        # Prerequisites are part of the gate, not just a roadmap decoration.
        # Without this a client can complete a locked lesson directly through
        # the API, and because unlocking trusts stored statuses, that would
        # cascade: a broken prerequisite chain unlocks everything downstream.
        #
        # When the curriculum is available the chain is evaluated against
        # *current* gate outcomes rather than stored statuses, so a mandatory
        # check that started failing invalidates everything downstream of it
        # even if those lessons were completed earlier.
        seen = set(seen or ())
        seen.add(lesson.id)
        for prerequisite in lesson.prerequisites:
            if not self._lesson_is_complete_locked(prerequisite, curriculum, seen):
                missing.append(f"prerequisite {prerequisite!r} is not complete")
        validations = record["validations"]
        for block in lesson.mandatory_validators():
            outcome = validations.get(block["id"])
            # `is True`, not truthiness: only a real boolean recorded by
            # record_validation counts as a pass. The loader rejects anything
            # else, and this is the second lock on the same door.
            if not isinstance(outcome, dict) or outcome.get("passed") is not True:
                missing.append(f"mandatory validation {block['id']!r} not passed")
        for block_id in lesson.required_quiz_ids():
            if block_id not in record["quiz_correct"]:
                missing.append(f"quiz {block_id!r} not answered correctly")
        for block_id in lesson.required_explain_ids():
            if block_id not in record["explain_done"]:
                missing.append(f"explain {block_id!r} not answered")
        return (not missing, missing)

    def mark_complete(
        self, lesson: Lesson, curriculum: Curriculum | None = None
    ) -> tuple[bool, list[str]]:
        with self._lock:
            complete, missing = self._lesson_completion_locked(lesson, curriculum)
            if not complete:
                return False, missing
            record = self._lesson_record_locked(lesson.id)
            record["status"] = "complete"
            record["completed_at"] = _now()
            self._save_locked()
        return True, []

    def completion_map(self, curriculum: Curriculum) -> dict[str, bool]:
        """Which lessons are complete, by the same rule prerequisites use.

        Navigation, roadmap locks and the progress counters all used to read
        the *stored* status, which drifts from the gate in both directions:
        a lesson whose prerequisite later broke stays stored "complete", and
        a lesson whose gates all pass is not complete until the learner says
        so.  Either drift shows the roadmap one thing and has
        ``/api/complete`` insist on another.

        One evaluation of one predicate, shared by every caller.  Reads
        recorded outcomes only — no validator re-runs.
        """
        with self._lock:
            return {
                lesson_id: self._lesson_is_complete_locked(lesson_id, curriculum, set())
                for lesson_id in curriculum.ordered_lesson_ids
            }

    def lesson_status(self, lesson: Lesson) -> str:
        with self._lock:
            record = self._state["lessons"].get(lesson.id)
        if record is None:
            return "not_started"
        return str(record["status"])

    def resume_lesson_id(self, curriculum: Curriculum) -> str:
        """First available, incomplete lesson in program order.

        Prerequisites are honored against the *current* gate outcome, not the
        stored status, so the learner never lands on a lesson whose
        prerequisite chain is broken — /api/validate would refuse to complete
        it and name a prerequisite the roadmap shows as done.
        """
        complete = self.completion_map(curriculum)
        for lesson_id in curriculum.ordered_lesson_ids:
            lesson = curriculum.lessons[lesson_id]
            if lesson.status == "unavailable":
                continue
            if complete.get(lesson_id):
                continue
            if all(complete.get(prereq, False) for prereq in lesson.prerequisites):
                return lesson_id
        # Everything available is complete: return the last lesson so the
        # UI has somewhere sensible to land.
        return curriculum.ordered_lesson_ids[-1]

    def concept_mastery(self, concepts: tuple[str, ...]) -> dict[str, dict[str, Any]]:
        with self._lock:
            stored = self._state["concepts"]
            mastery: dict[str, dict[str, Any]] = {}
            for concept in concepts:
                entry = stored.get(concept, {"correct": 0, "incorrect": 0})
                correct = int(entry.get("correct", 0))
                incorrect = int(entry.get("incorrect", 0))
                total = correct + incorrect
                mastery[concept] = {
                    "correct": correct,
                    "incorrect": incorrect,
                    "score": (correct / total) if total else None,
                    "weak": total > 0 and correct / total < 0.7,
                }
            return mastery

    def interview_weights(
        self, question_concepts: dict[str, tuple[str, ...]]
    ) -> dict[str, float]:
        """Weight each interview question by weakness of its concepts.

        Unseen questions get a neutral weight, misses weigh much more so they
        resurface, and each correct answer *decays* the weight so a question
        you have repeatedly answered right stops crowding out ones you have
        never seen.  The floor keeps mastered questions in the pool for
        occasional review instead of removing them.
        """
        mastery = self.concept_mastery(
            tuple({c for cs in question_concepts.values() for c in cs})
        )
        with self._lock:
            interview_state = self._state["interview"]
        weights: dict[str, float] = {}
        for qid, concepts in question_concepts.items():
            weight = 1.0
            for concept in concepts:
                entry = mastery.get(concept, {})
                score = entry.get("score")
                if score is None:
                    weight += 0.5
                else:
                    weight += 1.0 - float(score)
            history = interview_state.get(qid, {"correct": 0, "incorrect": 0})
            weight += 2.0 * float(history.get("incorrect", 0))
            # Decay on the *current streak*, not the lifetime total: a miss
            # resets the streak, so a question answered right six times and
            # then missed comes back immediately instead of staying buried
            # under its own history — the opposite of what this mode is for.
            streak = min(int(history.get("streak", 0)), 6)
            weight *= 0.5**streak
            weights[qid] = max(weight, MIN_INTERVIEW_WEIGHT)
        return weights
