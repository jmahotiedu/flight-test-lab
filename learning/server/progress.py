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


class ProgressStore:
    """Thread-safe, atomic learner-progress store."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return _empty_state()
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            backup = self._path.with_suffix(".corrupt.json")
            with contextlib.suppress(OSError):
                shutil.copy2(self._path, backup)
            return _empty_state()
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            return _empty_state()
        for key, default in _empty_state().items():
            data.setdefault(key, default)
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

    def _lesson_record_locked(self, lesson_id: str) -> dict[str, Any]:
        lessons: dict[str, dict[str, Any]] = self._state["lessons"]
        record = lessons.setdefault(
            lesson_id,
            {
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
            },
        )
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

    def record_validation(self, lesson_id: str, block_id: str, passed: bool) -> None:
        with self._lock:
            record = self._lesson_record_locked(lesson_id)
            record["validations"][block_id] = {"passed": passed, "at": _now()}
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

    def record_interview(self, question_id: str, correct: bool) -> None:
        with self._lock:
            entry = self._state["interview"].setdefault(
                question_id, {"correct": 0, "incorrect": 0}
            )
            entry["correct" if correct else "incorrect"] += 1
            self._save_locked()

    def _bump_concepts_locked(self, concepts: tuple[str, ...], correct: bool) -> None:
        for concept in concepts:
            entry = self._state["concepts"].setdefault(
                concept, {"correct": 0, "incorrect": 0, "last_seen": None}
            )
            entry["correct" if correct else "incorrect"] += 1
            entry["last_seen"] = _now()

    def lesson_completion(self, lesson: Lesson) -> tuple[bool, list[str]]:
        """Return (complete, list-of-missing-requirements) for a lesson."""
        if lesson.status == "unavailable":
            return False, ["lesson is unavailable"]
        with self._lock:
            record = self._state["lessons"].get(lesson.id)
        if record is None:
            missing = ["lesson not started"]
            return False, missing
        missing = []
        validations = record["validations"]
        for block in lesson.mandatory_validators():
            outcome = validations.get(block["id"])
            if not outcome or not outcome.get("passed"):
                missing.append(f"mandatory validation {block['id']!r} not passed")
        for block_id in lesson.required_quiz_ids():
            if block_id not in record["quiz_correct"]:
                missing.append(f"quiz {block_id!r} not answered correctly")
        for block_id in lesson.required_explain_ids():
            if block_id not in record["explain_done"]:
                missing.append(f"explain {block_id!r} not answered")
        return (not missing, missing)

    def mark_complete(self, lesson: Lesson) -> tuple[bool, list[str]]:
        complete, missing = self.lesson_completion(lesson)
        if not complete:
            return False, missing
        with self._lock:
            record = self._lesson_record_locked(lesson.id)
            record["status"] = "complete"
            record["completed_at"] = _now()
            self._save_locked()
        return True, []

    def lesson_status(self, lesson: Lesson) -> str:
        with self._lock:
            record = self._state["lessons"].get(lesson.id)
        if record is None:
            return "not_started"
        return str(record["status"])

    def resume_lesson_id(self, curriculum: Curriculum) -> str:
        """First available, incomplete lesson in program order.

        Prerequisites are honored: a lesson whose prerequisites are not all
        complete is skipped, so the learner always lands on actionable work.
        """
        with self._lock:
            lessons_state = dict(self._state["lessons"])
        for lesson_id in curriculum.ordered_lesson_ids:
            lesson = curriculum.lessons[lesson_id]
            if lesson.status == "unavailable":
                continue
            if lessons_state.get(lesson_id, {}).get("status") == "complete":
                continue
            if all(
                lessons_state.get(prereq, {}).get("status") == "complete"
                for prereq in lesson.prerequisites
            ):
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

        Unseen questions get a neutral weight; questions tagged with weak
        concepts weigh more, so misses resurface.
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
            weight += 0.2 * float(history.get("correct", 0))
            weights[qid] = weight
        return weights
