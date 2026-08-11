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


def read_artifact(target: Path, relative: str) -> tuple[str, str]:
    """Read a checked artifact.  Returns (text, failure) — never raises.

    ``exists()`` is not ``is_file()``: a learner who creates the expected path
    as a directory, or a file the server cannot read, made the check fail.
    Letting the OSError escape would take the whole /api/validate request down
    without a response, leaving the UI on "Running…" forever — the one outcome
    worse than a red check, because it reports nothing at all.
    """
    if not target.exists():
        return "", f"{relative} does not exist"
    if not target.is_file():
        return "", f"{relative} is not a file (it is a directory)"
    try:
        return target.read_text(encoding="utf-8", errors="replace"), ""
    except OSError as exc:
        return "", f"{relative} could not be read: {exc.strerror or exc}"


def _decorator_names(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """Every attribute/name used anywhere in this function's decorators.

    The whole subtree, because pytest's other idiom nests marks inside
    ``pytest.param(..., marks=pytest.mark.requirement(...))``.
    """
    names: set[str] = set()
    for decorator in function.decorator_list:
        for node in ast.walk(decorator):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
    return names


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


def _creates_exclusively(node: ast.AST) -> bool:
    """Does this call create a file in a way that fails if it already exists?

    The forms that are genuinely atomic against a concurrent contender:

    * ``os.open(..., os.O_CREAT | os.O_EXCL)``
    * ``open(path, "x")`` / ``Path(...).open("x")`` — any mode containing "x"
    * ``Path(...).touch(exist_ok=False)``
    * ``Path(...).mkdir()`` / ``os.mkdir(...)`` without ``exist_ok=True``
    * ``os.link`` / ``os.symlink`` — the classic NFS-safe lock primitives
    """
    if not isinstance(node, ast.Call):
        return False
    target = node.func
    name = (
        target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
    )

    if name == "open":
        # os.open's flags are the 2nd argument; builtin open's mode is too.
        if any(
            isinstance(inner, ast.Attribute | ast.Name)
            and (getattr(inner, "attr", None) or getattr(inner, "id", None)) == "O_EXCL"
            for argument in node.args[1:]
            for inner in ast.walk(argument)
        ):
            return True
        modes = [node.args[1]] if len(node.args) > 1 else []
        modes += [kw.value for kw in node.keywords if kw.arg in ("mode", "flags")]
        return any(
            isinstance(mode, ast.Constant)
            and isinstance(mode.value, str)
            and "x" in mode.value
            for mode in modes
        )

    if name == "touch":
        return any(
            kw.arg == "exist_ok"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
            for kw in node.keywords
        )

    if name == "mkdir":
        # exist_ok defaults to False, so a bare mkdir is already exclusive;
        # only an explicit exist_ok=True gives the lock away.
        return not any(
            kw.arg == "exist_ok"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is True
            for kw in node.keywords
        )

    return name in ("link", "symlink")


def _function_named(tree: ast.AST, name: str) -> ast.AST | None:
    functions = (ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if isinstance(node, functions) and node.name == name:
            return node
    return None


def run(args: dict[str, Any], context: ValidatorContext) -> CheckResult:
    relative = args.get("file")
    if not isinstance(relative, str) or not relative:
        raise ValueError("source_check requires a repo-relative 'file'")
    target = _resolve_repo_path(context.repo_root, relative)

    failures: list[str] = []
    text, error = read_artifact(target, relative)
    if error:
        failures.append(error)

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

    exclusive_function = args.get("must_create_exclusively")
    if isinstance(exclusive_function, str) and text:
        # Searching the text for "O_EXCL" accepts it in a comment — the very
        # failure mode _marked_functions was written to avoid, repeated for a
        # property no sequential probe can observe. Parse the named function
        # and look for a call that actually creates the file exclusively.
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            failures.append(f"{relative} does not parse as Python: {exc}")
        else:
            function = _function_named(tree, exclusive_function)
            if function is None:
                failures.append(f"{relative} does not define {exclusive_function!r}")
            elif not any(_creates_exclusively(node) for node in ast.walk(function)):
                failures.append(
                    f"{relative}: {exclusive_function}() never creates the lock "
                    "file with an exclusive-create operation — os.open(..., "
                    "os.O_CREAT | os.O_EXCL), open(path, 'x'), "
                    "touch(exist_ok=False) or mkdir() all fail when the file "
                    "already exists, and a check-then-write does not. The "
                    "identifier appearing in a comment is not the operation"
                )

    # Every condition here must hold on ONE function. Checking "some function
    # is marked" and "some function is parametrized" separately lets a marked
    # test_unrelated satisfy the marker while the test the traceability row
    # and the JUnit gate actually match carries no requirement link at all.
    if "must_have_requirement_marker" in args:
        # Ignoring the old key would turn a gate into a no-op, which is the
        # failure mode this whole check exists to prevent.
        raise ValueError(
            "must_have_requirement_marker was replaced by must_mark_function, "
            "which binds the marker to the function the other gates match; "
            "accepting any marked function in the file certified nothing"
        )

    marker_spec = args.get("must_mark_function")
    if marker_spec is not None and text:
        if not isinstance(marker_spec, dict):
            raise ValueError(
                "must_mark_function takes an object: "
                "{'requirement': ..., 'name': <regex>, 'also_decorated_with': [...]}"
            )
        requirement = str(marker_spec["requirement"])
        name_pattern = str(marker_spec.get("name", "."))
        also = [str(item) for item in marker_spec.get("also_decorated_with", [])]
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            failures.append(f"{relative} does not parse as Python: {exc}")
        else:
            candidates = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                and node.name.startswith("test_")
                and re.search(name_pattern, node.name)
            ]
            marked = set(_marked_functions(tree, requirement))
            satisfied = [
                node
                for node in candidates
                if node.name in marked
                and all(mark in _decorator_names(node) for mark in also)
            ]
            if not satisfied:
                described = f"a test matching /{name_pattern}/"
                extra = f" and decorated with {', '.join(also)}" if also else ""
                if not candidates:
                    failures.append(f"{relative} defines no {described}")
                else:
                    failures.append(
                        f"{relative}: no single function is both {described} "
                        f"and marked @pytest.mark.requirement({requirement!r})"
                        f"{extra} — found {sorted(n.name for n in candidates)}, "
                        f"marked {sorted(marked) or 'nothing'}. Marking a "
                        "different function does not link this one"
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
