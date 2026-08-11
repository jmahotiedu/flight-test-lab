"""Allowlisted lesson validators.

The browser can only ask for ``POST /api/validate {"lesson_id": ...}``; the
server looks up the validator *named in the lesson definition* (curriculum
files on disk) and runs it through this registry.  There is deliberately no
endpoint that accepts a command, path, or code from a request body.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_OUTPUT_CHARS = 4000
DEFAULT_TIMEOUT_SECONDS = 30.0
# The ceiling has to be at least the largest timeout any lesson authors, or
# clamp_timeout silently shortens it and a slow machine fails a check the
# curriculum explicitly allowed time for.  tests/learning/test_curriculum.py
# asserts no lesson exceeds this.
MAX_TIMEOUT_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class ValidatorContext:
    repo_root: Path
    python: str = sys.executable


@dataclass(slots=True)
class CheckResult:
    name: str
    passed: bool
    exit_status: int | None
    stdout: str
    stderr: str
    duration_ms: int
    interpretation: str
    timed_out: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "exit_status": self.exit_status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "interpretation": self.interpretation,
            "timed_out": self.timed_out,
            "details": self.details,
        }


ValidatorFn = Callable[[dict[str, Any], ValidatorContext], CheckResult]


def clamp_timeout(args: dict[str, Any]) -> float:
    raw = args.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS
    return max(1.0, min(value, MAX_TIMEOUT_SECONDS))


def truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + "\n... [truncated]"


def _build_registry() -> dict[str, ValidatorFn]:
    from learning.checks import (
        artifact_check,
        behavior_probe,
        pytest_check,
        python_probe,
        source_check,
        toolchain_check,
    )

    return {
        "python_probe": python_probe.run,
        "pytest_check": pytest_check.run,
        "behavior_probe": behavior_probe.run,
        "source_check": source_check.run,
        "artifact_check": artifact_check.run,
        "toolchain_check": toolchain_check.run,
    }


_REGISTRY: dict[str, ValidatorFn] | None = None


def registry() -> dict[str, ValidatorFn]:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _build_registry()
    return _REGISTRY


def validator_names() -> set[str]:
    return set(registry())


def run_validator(
    name: str, args: dict[str, Any], context: ValidatorContext
) -> CheckResult:
    fn = registry().get(name)
    if fn is None:
        raise KeyError(f"unknown validator {name!r}")
    return fn(args, context)
