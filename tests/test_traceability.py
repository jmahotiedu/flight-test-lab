"""The requirement map must describe things that actually exist.

A traceability CSV is only worth the audit it supports. Every failure mode
here is silent by nature: a renamed test, a requirement mapped to nothing, a
row pointing at an evidence file the pipeline never produces. Each looks fine
in the spreadsheet and proves nothing about the system.

These checks are deliberately mechanical — they compare the declared map
against the repository, not against intent.
"""

from __future__ import annotations

import ast
import csv
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "requirements" / "software_requirements.csv"
TRACEABILITY = REPO_ROOT / "requirements" / "traceability.csv"


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [row for row in csv.DictReader(handle) if any(row.values())]


def _defined_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def test_every_mapped_test_exists() -> None:
    """A row naming a test that no longer exists is a broken chain.

    Renaming or deleting a test is easy; updating the map is easy to forget,
    and nothing else in the suite would notice.
    """
    missing: list[str] = []
    for row in _rows(TRACEABILITY):
        node_id = row["test_case"].strip()
        if "::" not in node_id or node_id.startswith("cpp/"):
            continue  # CTest rows are covered by ctest itself
        file_part, _, test_name = node_id.partition("::")
        target = REPO_ROOT / file_part
        if not target.is_file():
            missing.append(f"{row['requirement_id']}: no such file {file_part}")
            continue
        if test_name.rstrip("*") and not any(
            name.startswith(test_name.rstrip("*"))
            for name in _defined_functions(target)
        ):
            missing.append(f"{row['requirement_id']}: {file_part} has no {test_name}")
    assert not missing, (
        "traceability rows point at tests that do not exist:\n" + "\n".join(missing)
    )


def test_every_requirement_is_mapped() -> None:
    """A declared requirement with no row is unverified on paper."""
    declared = {row["requirement_id"].strip() for row in _rows(REQUIREMENTS)}
    mapped = {row["requirement_id"].strip() for row in _rows(TRACEABILITY)}
    unmapped = declared - mapped
    assert not unmapped, (
        f"requirements declared but never mapped to a test: {sorted(unmapped)}"
    )


def test_every_mapped_requirement_is_declared() -> None:
    """A row for an undeclared ID is a dangling reference the other way."""
    declared = {row["requirement_id"].strip() for row in _rows(REQUIREMENTS)}
    mapped = {row["requirement_id"].strip() for row in _rows(TRACEABILITY)}
    undeclared = mapped - declared
    assert not undeclared, (
        f"traceability rows for undeclared requirements: {sorted(undeclared)}"
    )


@pytest.mark.parametrize("column", ["title", "requirement", "verification_method"])
def test_requirements_are_completely_specified(column: str) -> None:
    """An ID with empty columns is a placeholder, not a requirement."""
    blank = [
        row["requirement_id"]
        for row in _rows(REQUIREMENTS)
        if not row.get(column, "").strip()
    ]
    assert not blank, f"requirements with an empty {column!r}: {blank}"
