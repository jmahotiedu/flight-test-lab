"""Curriculum loading and schema validation.

The curriculum is data-driven: ``curriculum.json`` describes the program
(days, concept taxonomy), ``modules/dayNN.json`` files describe lessons, and
``interview.json`` describes interview-mode questions.  Everything is checked
at load time so a malformed curriculum fails loudly instead of confusing the
learner later.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from learning.server.toolchain import (
    Toolchain,
    cached_toolchain,
    requirement_reason,
)

BLOCK_TYPES = frozenset({"learn", "predict", "do", "verify", "quiz", "explain"})

# Native capabilities a lesson may declare via "requires".
KNOWN_REQUIREMENTS = frozenset({"cpp-build", "gdb"})

LESSON_REQUIRED_STRING_FIELDS = ("id", "title", "objective")

# Required keys per block type, beyond the shared "id"/"type".
BLOCK_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "learn": ("text",),
    "predict": ("question", "options", "answer_index", "reveal"),
    "do": ("instructions",),
    "verify": ("validator",),
    "quiz": ("question", "options", "answer_index"),
    "explain": ("question", "keywords", "sample_answer"),
}


class CurriculumError(ValueError):
    """Raised when curriculum content fails schema validation."""


@dataclass(frozen=True, slots=True)
class Lesson:
    id: str
    day: int
    title: str
    objective: str
    estimated_minutes: int
    status: str  # "available" | "unavailable"
    unavailable_reason: str
    concepts: tuple[str, ...]
    prerequisites: tuple[str, ...]
    source_files: tuple[str, ...]
    flow: str | None
    blocks: tuple[dict[str, Any], ...]
    hints: tuple[dict[str, Any], ...]
    requirements: tuple[str, ...]
    requires: tuple[str, ...] = ()  # native capabilities, e.g. "cpp-build"

    def mandatory_validators(self) -> list[dict[str, Any]]:
        return [
            block
            for block in self.blocks
            if block["type"] == "verify" and block.get("mandatory", False)
        ]

    def required_quiz_ids(self) -> list[str]:
        return [str(block["id"]) for block in self.blocks if block["type"] == "quiz"]

    def required_explain_ids(self) -> list[str]:
        return [str(block["id"]) for block in self.blocks if block["type"] == "explain"]

    def quiz_block(self, block_id: str) -> dict[str, Any] | None:
        for block in self.blocks:
            if block["type"] in ("quiz", "predict") and block["id"] == block_id:
                return block
        return None

    def explain_block(self, block_id: str) -> dict[str, Any] | None:
        for block in self.blocks:
            if block["type"] == "explain" and block["id"] == block_id:
                return block
        return None


@dataclass(frozen=True, slots=True)
class Day:
    day: int
    title: str
    summary: str
    lesson_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InterviewQuestion:
    id: str
    question: str
    concepts: tuple[str, ...]
    keywords: tuple[str, ...]
    sample_answer: str


@dataclass(frozen=True, slots=True)
class Curriculum:
    concepts: tuple[str, ...]
    days: tuple[Day, ...]
    lessons: dict[str, Lesson]
    ordered_lesson_ids: tuple[str, ...]
    interview: tuple[InterviewQuestion, ...]
    flows: dict[str, dict[str, Any]] = field(default_factory=dict)


def _fail(path: Path, message: str) -> NoReturn:
    raise CurriculumError(f"{path}: {message}")


def _require_str(data: dict[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        _fail(path, f"missing or empty string field {key!r}")
    return str(value)


def _require_str_list(data: dict[str, Any], key: str, path: Path) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail(path, f"field {key!r} must be a list of strings")
    return [str(item) for item in value]


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CurriculumError(f"{path}: invalid JSON: {exc}") from exc


def _validate_blocks(
    blocks: list[dict[str, Any]], path: Path, known_validators: set[str]
) -> None:
    seen_ids: set[str] = set()
    for block in blocks:
        block_type = block.get("type")
        if block_type not in BLOCK_TYPES:
            _fail(path, f"block has unknown type {block_type!r}")
        block_id = block.get("id")
        if not isinstance(block_id, str) or not block_id:
            _fail(path, f"{block_type} block missing string 'id'")
        if block_id in seen_ids:
            _fail(path, f"duplicate block id {block_id!r}")
        seen_ids.add(block_id)
        for key in BLOCK_REQUIRED_KEYS[str(block_type)]:
            if key not in block:
                _fail(path, f"{block_type} block {block_id!r} missing {key!r}")
        if block_type == "verify":
            validator = block["validator"]
            if validator not in known_validators:
                _fail(
                    path,
                    f"verify block {block_id!r} references unknown validator "
                    f"{validator!r}",
                )
        if block_type in ("quiz", "predict"):
            options = block["options"]
            answer_index = block["answer_index"]
            if not isinstance(options, list) or len(options) < 2:
                _fail(path, f"{block_type} block {block_id!r} needs >= 2 options")
            if not isinstance(answer_index, int) or not 0 <= answer_index < len(
                options
            ):
                _fail(path, f"{block_type} block {block_id!r} bad answer_index")
        if block_type == "explain" and not _require_str_list(block, "keywords", path):
            _fail(path, f"explain block {block_id!r} needs >= 1 keyword")


def _parse_lesson(
    data: dict[str, Any],
    day: int,
    path: Path,
    known_validators: set[str],
    toolchain: Toolchain,
) -> Lesson:
    for key in LESSON_REQUIRED_STRING_FIELDS:
        _require_str(data, key, path)

    estimated = data.get("estimated_minutes")
    if not isinstance(estimated, int) or estimated <= 0:
        _fail(path, "estimated_minutes must be a positive integer")

    status = data.get("status", "available")
    if status not in ("available", "unavailable"):
        _fail(path, f"status must be 'available' or 'unavailable', got {status!r}")
    unavailable_reason = str(data.get("unavailable_reason", ""))
    if status == "unavailable" and not unavailable_reason:
        _fail(path, "unavailable lessons must give unavailable_reason")

    # A lesson may declare native requirements ("cpp-build", "gdb").  Content
    # is authored either way; whether it is offered depends on what this
    # machine can actually verify, so a missing compiler produces an honest
    # explanation instead of a Verify button that always fails.
    requires = _require_str_list(data, "requires", path)
    for requirement in requires:
        if requirement not in KNOWN_REQUIREMENTS:
            _fail(path, f"unknown requires entry {requirement!r}")
        if not toolchain.satisfies(requirement):
            status = "unavailable"
            unavailable_reason = requirement_reason(requirement, toolchain)
            break

    blocks = data.get("blocks", [])
    if status == "available" and not blocks:
        _fail(path, "available lessons must define blocks")
    if not isinstance(blocks, list) or not all(isinstance(b, dict) for b in blocks):
        _fail(path, "blocks must be a list of objects")
    _validate_blocks(blocks, path, known_validators)

    hints = data.get("hints", [])
    if not isinstance(hints, list) or not all(isinstance(h, dict) for h in hints):
        _fail(path, "hints must be a list of objects")
    for index, hint in enumerate(hints):
        if hint.get("level") != index + 1 or not isinstance(hint.get("text"), str):
            _fail(path, f"hint {index + 1} must have sequential level and text")

    return Lesson(
        id=str(data["id"]),
        day=day,
        title=str(data["title"]),
        objective=str(data["objective"]),
        estimated_minutes=int(estimated),
        status=str(status),
        unavailable_reason=unavailable_reason,
        concepts=tuple(_require_str_list(data, "concepts", path)),
        prerequisites=tuple(_require_str_list(data, "prerequisites", path)),
        source_files=tuple(_require_str_list(data, "source_files", path)),
        flow=data.get("flow") if isinstance(data.get("flow"), str) else None,
        blocks=tuple(blocks),
        hints=tuple(hints),
        requirements=tuple(_require_str_list(data, "requirements", path)),
        requires=tuple(requires),
    )


def load_curriculum(
    root: Path,
    known_validators: set[str],
    toolchain: Toolchain | None = None,
) -> Curriculum:
    """Load and fully validate the curriculum under ``root``.

    ``root`` is the ``learning/`` package directory.  ``known_validators``
    is the set of registered validator names; verify blocks referencing
    anything else are rejected.  ``toolchain`` decides whether lessons with
    native ``requires`` are offered — injectable so tests can load the
    curriculum as it would appear on a machine with, or without, a compiler.
    """
    resolved_toolchain = toolchain if toolchain is not None else cached_toolchain()
    curriculum_dir = root / "curriculum"
    program_path = curriculum_dir / "curriculum.json"
    program = _load_json(program_path)
    if not isinstance(program, dict):
        _fail(program_path, "top level must be an object")

    concepts = _require_str_list(program, "concepts", program_path)
    if not concepts:
        _fail(program_path, "concepts must not be empty")
    concept_set = set(concepts)

    lessons: dict[str, Lesson] = {}
    days: list[Day] = []
    ordered: list[str] = []

    day_entries = program.get("days")
    if not isinstance(day_entries, list) or not day_entries:
        _fail(program_path, "days must be a non-empty list")

    for entry in day_entries:
        if not isinstance(entry, dict):
            _fail(program_path, "each day must be an object")
        day_number = entry.get("day")
        if not isinstance(day_number, int) or day_number <= 0:
            _fail(program_path, "each day needs a positive integer 'day'")
        module_name = entry.get("module")
        if not isinstance(module_name, str):
            _fail(program_path, f"day {day_number} missing 'module' file name")
        day_title = _require_str(entry, "title", program_path)
        day_summary = str(entry.get("summary", ""))

        module_path = curriculum_dir / "modules" / module_name
        module = _load_json(module_path)
        if not isinstance(module, dict) or not isinstance(module.get("lessons"), list):
            _fail(module_path, "module must be an object with a 'lessons' list")

        lesson_ids: list[str] = []
        for raw in module["lessons"]:
            if not isinstance(raw, dict):
                _fail(module_path, "each lesson must be an object")
            lesson = _parse_lesson(
                raw, day_number, module_path, known_validators, resolved_toolchain
            )
            if lesson.id in lessons:
                _fail(module_path, f"duplicate lesson id {lesson.id!r}")
            for concept in lesson.concepts:
                if concept not in concept_set:
                    _fail(
                        module_path,
                        f"lesson {lesson.id!r} uses unknown concept {concept!r}",
                    )
            lessons[lesson.id] = lesson
            lesson_ids.append(lesson.id)
            ordered.append(lesson.id)
        days.append(
            Day(
                day=day_number,
                title=day_title,
                summary=day_summary,
                lesson_ids=tuple(lesson_ids),
            )
        )

    if not ordered:
        # An all-empty program loads fine and then makes the first
        # /api/state raise IndexError inside resume_lesson_id, which is the
        # runtime discovery the loader exists to prevent.
        _fail(program_path, "the curriculum defines no lessons at all")

    # Prerequisites must resolve *and* point backwards.  A forward or circular
    # dependency leaves that lesson permanently locked in the roadmap and
    # skipped by resume, which is a curriculum defect the learner would
    # experience as a dead end.  Checking it here means the server refuses to
    # start, rather than relying on someone having run the test suite.
    position = {lesson_id: index for index, lesson_id in enumerate(ordered)}
    for lesson in lessons.values():
        for prerequisite in lesson.prerequisites:
            if prerequisite not in lessons:
                _fail(
                    program_path,
                    f"lesson {lesson.id!r} has unknown prerequisite {prerequisite!r}",
                )
            if prerequisite == lesson.id:
                _fail(
                    program_path,
                    f"lesson {lesson.id!r} lists itself as a prerequisite",
                )
            if position[prerequisite] > position[lesson.id]:
                _fail(
                    program_path,
                    f"lesson {lesson.id!r} depends on {prerequisite!r}, which "
                    "comes later in the program — it could never be unlocked",
                )

    interview_path = curriculum_dir / "interview.json"
    interview_raw = _load_json(interview_path)
    if not isinstance(interview_raw, dict) or not isinstance(
        interview_raw.get("questions"), list
    ):
        _fail(interview_path, "must be an object with a 'questions' list")
    if not interview_raw["questions"]:
        # An empty bank loads fine and then makes the first /api/interview
        # request raise IndexError inside random.choices — a broken mode
        # discovered at runtime, which is exactly what loading loudly is for.
        _fail(interview_path, "must define at least one interview question")
    interview: list[InterviewQuestion] = []
    seen_qids: set[str] = set()
    for raw_q in interview_raw["questions"]:
        if not isinstance(raw_q, dict):
            _fail(interview_path, "each question must be an object")
        qid = _require_str(raw_q, "id", interview_path)
        if qid in seen_qids:
            _fail(interview_path, f"duplicate interview question id {qid!r}")
        seen_qids.add(qid)
        question = _require_str(raw_q, "question", interview_path)
        sample = _require_str(raw_q, "sample_answer", interview_path)
        keywords = _require_str_list(raw_q, "keywords", interview_path)
        if not keywords:
            _fail(interview_path, f"question {qid!r} needs >= 1 keyword")
        q_concepts = _require_str_list(raw_q, "concepts", interview_path)
        for concept in q_concepts:
            if concept not in concept_set:
                _fail(
                    interview_path,
                    f"question {qid!r} uses unknown concept {concept!r}",
                )
        interview.append(
            InterviewQuestion(
                id=qid,
                question=question,
                concepts=tuple(q_concepts),
                keywords=tuple(keywords),
                sample_answer=sample,
            )
        )

    flows: dict[str, dict[str, Any]] = {}
    flows_dir = root / "flows"
    if flows_dir.is_dir():
        for flow_path in sorted(flows_dir.glob("*.json")):
            flow_data = _load_json(flow_path)
            if not isinstance(flow_data, dict) or not isinstance(
                flow_data.get("stages"), list
            ):
                _fail(flow_path, "flow must be an object with a 'stages' list")
            flows[flow_path.stem] = flow_data

    for lesson in lessons.values():
        if lesson.flow is not None and lesson.flow not in flows:
            _fail(
                program_path,
                f"lesson {lesson.id!r} references unknown flow {lesson.flow!r}",
            )

    return Curriculum(
        concepts=tuple(concepts),
        days=tuple(days),
        lessons=lessons,
        ordered_lesson_ids=tuple(ordered),
        interview=tuple(interview),
        flows=flows,
    )
