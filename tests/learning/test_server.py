"""HTTP API tests: routing, safety, structured results, loopback binding."""

from __future__ import annotations

import http.client
import json
import threading
import time
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
    # Entering a lesson is a POST now: the GET is read-only, so it no longer
    # creates the record this assertion reads.
    _request(server, "POST", "/api/start", {"lesson_id": "d1-main-entrypoint"})
    status, data = _request(
        server, "POST", "/api/complete", {"lesson_id": "d1-main-entrypoint"}
    )
    assert status == 409
    assert data["complete"] is False
    assert any("validation" in item for item in data["missing"])


def test_reading_a_lesson_does_not_touch_progress(server: LearningServer) -> None:
    """A GET with no unusual header is a request any page can make.

    It cannot read the reply cross-origin, but a GET that writes does not need
    to be read to do damage: this one used to bump attempts, set status and
    rewrite the progress file, so a page in another tab could scribble over a
    learner's record.
    """
    for _ in range(3):
        status, _ = _request(server, "GET", "/api/lesson/d1-import-no-side-effects")
        assert status == 200

    assert server.progress.snapshot()["lessons"] == {}

    started, _ = _request(
        server, "POST", "/api/start", {"lesson_id": "d1-import-no-side-effects"}
    )
    assert started == 200
    record = server.progress.snapshot()["lessons"]["d1-import-no-side-effects"]
    assert record["status"] == "in_progress"
    assert record["attempts"] == 1


def test_starting_a_lesson_is_same_origin_protected(server: LearningServer) -> None:
    status, data = _raw_post(
        server,
        {"Content-Type": "text/plain", "Origin": "http://evil.example"},
        path="/api/start",
    )
    assert status == 403
    assert server.progress.snapshot()["lessons"] == {}
    assert "application/json" in data["error"]


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


def _raw_post(
    server: LearningServer, headers: dict[str, str], path: str = "/api/step"
) -> tuple[int, dict]:
    host, port = server.server_address[:2]
    body = json.dumps(
        {"lesson_id": "d1-import-no-side-effects", "block_id": "learn", "kind": "learn"}
    )
    connection = http.client.HTTPConnection(str(host), port, timeout=30)
    connection.request("POST", path, body=body, headers=headers)
    response = connection.getresponse()
    data = json.loads(response.read())
    connection.close()
    return response.status, data


@pytest.mark.parametrize(
    ("label", "headers"),
    [
        # A "simple request": no preflight, so a page on any origin can send
        # it and the browser will not stop it.
        ("text/plain from a foreign page", {"Content-Type": "text/plain"}),
        ("form encoding", {"Content-Type": "application/x-www-form-urlencoded"}),
        ("no content type at all", {}),
    ],
)
def test_a_cross_origin_simple_post_is_refused(
    server: LearningServer, label: str, headers: dict[str, str]
) -> None:
    """Loopback binding keeps other machines out, not other pages.

    /api/validate starts pytest, CMake, a DUT or gdb, so accepting a POST that
    any website can make is a page on the internet spawning processes on this
    machine. Requiring application/json means a cross-origin attempt needs a
    preflight, which this server never answers.
    """
    status, data = _raw_post(server, {**headers, "Origin": "http://evil.example"})
    assert status == 403
    assert "application/json" in data["error"]


def test_a_foreign_origin_is_refused_even_with_json(server: LearningServer) -> None:
    status, data = _raw_post(
        server,
        {"Content-Type": "application/json", "Origin": "http://evil.example"},
    )
    assert status == 403
    assert "cross-origin" in data["error"]


def test_a_foreign_host_header_is_refused(server: LearningServer) -> None:
    """DNS rebinding: the document's origin stays evil.example while the name
    resolves to 127.0.0.1, so Origin looks consistent and Host does not."""
    status, data = _raw_post(
        server, {"Content-Type": "application/json", "Host": "evil.example"}
    )
    assert status == 403
    assert "loopback" in data["error"]


def test_a_cross_site_fetch_marker_is_refused(server: LearningServer) -> None:
    status, data = _raw_post(
        server,
        {"Content-Type": "application/json", "Sec-Fetch-Site": "cross-site"},
    )
    assert status == 403
    assert "cross-site" in data["error"]


def test_the_app_and_non_browser_clients_still_work(server: LearningServer) -> None:
    """The guard must not lock out the UI, curl, or this suite.

    Nothing here is authentication — a non-browser client sends no Origin and
    is unaffected. The point is that a *browser* cannot be used as the
    confused deputy.
    """
    host, port = server.server_address[:2]
    same_origin, _ = _raw_post(
        server,
        {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{port}",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert same_origin == 200
    no_origin, _ = _raw_post(server, {"Content-Type": "application/json"})
    assert no_origin == 200


def test_an_undecodable_post_body_gets_a_400(server: LearningServer) -> None:
    """CPython's digit cap raises a plain ValueError, not a JSONDecodeError.

    It escaped the request thread, so the client got a closed connection
    instead of the documented 400 and the page waited forever.
    """
    host, port = server.server_address[:2]
    connection = http.client.HTTPConnection(str(host), port, timeout=30)
    connection.request(
        "POST",
        "/api/step",
        body='{"lesson_id": ' + "1" * 5000 + "}",
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    data = json.loads(response.read())
    connection.close()

    assert response.status == 400
    assert "invalid JSON body" in data["error"]


def test_validators_do_not_run_concurrently(
    server: LearningServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validators share one checkout, so they have to take turns.

    Day 11's verify-configure writes cpp/build while verify-build reads and
    writes the same tree; two tabs or a double-click would have them racing and
    reporting failures that are artefacts of the collision. Disabling a DOM
    button cannot fix that — it is not the same document.
    """
    import learning.server.app as app_module
    from learning.server.validators import CheckResult

    intervals: list[tuple[float, float]] = []
    guard = threading.Lock()

    def slow_validator(name: str, args: dict, context: object) -> CheckResult:
        started = time.monotonic()
        time.sleep(0.25)
        with guard:
            intervals.append((started, time.monotonic()))
        return CheckResult(
            name=name,
            passed=True,
            exit_status=0,
            stdout="",
            stderr="",
            duration_ms=250,
            interpretation="ok",
        )

    monkeypatch.setattr(app_module, "run_validator", slow_validator)

    def hit() -> None:
        _request(
            server,
            "POST",
            "/api/validate",
            {"lesson_id": "d1-import-no-side-effects", "block_id": "verify"},
        )

    threads = [threading.Thread(target=hit) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(intervals) == 4
    intervals.sort()
    for earlier, later in zip(intervals, intervals[1:], strict=False):
        assert earlier[1] <= later[0] + 0.01, f"validators overlapped: {intervals}"


def test_queued_validators_refuse_once_shutdown_begins(
    server: LearningServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A handler waiting on the lock must not start work after the sweep.

    terminate_active_validators() kills the children registered when it runs.
    A queued handler waking afterwards starts a fresh pytest, CMake or DUT in
    its own process group — one that outlives the server that spawned it.
    """
    import learning.server.app as app_module
    from learning.server.validators import CheckResult

    started = threading.Event()
    release = threading.Event()
    runs = 0

    def blocking_validator(name: str, args: dict, context: object) -> CheckResult:
        nonlocal runs
        runs += 1
        started.set()
        release.wait(timeout=30)
        return CheckResult(
            name=name,
            passed=True,
            exit_status=0,
            stdout="",
            stderr="",
            duration_ms=0,
            interpretation="ok",
        )

    monkeypatch.setattr(app_module, "run_validator", blocking_validator)

    statuses: list[int] = []

    def hit() -> None:
        status, _ = _request(
            server,
            "POST",
            "/api/validate",
            {"lesson_id": "d1-import-no-side-effects", "block_id": "verify"},
        )
        statuses.append(status)

    first = threading.Thread(target=hit)
    first.start()
    assert started.wait(timeout=30), "the first validator never started"

    queued = threading.Thread(target=hit)
    queued.start()
    time.sleep(0.2)  # let it reach the lock

    server.begin_shutdown()
    release.set()
    first.join(timeout=30)
    queued.join(timeout=30)

    assert runs == 1, "the queued handler started a subprocess during shutdown"
    assert sorted(statuses) == [200, 503]
