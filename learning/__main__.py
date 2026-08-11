"""Launch the flight-test-lab learning environment.

Usage:
    python -m learning [--port N] [--no-browser]

Starts a local server bound to 127.0.0.1 and opens the learning dashboard.
Progress is stored in ``learning/.progress.json`` (gitignored) and resumes
automatically on the next launch.
"""

from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

from learning.checks.common import terminate_active_validators
from learning.server.app import build_server
from learning.server.curriculum import CurriculumError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Port to bind (default: pick a free one). Loopback only.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser window automatically.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    try:
        server = build_server(args.port, repo_root=repo_root)
    except CurriculumError as exc:
        print(f"Curriculum failed validation:\n{exc}")
        return 2

    host = str(server.server_address[0])
    port = int(server.server_address[1])
    url = f"http://{host}:{port}/"
    print("flight-test-lab learning environment")
    print(f"  dashboard: {url}")
    print(f"  bound to {host} only — not reachable from other machines")
    print("  press Ctrl+C to stop")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        # Handler threads are daemons and validator children run in their own
        # process group, so without this a Ctrl+C during a long pytest, CMake
        # or GDB check would leave that process — and any DUT it started —
        # running after the UI said it stopped.
        reaped = terminate_active_validators()
        if reaped:
            print(f"  reaped {reaped} running check(s)")
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
