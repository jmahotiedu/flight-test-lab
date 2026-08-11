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
