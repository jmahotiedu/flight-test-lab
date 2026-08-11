"""Root conftest: register command-line options before pytest parses them.

``--dut`` used to live in ``tests/conftest.py``, and that works only when the
command names a path under ``tests/``.  A bare ``pytest --dut cpp`` from the
repository root — the form the README documents — parses its arguments before
any nested conftest is imported, so the option did not exist yet:

    error: unrecognized arguments: --dut

CI never saw it because every CI invocation names a path.  A root conftest is
loaded during startup, so the option is registered whichever way pytest is
called.
"""

from __future__ import annotations

import os

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--dut",
        action="store",
        default=os.environ.get("FTL_DUT", "python"),
        choices=("python", "cpp"),
        help=(
            "Which device-under-test implementation to exercise. Both speak the "
            "same protocol; 'cpp' requires cpp/build to be built first."
        ),
    )
