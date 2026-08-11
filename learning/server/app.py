"""HTTP server for the learning environment.

Binds loopback only.  All mutating endpoints accept identifiers (lesson IDs,
block IDs) — never commands, code, or filesystem paths.  Validators are
resolved from the on-disk curriculum through an allowlisted registry.
"""

from __future__ import annotations

import json
import random
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from learning.server.curriculum import Curriculum, Lesson
from learning.server.progress import ProgressStore
from learning.server.toolchain import Toolchain
from learning.server.validators import (
    ValidatorContext,
    run_validator,
    validator_names,
)

LEARNING_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = LEARNING_ROOT / "static"
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class LearningServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        curriculum: Curriculum,
        progress: ProgressStore,
        context: ValidatorContext,
    ) -> None:
        super().__init__(server_address, LearningHandler)
        self.curriculum = curriculum
        self.progress = progress
        self.context = context


def build_server(
    port: int,
    *,
    repo_root: Path,
    learning_root: Path = LEARNING_ROOT,
    progress_path: Path | None = None,
    toolchain: Toolchain | None = None,
) -> LearningServer:
    """Build the server. ``toolchain`` overrides native-capability detection,
    which lets tests exercise both the with- and without-compiler curricula."""
    from learning.server.curriculum import load_curriculum

    curriculum = load_curriculum(learning_root, validator_names(), toolchain=toolchain)
    progress = ProgressStore(progress_path or (learning_root / ".progress.json"))
    context = ValidatorContext(repo_root=repo_root)
    return LearningServer(("127.0.0.1", port), curriculum, progress, context)


class LearningHandler(BaseHTTPRequestHandler):
    server: LearningServer  # narrow the type for mypy

    # -- plumbing ---------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:
        import sys

        print(f"[learning] {self.address_string()} {format % args}", file=sys.stderr)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message}, status)

    def _read_body(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 64 * 1024:
            return None
        try:
            data = json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _lesson_or_404(self, lesson_id: object) -> Lesson | None:
        if not isinstance(lesson_id, str):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "lesson_id must be a string")
            return None
        lesson = self.server.curriculum.lessons.get(lesson_id)
        if lesson is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, f"unknown lesson {lesson_id!r}")
            return None
        return lesson

    # -- static files ------------------------------------------------------

    def _serve_static(self, url_path: str) -> None:
        relative = unquote(url_path.lstrip("/"))
        if relative in ("", "static"):
            relative = "static/index.html"
        elif not relative.startswith("static/"):
            self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        candidate = (STATIC_ROOT / relative[len("static/") :]).resolve()
        static_root = STATIC_ROOT.resolve()
        if static_root != candidate and static_root not in candidate.parents:
            self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        if not candidate.is_file():
            self._send_error_json(HTTPStatus.NOT_FOUND, "not found")
            return
        content_type = CONTENT_TYPES.get(candidate.suffix, "application/octet-stream")
        body = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # -- GET routing --------------------------------------------------------

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/" or path.startswith("/static/"):
            self._serve_static(path)
        elif path == "/api/state":
            self._handle_state()
        elif path == "/api/curriculum":
            self._handle_curriculum()
        elif path.startswith("/api/lesson/"):
            self._handle_lesson(path[len("/api/lesson/") :])
        elif path == "/api/mastery":
            self._handle_mastery()
        elif path.startswith("/api/flow/"):
            self._handle_flow(path[len("/api/flow/") :])
        elif path == "/api/interview":
            self._handle_interview_next()
        else:
            self._send_error_json(HTTPStatus.NOT_FOUND, "not found")

    # -- POST routing -------------------------------------------------------

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        body = self._read_body()
        if body is None:
            self._send_error_json(HTTPStatus.BAD_REQUEST, "invalid JSON body")
            return
        if path == "/api/step":
            self._handle_step(body)
        elif path == "/api/hint":
            self._handle_hint(body)
        elif path == "/api/validate":
            self._handle_validate(body)
        elif path == "/api/complete":
            self._handle_complete(body)
        elif path == "/api/interview/answer":
            self._handle_interview_answer(body)
        else:
            self._send_error_json(HTTPStatus.NOT_FOUND, "not found")

    # -- GET handlers --------------------------------------------------------

    def _lesson_public(self, lesson: Lesson) -> dict[str, Any]:
        progress = self.server.progress
        snapshot = progress.snapshot()
        record = snapshot["lessons"].get(lesson.id, {})
        complete, missing = progress.lesson_completion(lesson, self.server.curriculum)
        return {
            "id": lesson.id,
            "day": lesson.day,
            "title": lesson.title,
            "objective": lesson.objective,
            "estimated_minutes": lesson.estimated_minutes,
            "status": lesson.status,
            "unavailable_reason": lesson.unavailable_reason,
            "concepts": list(lesson.concepts),
            "prerequisites": list(lesson.prerequisites),
            "source_files": list(lesson.source_files),
            "flow": lesson.flow,
            "blocks": list(lesson.blocks),
            "hint_count": len(lesson.hints),
            "requirements": list(lesson.requirements),
            "progress": {
                "status": record.get("status", "not_started"),
                "steps_done": record.get("steps_done", []),
                "validations": record.get("validations", {}),
                "quiz_correct": record.get("quiz_correct", []),
                "explain_done": record.get("explain_done", []),
                "hints_used": record.get("hints_used", 0),
                "complete": complete,
                "missing": missing,
            },
        }

    def _handle_state(self) -> None:
        curriculum = self.server.curriculum
        progress = self.server.progress
        resume_id = progress.resume_lesson_id(curriculum)
        snapshot = progress.snapshot()
        lessons_state = snapshot["lessons"]
        total_available = sum(
            1 for lesson in curriculum.lessons.values() if lesson.status == "available"
        )
        completed = sum(
            1
            for lesson_id in curriculum.ordered_lesson_ids
            if curriculum.lessons[lesson_id].status == "available"
            and lessons_state.get(lesson_id, {}).get("status") == "complete"
        )
        day_progress = []
        for day in curriculum.days:
            available = [
                lid
                for lid in day.lesson_ids
                if curriculum.lessons[lid].status == "available"
            ]
            done = [
                lid
                for lid in available
                if lessons_state.get(lid, {}).get("status") == "complete"
            ]
            day_progress.append(
                {
                    "day": day.day,
                    "title": day.title,
                    "summary": day.summary,
                    "total": len(available),
                    "done": len(done),
                }
            )
        self._send_json(
            {
                "resume_lesson_id": resume_id,
                "total_lessons": total_available,
                "completed_lessons": completed,
                "days": day_progress,
                "mastery": progress.concept_mastery(curriculum.concepts),
            }
        )

    def _handle_curriculum(self) -> None:
        curriculum = self.server.curriculum
        progress = self.server.progress
        snapshot = progress.snapshot()
        lessons_state = snapshot["lessons"]
        days = []
        for day in curriculum.days:
            lessons = []
            for lesson_id in day.lesson_ids:
                lesson = curriculum.lessons[lesson_id]
                record = lessons_state.get(lesson_id, {})
                prereqs_met = all(
                    lessons_state.get(p, {}).get("status") == "complete"
                    for p in lesson.prerequisites
                )
                lessons.append(
                    {
                        "id": lesson.id,
                        "title": lesson.title,
                        "estimated_minutes": lesson.estimated_minutes,
                        "status": lesson.status,
                        "unavailable_reason": lesson.unavailable_reason,
                        "progress": record.get("status", "not_started"),
                        "locked": lesson.status == "available" and not prereqs_met,
                    }
                )
            days.append(
                {
                    "day": day.day,
                    "title": day.title,
                    "summary": day.summary,
                    "lessons": lessons,
                }
            )
        self._send_json({"concepts": list(curriculum.concepts), "days": days})

    def _handle_lesson(self, lesson_id: str) -> None:
        lesson = self.server.curriculum.lessons.get(unquote(lesson_id))
        if lesson is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, f"unknown lesson {lesson_id!r}")
            return
        if lesson.status == "available":
            self.server.progress.mark_started(lesson.id)
        self._send_json(self._lesson_public(lesson))

    def _handle_mastery(self) -> None:
        curriculum = self.server.curriculum
        self._send_json(self.server.progress.concept_mastery(curriculum.concepts))

    def _handle_flow(self, name: str) -> None:
        flow = self.server.curriculum.flows.get(unquote(name))
        if flow is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, f"unknown flow {name!r}")
            return
        self._send_json(flow)

    def _handle_interview_next(self) -> None:
        curriculum = self.server.curriculum
        questions = {q.id: q.concepts for q in curriculum.interview}
        weights = self.server.progress.interview_weights(questions)
        population = list(weights)
        chosen = random.choices(
            population, weights=[weights[q] for q in population], k=1
        )[0]
        question = next(q for q in curriculum.interview if q.id == chosen)
        self._send_json({"id": question.id, "question": question.question})

    # -- POST handlers -------------------------------------------------------

    def _handle_step(self, body: dict[str, Any]) -> None:
        lesson = self._lesson_or_404(body.get("lesson_id"))
        if lesson is None:
            return
        block_id = body.get("block_id")
        kind = body.get("kind")
        if not isinstance(block_id, str) or not isinstance(kind, str):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "block_id and kind required")
            return
        progress = self.server.progress

        if kind in ("learn", "do"):
            progress.record_step(lesson.id, block_id)
            self._send_json({"recorded": True})
            return

        if kind in ("quiz", "predict"):
            block = lesson.quiz_block(block_id)
            if block is None or block["type"] != kind:
                self._send_error_json(HTTPStatus.NOT_FOUND, f"unknown {kind} block")
                return
            answer_index = body.get("answer_index")
            if not isinstance(answer_index, int):
                self._send_error_json(HTTPStatus.BAD_REQUEST, "answer_index required")
                return
            correct = answer_index == block["answer_index"]
            if kind == "quiz":
                progress.record_quiz(lesson.id, block_id, correct, lesson.concepts)
            else:
                progress.record_step(lesson.id, block_id)
            self._send_json(
                {
                    "correct": correct,
                    "explanation": block.get("reveal", block.get("explanation", "")),
                    "answer_index": block["answer_index"],
                }
            )
            return

        if kind == "explain":
            block = lesson.explain_block(block_id)
            if block is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "unknown explain block")
                return
            answer_text = body.get("answer_text")
            if not isinstance(answer_text, str) or not answer_text.strip():
                self._send_error_json(HTTPStatus.BAD_REQUEST, "answer_text required")
                return
            lowered = answer_text.lower()
            hits = [kw for kw in block["keywords"] if kw.lower() in lowered]
            passed = len(hits) >= 1
            progress.record_explain(lesson.id, block_id, passed, lesson.concepts)
            self._send_json(
                {
                    "passed": passed,
                    "matched_keywords": hits,
                    "sample_answer": block["sample_answer"],
                }
            )
            return

        self._send_error_json(HTTPStatus.BAD_REQUEST, f"unknown step kind {kind!r}")

    def _handle_hint(self, body: dict[str, Any]) -> None:
        lesson = self._lesson_or_404(body.get("lesson_id"))
        if lesson is None:
            return
        index = body.get("index")
        if not isinstance(index, int) or not 0 <= index < len(lesson.hints):
            self._send_error_json(HTTPStatus.BAD_REQUEST, "hint index out of range")
            return
        self.server.progress.record_hint(lesson.id)
        self._send_json(
            {
                "level": lesson.hints[index]["level"],
                "text": lesson.hints[index]["text"],
                "total": len(lesson.hints),
            }
        )

    def _handle_validate(self, body: dict[str, Any]) -> None:
        lesson = self._lesson_or_404(body.get("lesson_id"))
        if lesson is None:
            return
        if lesson.status != "available":
            self._send_error_json(HTTPStatus.CONFLICT, "lesson is unavailable")
            return
        block_id = body.get("block_id")
        verify_block = None
        for block in lesson.blocks:
            if block["type"] == "verify" and block["id"] == block_id:
                verify_block = block
                break
        if verify_block is None:
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                f"lesson {lesson.id!r} has no verify block {block_id!r}",
            )
            return
        args = verify_block.get("args", {})
        if not isinstance(args, dict):
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR, "bad validator args"
            )
            return
        result = run_validator(verify_block["validator"], args, self.server.context)
        self.server.progress.record_validation(
            lesson.id,
            str(block_id),
            result.passed,
            mandatory=bool(verify_block.get("mandatory", False)),
            concepts=lesson.concepts,
        )
        self._send_json(result.to_dict())

    def _handle_complete(self, body: dict[str, Any]) -> None:
        lesson = self._lesson_or_404(body.get("lesson_id"))
        if lesson is None:
            return
        complete, missing = self.server.progress.mark_complete(
            lesson, self.server.curriculum
        )
        if not complete:
            self._send_json(
                {"complete": False, "missing": missing}, HTTPStatus.CONFLICT
            )
            return
        curriculum = self.server.curriculum
        resume = self.server.progress.resume_lesson_id(curriculum)
        self._send_json({"complete": True, "next_lesson_id": resume})

    def _handle_interview_answer(self, body: dict[str, Any]) -> None:
        question_id = body.get("question_id")
        answer_text = body.get("answer_text")
        if not isinstance(answer_text, str) or not answer_text.strip():
            self._send_error_json(HTTPStatus.BAD_REQUEST, "answer_text required")
            return
        question = next(
            (q for q in self.server.curriculum.interview if q.id == question_id), None
        )
        if question is None:
            self._send_error_json(HTTPStatus.NOT_FOUND, "unknown question")
            return
        lowered = answer_text.lower()
        hits = [kw for kw in question.keywords if kw.lower() in lowered]
        correct = len(hits) >= 1
        self.server.progress.record_interview(question.id, correct, question.concepts)
        self._send_json(
            {
                "correct": correct,
                "matched_keywords": hits,
                "sample_answer": question.sample_answer,
            }
        )
