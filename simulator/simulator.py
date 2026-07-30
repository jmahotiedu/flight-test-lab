"""Synthetic line-delimited JSON device-under-test server."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import socketserver
import threading
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("synthetic_dut")


def build_response(message: object) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {"status": "error", "error_code": "INVALID_MESSAGE_TYPE", "message": "Request must be a JSON object"}

    sequence = message.get("sequence")
    command = message.get("command")

    if command is None:
        return {"status": "error", "error_code": "MISSING_COMMAND", "sequence": sequence}
    if command == "status":
        return {"status": "ok", "state": "READY", "sequence": sequence}

    return {
        "status": "error",
        "error_code": "UNSUPPORTED_COMMAND",
        "command": command,
        "sequence": sequence,
    }


class DutRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        peer = self.client_address
        LOGGER.info("client_connected peer=%s:%s", peer[0], peer[1])

        while line := self.rfile.readline():
            raw_line = line.decode("utf-8", errors="replace").rstrip("\r\n")
            LOGGER.info("request peer=%s:%s payload=%s", peer[0], peer[1], raw_line)

            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError:
                response: dict[str, Any] = {"status": "error", "error_code": "INVALID_JSON"}
            else:
                response = build_response(message)

            self.wfile.write((json.dumps(response, sort_keys=True) + "\n").encode("utf-8"))
            self.wfile.flush()
            LOGGER.info("response peer=%s:%s payload=%s", peer[0], peer[1], response)

        LOGGER.info("client_disconnected peer=%s:%s", peer[0], peer[1])


class ThreadingDutServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def configure_logging(log_file: Path | None, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        fmt="%(asctime)sZ level=%(levelname)s message=%(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = __import__("time").gmtime

    handlers: list[logging.Handler] = []
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    handlers.append(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers, force=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def run_server(host: str, port: int) -> None:
    with ThreadingDutServer((host, port), DutRequestHandler) as server:
        stop_requested = threading.Event()

        def request_shutdown(signum: int, frame: object) -> None:
            del frame
            LOGGER.info("shutdown_requested signal=%s", signum)
            stop_requested.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGINT, request_shutdown)
            signal.signal(signal.SIGTERM, request_shutdown)

        LOGGER.info("dut_ready host=%s port=%s", host, port)
        server.serve_forever(poll_interval=0.05)
        LOGGER.info("dut_stopped requested=%s", stop_requested.is_set())


def main() -> int:
    args = parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    configure_logging(args.log_file, args.verbose)
    run_server(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
