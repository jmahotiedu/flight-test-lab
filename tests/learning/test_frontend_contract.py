"""Frontend contract tests: the JS client must only call routes the server
implements — without introducing a JS toolchain."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_JS = REPO_ROOT / "learning" / "static" / "app.js"
SERVER_PY = REPO_ROOT / "learning" / "server" / "app.py"


def _server_routes() -> set[str]:
    source = SERVER_PY.read_text(encoding="utf-8")
    routes = set(re.findall(r'["\'](/api/[a-z/_-]+)["\']', source))
    # Dynamic prefixes handled with startswith in the server.
    routes.update(re.findall(r'startswith\(["\'](/api/[a-z/_-]+/)["\']\)', source))
    return routes


def _client_calls() -> set[str]:
    source = APP_JS.read_text(encoding="utf-8")
    calls = set(re.findall(r'["\'](/api/[a-z/_-]+)["\']', source))
    calls.update(re.findall(r'["\'](/api/[a-z/_-]+/)["\']', source))
    return calls


def test_client_api_calls_match_server_routes() -> None:
    routes = _server_routes()
    assert routes, "no routes found in app.py — test is broken"
    for call in _client_calls():
        assert call in routes, f"app.js calls {call} but app.py has no such route"


def test_client_has_no_generic_command_execution() -> None:
    source = APP_JS.read_text(encoding="utf-8")
    forbidden = ("run-command", "/api/exec", "eval(", "new Function")
    for token in forbidden:
        assert token not in source, f"app.js contains forbidden pattern {token!r}"


def _fence_rules() -> list[tuple[str, str]]:
    """The regex rules app.js applies to a fenced code chunk, in order."""
    source = APP_JS.read_text(encoding="utf-8")
    line = next((ln for ln in source.splitlines() if "const code = chunk" in ln), None)
    assert line, "app.js no longer builds code blocks the expected way"
    rules = re.findall(r'\.replace\(/(.+?)/[gimsuy]*,\s*"([^"]*)"\)', line)
    assert rules, f"could not parse fence rules from: {line.strip()}"
    return rules


def _render_fence(chunk: str) -> str:
    for pattern, replacement in _fence_rules():
        chunk = re.sub(pattern, replacement, chunk)
    return chunk


def test_code_fences_keep_the_command_the_learner_must_run() -> None:
    """A fenced chunk may open with the command itself (```python -m ...).

    A language-tag strip that is not newline-anchored silently deletes that
    first word, handing the learner a broken command.  Run the real app.js
    rules over the real curriculum and require the first line to survive.
    """
    from learning.server.curriculum import load_curriculum
    from learning.server.validators import validator_names

    curriculum = load_curriculum(REPO_ROOT / "learning", validator_names())
    texts: list[tuple[str, str]] = []
    for lesson in curriculum.lessons.values():
        for block in lesson.blocks:
            for key in ("text", "more", "instructions", "reveal", "explanation"):
                value = block.get(key)
                if isinstance(value, str):
                    texts.append((f"{lesson.id}/{block['id']}/{key}", value))
        for hint in lesson.hints:
            texts.append((f"{lesson.id}/hint{hint['level']}", str(hint["text"])))

    checked = 0
    for where, text in texts:
        for chunk in text.split("\n\n"):
            if not chunk.startswith("```"):
                continue
            fence_line = chunk[3:].split("\n", 1)[0]
            if not fence_line.strip() or " " not in fence_line:
                continue  # a bare language tag, correctly strippable
            checked += 1
            rendered = _render_fence(chunk)
            assert rendered.split("\n", 1)[0] == fence_line, (
                f"{where}: rendering dropped part of the first code line\n"
                f"  content:  {fence_line!r}\n"
                f"  rendered: {rendered.split(chr(10), 1)[0]!r}"
            )
    assert checked > 20, (
        f"only {checked} command fences checked — test is not exercising the curriculum"
    )
