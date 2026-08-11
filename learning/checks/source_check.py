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


def _is_module_reference(node: ast.AST) -> bool:
    """True for `os` in `os.open(...)` — a module, not a path."""
    return isinstance(node, ast.Name) and node.id in ("os", "pathlib", "shutil")


def _names_lock_path(node: ast.AST | None, lock_paths: set[str]) -> bool:
    """Does this expression name the lock file itself?

    ``self.path`` and any local bound to it count; ``self.path.parent`` does
    not, and neither does an unrelated attribute.
    """
    if isinstance(node, ast.Attribute):
        return node.attr in lock_paths and isinstance(node.value, ast.Name)
    if isinstance(node, ast.Name):
        return node.id in lock_paths
    return False


def _lock_path_names(tree: ast.AST, function: ast.AST) -> set[str]:
    """Every name that refers to the lock file.

    Taken from ``__init__``: whatever attribute is assigned from a parameter
    whose name mentions "path" is the lock, plus any local inside the checked
    function bound to that attribute.  With nothing to go on the set stays
    empty and the caller falls back to accepting any target rather than
    rejecting a correct implementation for being differently written.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "__init__":
            continue
        parameters = {
            argument.arg
            for argument in node.args.args
            if "path" in argument.arg.lower()
        }
        if not parameters:
            continue
        names |= parameters
        for statement in ast.walk(node):
            if not isinstance(statement, ast.Assign):
                continue
            if not any(
                isinstance(inner, ast.Name) and inner.id in parameters
                for inner in ast.walk(statement.value)
            ):
                continue
            for assigned in statement.targets:
                if isinstance(assigned, ast.Attribute):
                    names.add(assigned.attr)
                elif isinstance(assigned, ast.Name):
                    names.add(assigned.id)
    # Locals inside the function that alias one of those, e.g. `p = self.path`.
    for statement in ast.walk(function):
        if not isinstance(statement, ast.Assign):
            continue
        if not _names_lock_path(statement.value, names):
            continue
        for assigned in statement.targets:
            if isinstance(assigned, ast.Name):
                names.add(assigned.id)
    return names


def _creates_exclusively(node: ast.AST, lock_paths: set[str]) -> bool:
    """Does this call create *the lock* in a way that fails if it exists?

    The forms that are genuinely atomic against a concurrent contender:

    * ``os.open(..., os.O_CREAT | os.O_EXCL)``
    * ``open(path, "x")`` / ``Path(...).open("x")`` — any mode containing "x"
    * ``Path(...).touch(exist_ok=False)``
    * ``Path(...).mkdir()`` / ``os.mkdir(...)`` without ``exist_ok=True``
    * ``os.link`` / ``os.symlink`` — the classic NFS-safe lock primitives

    ``lock_paths`` names the expressions that are the lock file, so an
    exclusive call aimed somewhere else does not count.  A ``mkdir()`` on
    ``self.path.parent`` — creating the directory the lock lives in, which is
    ordinary and harmless — otherwise read as proof that the lock itself was
    created atomically, and a read-then-write ``acquire()`` passed the gate.
    """
    if not isinstance(node, ast.Call):
        return False
    target = node.func
    name = (
        target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
    )
    # For a method call the receiver is the path (self.path.touch()); for a
    # function call it is the first argument (os.open(self.path, ...)).
    receiver = target.value if isinstance(target, ast.Attribute) else None
    subject = receiver if name in ("touch", "mkdir", "open") and receiver else None
    if subject is None or _is_module_reference(subject):
        subject = node.args[0] if node.args else None
    if not _names_lock_path(subject, lock_paths):
        return False

    if name == "open":
        # os.open's flags and builtin open's mode can each be positional or
        # keyword. Both spellings are searched for both properties: looking
        # for O_EXCL in positional arguments only rejected
        # `os.open(path, flags=os.O_CREAT | os.O_EXCL)`, which is ordinary
        # code and passes every behavioural probe.
        candidates = list(node.args[1:])
        candidates += [kw.value for kw in node.keywords if kw.arg in ("mode", "flags")]
        if any(
            isinstance(inner, ast.Attribute | ast.Name)
            and (getattr(inner, "attr", None) or getattr(inner, "id", None)) == "O_EXCL"
            for argument in candidates
            for inner in ast.walk(argument)
        ):
            return True
        return any(
            isinstance(mode, ast.Constant)
            and isinstance(mode.value, str)
            and "x" in mode.value
            for mode in candidates
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


def _mentions_lock(node: ast.AST) -> bool:
    """Does this `with` header name something lock-shaped?

    Deliberately a name test rather than type inference: the value is created
    at module scope by `threading.Lock()` and there is no way to follow that
    statically without a type checker.  A learner who calls their lock
    something else gets a message saying exactly what the check looked for.
    """
    for inner in ast.walk(node):
        text = getattr(inner, "attr", None) or getattr(inner, "id", None)
        if isinstance(text, str) and "lock" in text.lower():
            return True
    return False


def _unguarded_uses(node: ast.AST, target: str, guarded: bool) -> list[int]:
    """Lines where `target` is referenced outside a lock-holding `with`.

    Reads count, not just writes: a `current = _counter` outside the lock and
    a `_counter = current + 1` inside it is still the lost update, and is the
    shape the lesson walks the learner through.
    """
    if isinstance(node, ast.With | ast.AsyncWith):
        holds = guarded or any(_mentions_lock(item.context_expr) for item in node.items)
        offenders: list[int] = []
        for item in node.items:
            offenders += _unguarded_uses(item.context_expr, target, guarded)
        for child in node.body:
            offenders += _unguarded_uses(child, target, holds)
        return offenders
    if isinstance(node, ast.Name) and node.id == target:
        return [] if guarded else [node.lineno]
    found: list[int] = []
    for descendant in ast.iter_child_nodes(node):
        found += _unguarded_uses(descendant, target, guarded)
    return found


def _branch_on_constant(tree: ast.AST, value: str) -> ast.If | None:
    """The `if` whose test compares something to `value`."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        for inner in ast.walk(node.test):
            if isinstance(inner, ast.Constant) and inner.value == value:
                return node
    return None


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
            elif not any(
                _creates_exclusively(node, _lock_path_names(tree, function))
                for node in ast.walk(function)
            ):
                failures.append(
                    f"{relative}: {exclusive_function}() never creates the lock "
                    "file with an exclusive-create operation — os.open(..., "
                    "os.O_CREAT | os.O_EXCL), open(path, 'x'), "
                    "touch(exist_ok=False) or mkdir() all fail when the file "
                    "already exists, and a check-then-write does not. The "
                    "identifier appearing in a comment is not the operation"
                )

    lock_spec = args.get("must_hold_lock")
    if lock_spec is not None and text:
        if not isinstance(lock_spec, dict):
            raise ValueError(
                "must_hold_lock takes an object: "
                "{'branch': <string the if compares to>, 'target': <name>}"
            )
        branch_value = str(lock_spec["branch"])
        guarded_name = str(lock_spec["target"])
        try:
            tree = ast.parse(text)
        except SyntaxError as exc:
            failures.append(f"{relative} does not parse as Python: {exc}")
        else:
            branch = _branch_on_constant(tree, branch_value)
            if branch is None:
                failures.append(
                    f"{relative} has no branch on {branch_value!r} — the check "
                    "cannot inspect an implementation it cannot find"
                )
            elif not any(
                isinstance(node, ast.Name) and node.id == guarded_name
                for node in ast.walk(branch)
            ):
                failures.append(
                    f"{relative}: the {branch_value!r} branch never touches "
                    f"{guarded_name!r}"
                )
            else:
                # A behavioural probe cannot carry this: CPython's read and
                # store usually complete inside one scheduling quantum, so an
                # unlocked counter prints the right total nearly every run and
                # the gate would certify the exact bug the lesson is about.
                offenders = _unguarded_uses(branch, guarded_name, guarded=False)
                if offenders:
                    failures.append(
                        f"{relative}: {guarded_name} is read or written outside "
                        f"a lock at line(s) {', '.join(map(str, offenders))} — "
                        "wrap the whole read-modify-write in `with "
                        "<something>_lock:`. The GIL makes an unlocked counter "
                        "produce the right answer almost every run; that is "
                        "why this is checked in the source and not by counting"
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
