"""HTTP API tests: routing, safety, structured results, loopback binding."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from learning.server.app import LearningServer, build_server
from learning.server.toolchain import Toolchain

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def server(tmp_path: Path) -> Iterator[LearningServer]:
    instance = build_server(
        0, repo_root=REPO_ROOT, progress_path=tmp_path / "progress.json"
    )
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        yield instance
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def _request(
    server: LearningServer, method: str, path: str, body: dict | None = None
) -> tuple[int, dict]:
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(str(host), port, timeout=30)
    payload = json.dumps(body) if body is not None else None
    connection.request(
        method,
        path,
        body=payload,
        headers={"Content-Type": "application/json"} if body else {},
    )
    response = connection.getresponse()
    data = json.loads(response.read())
    connection.close()
    return response.status, data


def test_server_binds_loopback_only(server: LearningServer) -> None:
    assert server.server_address[0] == "127.0.0.1"


def test_index_and_static_served(server: LearningServer) -> None:
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(str(host), port, timeout=10)
    connection.request("GET", "/")
    response = connection.getresponse()
    body = response.read().decode()
    assert response.status == 200
    assert "flight-test-lab" in body
    connection.close()


def test_static_path_traversal_blocked(server: LearningServer) -> None:
    host, port = server.server_address[:2]
    for attempt in (
        "/static/../server/app.py",
        "/static/..%2f..%2fpyproject.toml",
        "/static/%2e%2e/%2e%2e/pyproject.toml",
    ):
        connection = http.client.HTTPConnection(str(host), port, timeout=10)
        connection.request("GET", attempt)
        response = connection.getresponse()
        response.read()
        assert response.status == 404, attempt
        connection.close()


def test_state_returns_resume_and_progress(server: LearningServer) -> None:
    status, data = _request(server, "GET", "/api/state")
    assert status == 200
    assert data["resume_lesson_id"] == "d1-import-no-side-effects"
    assert data["total_lessons"] > 30
    assert len(data["days"]) == 14


def test_validate_rejects_unknown_lesson(server: LearningServer) -> None:
    status, data = _request(
        server, "POST", "/api/validate", {"lesson_id": "nope", "block_id": "v"}
    )
    assert status == 404
    assert "error" in data


def test_validate_rejects_non_verify_block(server: LearningServer) -> None:
    """There is no way to run anything but the lesson's own verify blocks."""
    status, data = _request(
        server,
        "POST",
        "/api/validate",
        {"lesson_id": "d1-import-no-side-effects", "block_id": "learn"},
    )
    assert status == 404
    assert "error" in data


def test_no_arbitrary_command_endpoint(server: LearningServer) -> None:
    for path in ("/api/run-command", "/api/exec", "/api/run"):
        status, data = _request(server, "POST", path, {"command": "echo pwned"})
        assert status == 404, path


def test_validate_returns_structured_pass(server: LearningServer) -> None:
    status, data = _request(
        server,
        "POST",
        "/api/validate",
        {"lesson_id": "d1-import-no-side-effects", "block_id": "verify"},
    )
    assert status == 200
    for key in (
        "name",
        "passed",
        "exit_status",
        "stdout",
        "stderr",
        "duration_ms",
        "interpretation",
    ):
        assert key in data, key
    assert data["passed"] is True
    assert "simulator.simulator" in data["stdout"]


def test_validate_returns_structured_failure(server: LearningServer) -> None:
    """d8-config-driven-faults fails until the learner creates the config file —
    the failure must be truthful and structured, not an exception."""
    status, data = _request(
        server,
        "POST",
        "/api/validate",
        {"lesson_id": "d8-config-driven-faults", "block_id": "verify"},
    )
    assert status == 200
    assert data["passed"] is False
    assert data["interpretation"].startswith("Check failed")


def test_complete_refused_without_validation(server: LearningServer) -> None:
    _request(server, "GET", "/api/lesson/d1-main-entrypoint")
    status, data = _request(
        server, "POST", "/api/complete", {"lesson_id": "d1-main-entrypoint"}
    )
    assert status == 409
    assert data["complete"] is False
    assert any("validation" in item for item in data["missing"])


def test_hint_endpoint_is_progressive(server: LearningServer) -> None:
    _request(server, "GET", "/api/lesson/d1-import-no-side-effects")
    status, first = _request(
        server,
        "POST",
        "/api/hint",
        {"lesson_id": "d1-import-no-side-effects", "index": 0},
    )
    assert status == 200
    assert first["level"] == 1
    status, out_of_range = _request(
        server,
        "POST",
        "/api/hint",
        {"lesson_id": "d1-import-no-side-effects", "index": 99},
    )
    assert status == 400


def test_quiz_step_scored_server_side(server: LearningServer) -> None:
    _request(server, "GET", "/api/lesson/d1-argparse-logging")
    status, wrong = _request(
        server,
        "POST",
        "/api/step",
        {
            "lesson_id": "d1-argparse-logging",
            "block_id": "quiz",
            "kind": "quiz",
            "answer_index": 0,
        },
    )
    assert status == 200
    assert wrong["correct"] is False
    status, right = _request(
        server,
        "POST",
        "/api/step",
        {
            "lesson_id": "d1-argparse-logging",
            "block_id": "quiz",
            "kind": "quiz",
            "answer_index": 1,
        },
    )
    assert right["correct"] is True


def test_interview_flow(server: LearningServer) -> None:
    status, question = _request(server, "GET", "/api/interview")
    assert status == 200
    assert question["id"].startswith("iq-")
    status, result = _request(
        server,
        "POST",
        "/api/interview/answer",
        {"question_id": question["id"], "answer_text": "I do not know"},
    )
    assert status == 200
    assert "correct" in result
    assert "sample_answer" in result


def test_unavailable_lesson_cannot_validate(tmp_path: Path) -> None:
    """A lesson withheld for a missing toolchain must refuse to run its checks.

    The server is built with an empty Toolchain so this holds regardless of
    what is installed on the machine running the suite.
    """
    instance = build_server(
        0,
        repo_root=REPO_ROOT,
        progress_path=tmp_path / "progress.json",
        toolchain=Toolchain(),
    )
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    try:
        lesson_status, lesson = _request(instance, "GET", "/api/lesson/d11-cpp-dut")
        assert lesson_status == 200
        assert lesson["status"] == "unavailable"

        status, data = _request(
            instance,
            "POST",
            "/api/validate",
            {"lesson_id": "d11-cpp-dut", "block_id": "verify-build"},
        )
        assert status == 409
        assert "unavailable" in data["error"]
    finally:
        instance.shutdown()
        instance.server_close()
        thread.join(timeout=5)


def test_a_crashing_validator_answers_instead_of_hanging(
    server: LearningServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A validator that raises must still produce a response.

    Without this the exception unwinds out of do_POST, the connection closes
    with no body, and the page sits on "Running…" forever — the one outcome
    worse than a red check, because it reports nothing at all.
    """
    import learning.server.app as app_module

    def explode(*_args: object, **_kwargs: object) -> None:
        raise IsADirectoryError(21, "Is a directory")

    monkeypatch.setattr(app_module, "run_validator", explode)

    status, data = _request(
        server,
        "POST",
        "/api/validate",
        {"lesson_id": "d1-import-no-side-effects", "block_id": "verify"},
    )

    assert status == 200
    assert data["passed"] is False
    assert "could not run" in data["interpretation"]
    assert "IsADirectoryError" in data["interpretation"]


def test_a_crashing_validator_does_not_mark_the_learner_weak(
    server: LearningServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug in a check is not evidence about the learner.

    Crediting the crash as a missed concept would mark someone weak on a topic
    because of a defect in this codebase.
    """
    import learning.server.app as app_module

    before = server.progress.concept_mastery(server.curriculum.concepts)
    monkeypatch.setattr(
        app_module,
        "run_validator",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("check is broken")),
    )
    _request(
        server,
        "POST",
        "/api/validate",
        {"lesson_id": "d1-import-no-side-effects", "block_id": "verify"},
    )
    after = server.progress.concept_mastery(server.curriculum.concepts)
    assert after == before
