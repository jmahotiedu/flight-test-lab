"""Lessons make factual claims. These check the facts.

Prose is the part of the curriculum no validator runs, so it rots silently:
a lesson counted six tests in `tests/`, the suite grew to two hundred, and the
question kept its old answer. Another explained pytest teardown in terms of
the test's outcome, which is not how pytest behaves. Both read fine.

Each test here pins one such claim to something executable.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from learning.server.curriculum import load_curriculum
from learning.server.validators import validator_names

REPO_ROOT = Path(__file__).resolve().parents[2]
LEARNING_ROOT = REPO_ROOT / "learning"

FIXTURE_FILES = (
    "tests/test_cleanup.py",
    "tests/test_status.py",
    "tests/test_protocol.py",
)


def _fixture_users(path: Path) -> dict[str, int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    counts = {"tests": 0, "lab_client": 0, "dut": 0}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        counts["tests"] += 1
        for argument in node.args.args:
            if argument.arg in counts:
                counts[argument.arg] += 1
    return counts


def test_the_fixture_lesson_counts_match_the_repository() -> None:
    """Day 5 asks how many simulator processes a named command starts.

    The question used to be scoped to the whole `tests/` tree, which by now
    holds several suites that spawn DUTs of their own — so its premise and its
    marked answer were both false. It is scoped to these three files instead,
    and this is what keeps that scoping honest as they change.
    """
    curriculum = load_curriculum(LEARNING_ROOT, validator_names())
    lesson = curriculum.lessons["d5-fixtures-injection"]
    predict = next(b for b in lesson.blocks if b["id"] == "predict")

    for relative in FIXTURE_FILES:
        assert relative in predict["question"], (
            f"the question no longer names {relative}; its counts describe "
            "these files and nothing else"
        )

    totals = {"tests": 0, "lab_client": 0, "dut": 0}
    for relative in FIXTURE_FILES:
        for key, value in _fixture_users(REPO_ROOT / relative).items():
            totals[key] += value

    assert f"{totals['tests']} tests" in predict["question"]
    assert f"{totals['lab_client']} of which request" in predict["question"]
    assert predict["options"][predict["answer_index"]].startswith("1"), (
        "one session-scoped DUT is the whole point of the lesson"
    )


def test_only_one_dut_fixture_is_built_for_those_files() -> None:
    """The marked answer, measured rather than asserted.

    `--setup-show` names each fixture as it is built, so an exact count of
    `SETUP S dut` lines is the claim itself: one simulator, however many tests
    ask for it.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--setup-show",
            "-p",
            "no:cacheprovider",
            *FIXTURE_FILES,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout[-2000:]
    # "dut" exactly, not the dut_implementation fixture it depends on.
    builds = [
        line
        for line in completed.stdout.splitlines()
        if line.strip().startswith("SETUP") and " S dut (" in line
    ]
    assert len(builds) == 1, f"expected one session DUT, saw {len(builds)}: {builds}"


def test_no_interview_answer_teaches_posix_as_universal() -> None:
    """Graded answers must be true on the platform the learner is using.

    terminate() and kill() both call TerminateProcess on Windows — Day 6 says
    so in its own deeper note — so an answer of "there is no difference here"
    is correct, and was being marked wrong while the revealed answer taught
    the SIGTERM/SIGKILL distinction as if it were universal.
    """
    curriculum = load_curriculum(LEARNING_ROOT, validator_names())
    question = next(q for q in curriculum.interview if q.id == "iq-terminate-kill")

    windows_answer = "on Windows there is no difference: both call TerminateProcess"
    assert any(
        keyword.lower() in windows_answer.lower() for keyword in question.keywords
    )
    assert "TerminateProcess" in question.sample_answer
    assert "SIGTERM" in question.sample_answer, "the POSIX half is still taught"


def test_no_gate_before_day_11_can_demand_a_ported_command() -> None:
    """Day 2 adds `ping` to the Python DUT; Day 11 ports it to the C++ one.

    Between those lessons the two DUTs genuinely disagree, and on a checkout
    with a built C++ DUT any earlier gate that runs the whole suite fails —
    including Day 10's, which sits in front of the lesson that fixes it. That
    is a deadlock, so every full-suite gate before Day 11 has to exclude the
    port_parity marker.
    """
    curriculum = load_curriculum(LEARNING_ROOT, validator_names())
    offenders = []
    for lesson in curriculum.lessons.values():
        if lesson.day >= 11:
            continue
        for block in lesson.blocks:
            if block["type"] != "verify" or not block.get("mandatory"):
                continue
            args = block.get("args", {})
            snippet = str(args.get("snippet", ""))
            runs_everything = "'pytest'" in snippet and "-q" in snippet
            nodeids = [str(n) for n in args.get("nodeids", [])]
            runs_everything = runs_everything or any(
                n.rstrip("/") in ("tests", ".") for n in nodeids
            )
            if runs_everything and "not port_parity" not in snippet:
                offenders.append(f"{lesson.id}:{block['id']}")
    assert not offenders, (
        f"these gates run the full suite before Day 11 without excluding "
        f"port_parity, so they deadlock a learner with a built C++ DUT: "
        f"{offenders}"
    )


def test_the_parity_check_that_stages_around_the_course_is_marked() -> None:
    """The exclusion above is only meaningful if the marker is applied."""
    source = (REPO_ROOT / "tests" / "test_cpp_dut.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_course_added_commands_do_not_diverge"
    )
    marks = {
        node.attr
        for decorator in target.decorator_list
        for node in ast.walk(decorator)
        if isinstance(node, ast.Attribute)
    }
    assert "port_parity" in marks


TEARDOWN_PROBE = """
import pytest


@pytest.fixture()
def plain_cleanup():
    yield "resource"
    print("PLAIN_RAN")


@pytest.fixture()
def guarded_cleanup():
    try:
        yield "resource"
    finally:
        print("GUARDED_RAN")


@pytest.fixture()
def plain_ladder():
    yield "resource"
    raise RuntimeError("an earlier teardown step failed")


@pytest.fixture()
def guarded_ladder():
    try:
        yield "resource"
    finally:
        try:
            raise RuntimeError("an earlier teardown step failed")
        except RuntimeError:
            pass
        print("LADDER_GUARDED_RAN")


def test_fails(plain_cleanup, guarded_cleanup):
    assert False, "deliberate failure"


def test_ladder(plain_ladder, guarded_ladder):
    assert True
"""


def test_the_teardown_lesson_describes_pytest_as_it_behaves(tmp_path: Path) -> None:
    """Day 6 explains why the DUT teardown lives in a `finally`.

    It used to say a failing test could skip plain code after `yield`. It
    cannot: pytest resumes a yield fixture for teardown after every outcome.
    What `finally` actually protects is the teardown's own path — a step that
    raises before the cleanup. Both halves are measured here, because the
    lesson now asserts both.
    """
    (tmp_path / "test_teardown.py").write_text(TEARDOWN_PROBE, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-s", "-p", "no:cacheprovider", "-q"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    output = completed.stdout

    # The failing test still runs both teardowns — the outcome decides nothing.
    assert "PLAIN_RAN" in output, output[-1500:]
    assert "GUARDED_RAN" in output

    # A teardown step that raises skips what follows it; `finally` does not.
    assert "LADDER_GUARDED_RAN" in output

    curriculum = load_curriculum(LEARNING_ROOT, validator_names())
    lesson = curriculum.lessons["d6-broken-cleanup-sandbox"]
    explain = next(b for b in lesson.blocks if b["type"] == "explain")
    answer = explain["sample_answer"].lower()
    assert "resumes" in answer
    assert "never decides" in answer
    prose = json.dumps(
        [b for b in lesson.blocks if b["type"] == "learn"], ensure_ascii=False
    )
    assert "only runs if control returns" not in prose


def test_lesson_line_references_point_at_the_code_they_name() -> None:
    """A lesson that cites a line number is asserting something about a file.

    Day 6 sent learners to conftest.py lines 64-66 for the readiness call; the
    fixture had moved and those lines were the middle of dut_command's CMake
    hint. Nothing failed, because prose is the part of the curriculum no
    validator runs — so the reference is recomputed here instead.
    """
    curriculum = load_curriculum(LEARNING_ROOT, validator_names())
    lesson = curriculum.lessons["d6-readiness-polling"]
    instructions = next(b for b in lesson.blocks if b["type"] == "do")["instructions"]

    source = (REPO_ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    spans = {
        node.name: (node.lineno, node.end_lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    port_start, port_end = spans["reserve_local_port"]
    readiness = [
        number
        for number, line in enumerate(source.splitlines(), 1)
        if "wait_until_ready" in line
    ]
    assert len(readiness) == 1, f"ambiguous readiness call: lines {readiness}"

    assert f"lines {port_start}-{port_end}" in instructions, (
        f"reserve_local_port is at {port_start}-{port_end}; the lesson cites "
        "something else"
    )
    assert f"line {readiness[0]}" in instructions, (
        f"wait_until_ready is called on line {readiness[0]}; the lesson cites "
        "something else"
    )
