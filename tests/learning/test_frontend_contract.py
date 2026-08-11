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


def test_mdinline_output_is_always_interpreted_as_markup() -> None:
    """mdInline() returns HTML, so it must reach the DOM through `html:`.

    el() appends a string child as a text node, so passing the markup
    positionally shows the tags: Day 10's choices read literally as
    "<code>ruff format</code>" and "&lt;failure&gt;". There is no JS runtime
    here, so the rule is enforced structurally — every call site, not just the
    one that was wrong.
    """
    source = APP_JS.read_text(encoding="utf-8")
    lines = source.splitlines()
    offenders = []
    for match in re.finditer(r"mdInline\(", source):
        number = source[: match.start()].count("\n") + 1
        if lines[number - 1].lstrip().startswith("//"):
            continue  # a comment mentioning it
        prefix = source[: match.start()].rstrip()
        if prefix.endswith("function") or prefix.endswith("html:"):
            continue  # the definition itself, or a correct call site
        offenders.append(f"line {number}: {lines[number - 1].strip()[:70]}")
    assert not offenders, (
        "mdInline() output must reach the DOM through html:, or el() renders "
        f"it as literal text — {offenders}"
    )


def test_a_quiz_answered_earlier_is_disabled_on_reload() -> None:
    """Restoring the highlight is not restoring the state.

    The live success path disables every option; the reload path used to
    restore only the green highlight, so a stray click on a finished quiz
    could show a failure and re-post the answer, moving concept mastery for a
    question already answered correctly.
    """
    source = APP_JS.read_text(encoding="utf-8")
    branch = re.search(r"if \(alreadyCorrect\) \{(.*?)\n    \}", source, re.DOTALL)
    assert branch, "no alreadyCorrect restore branch found"
    assert "disabled = true" in branch.group(1), (
        "the restore branch does not disable the options"
    )


def test_state_changing_clicks_disable_before_they_post() -> None:
    """A handler that awaits before disabling can be clicked twice.

    The server counts both: record_quiz bumps quiz_attempts and concept
    mastery per request, and the Day 14 drill counts raw interview answers, so
    one choice submitted five times satisfied a five-answer gate. There is no
    JS runtime here, so the rule is enforced structurally: every click handler
    that posts must go through guardedClick, hold an explicit in-flight flag,
    or disable its control before the await.
    """
    source = APP_JS.read_text(encoding="utf-8")
    unguarded = []
    for match in re.finditer(r"addEventListener\(\"click\", async \(\) => \{", source):
        # The handler body, up to the await that changes server state.
        tail = source[match.end() :]
        post = tail.find("await api.post")
        if post < 0:
            continue
        before = tail[:post]
        guarded = (
            "InFlight" in before
            or "disabled = true" in before
            or "disabled) return" in before
        )
        if not guarded:
            line = source[: match.start()].count("\n") + 1
            unguarded.append(f"line {line}")
    assert not unguarded, (
        "these click handlers post before disabling anything, so a "
        f"double-click posts twice: {unguarded}"
    )


def test_continue_checks_the_navigation_it_started_from() -> None:
    """loadLesson's own token cannot protect a delayed completion.

    A stale Continue handler calls loadLesson, which *creates* the newest
    token — so it would navigate over the lesson the learner picked from the
    roadmap while the POST was in flight. The check has to happen in the
    handler, against the navigation that was current when it was clicked.
    """
    source = APP_JS.read_text(encoding="utf-8")
    handler = re.search(
        r"guardedClick\(continueBtn, async \(\) => \{(.*?)\n  \}\);", source, re.DOTALL
    )
    assert handler, "the Continue handler is no longer shaped as expected"
    body = handler.group(1)
    captured = body.index("state.navigation")
    posted = body.index("await api.post")
    assert captured < posted, "the navigation token is captured after the request"
    assert "!== state.navigation" in body, "the stale response is never discarded"


def test_handlers_that_finish_a_block_keep_their_button_disabled() -> None:
    """guardedClick re-enables anything that does not say otherwise.

    The explain handler set `disabled = true` itself and returned nothing, so
    the guard's `if (!keepDisabled)` turned it straight back on — and
    record_explain credits concept mastery on every request, so a finished
    answer could be resubmitted from a disabled textarea, once per click.
    """
    source = APP_JS.read_text(encoding="utf-8")
    handler = re.search(r'kind: "explain".*?\n    \}\);', source, re.DOTALL)
    assert handler, "the explain handler is no longer shaped as expected"
    body = handler.group(0)
    success = body[body.index("if (data.passed)") : body.index("} else {")]
    assert "return true" in success, (
        "the success path does not tell guardedClick to keep the button disabled"
    )
    assert "btn.disabled = true" not in success, (
        "disabling the button by hand does not survive the guard's finally"
    )
