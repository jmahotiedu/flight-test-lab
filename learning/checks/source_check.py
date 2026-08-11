"""Check learner-edited source files with text/AST rules.

Used when a lesson asks the learner to modify real repository code and the
check needs to confirm the edit exists in the expected shape (e.g. a new
test carries a requirement marker).  Paths are always repo-relative and
verified to stay inside the repository.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from learning.server.validators import CheckResult, ValidatorContext


def _resolve_repo_path(repo_root: Path, relative: str) -> Path:
    candidate = (repo_root / relative).resolve()
    root = repo_root.resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError(f"path {relative!r} escapes the repository")
    return candidate


def _marked_functions(tree: ast.AST, requirement: str) -> list[str]:
    """Names of test functions carrying @pytest.mark.requirement(<requirement>).

    Only a real decorator counts.  Matching the raw text would accept the ID
    written in a comment, a docstring or an unrelated string constant, none of
    which links a test to anything.
    """

    def is_requirement_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        target = node.func
        # pytest.mark.requirement(...) — match the attribute chain ending in
        # `.mark.requirement`, however pytest was imported or aliased.
        if not (
            isinstance(target, ast.Attribute)
            and target.attr == "requirement"
            and isinstance(target.value, ast.Attribute)
            and target.value.attr == "mark"
        ):
            return False
        return any(
            isinstance(argument, ast.Constant) and argument.value == requirement
            for argument in node.args
        )

    marked: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        # Search the whole decorator subtree, not just the top-level
        # decorators: pytest's other idiom puts the mark on individual cases,
        # `pytest.param(..., marks=pytest.mark.requirement("REQ-..."))`, which
        # is what the Day 5 parametrize lesson teaches. Both forms genuinely
        # mark the test, so both have to count.
        for decorator in node.decorator_list:
            if any(is_requirement_call(inner) for inner in ast.walk(decorator)):
                marked.append(node.name)
                break
    return marked


def run(args: dict[str, Any], context: ValidatorContext) -> CheckResult:
    relative = args.get("file")
    if not isinstance(relative, str) or not relative:
        raise ValueError("source_check requires a repo-relative 'file'")
    target = _resolve_repo_path(context.repo_root, relative)

    failures: list[str] = []
    if not target.exists():
        failures.append(f"{relative} does not exist")
        text = ""
    else:
        text = target.read_text(encoding="utf-8", errors="replace")

    for pattern in args.get("must_contain", []):
        if not isinstance(pattern, str):
            continue
        if not re.search(pattern, text):
            failures.append(f"{relative} does not match /{pattern}/")
    for pattern in args.get("must_not_contain", []):
        if not isinstance(pattern, str):
            continue
        if re.search(pattern, text):
            failures.append(f"{relative} unexpectedly matches /{pattern}/")

    function_name = args.get("must_define_function")
    if isinstance(function_name, str) and text:
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            failures.append(f"{relative} does not parse as Python: {exc}")
        else:
            defined = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            if function_name not in defined:
                failures.append(
                    f"{relative} does not define a function named {function_name!r}"
                )

    marker_requirement = args.get("must_have_requirement_marker")
    if isinstance(marker_requirement, str) and text:
        # Parse the decorators rather than searching the text: the ID appearing
        # in a comment or a docstring is not a marker, and a check that accepts
        # it certifies a test with no requirement linkage at all.
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            failures.append(f"{relative} does not parse as Python: {exc}")
        else:
            if not _marked_functions(tree, marker_requirement):
                failures.append(
                    f"{relative} has no test function decorated with "
                    f"@pytest.mark.requirement({marker_requirement!r}) — the ID "
                    "appearing in a comment or string does not link anything"
                )

    passed = not failures
    if passed:
        interpretation = str(
            args.get("success_note", f"{relative} has the expected shape.")
        )
    else:
        interpretation = "Check failed: " + "; ".join(failures)

    return CheckResult(
        name="source_check",
        passed=passed,
        exit_status=0 if passed else 1,
        stdout="",
        stderr="",
        duration_ms=0,
        interpretation=interpretation,
    )
