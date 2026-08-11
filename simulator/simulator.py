"""Synthetic line-delimited JSON device-under-test server."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socketserver
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("synthetic_dut")


@dataclass(frozen=True, slots=True)
class FaultConfig:
    """Configuration-driven fault injection for the DUT.

    All fields default to "no fault".  A fault config can come from the
    --fault presets or from a JSON file via --fault-config with the keys
    response_delay_ms, drop_connection, malformed_response,
    startup_delay_ms, and exit_after_requests.
    """

    response_delay_ms: int = 0
    drop_connection: bool = False
    malformed_response: bool = False
    startup_delay_ms: int = 0
    exit_after_requests: int | None = None


FAULT_NAMES = (
    "delayed_response",
    "dropped_connection",
    "malformed_response",
    "startup_delay",
    "process_termination",
)

_FAULT_CONFIG_KEYS = {
    "response_delay_ms",
    "drop_connection",
    "malformed_response",
    "startup_delay_ms",
    "exit_after_requests",
}


def load_fault_config(path: Path) -> FaultConfig:
    """Load a fault configuration from a JSON file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"--fault-config: cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("--fault-config must contain a JSON object")
    unknown = set(data) - _FAULT_CONFIG_KEYS
    if unknown:
        raise SystemExit(f"--fault-config: unknown keys: {sorted(unknown)}")
    return FaultConfig(
        response_delay_ms=int(data.get("response_delay_ms") or 0),
        drop_connection=bool(data.get("drop_connection", False)),
        malformed_response=bool(data.get("malformed_response", False)),
        startup_delay_ms=int(data.get("startup_delay_ms") or 0),
        exit_after_requests=(
            int(data["exit_after_requests"])
            if data.get("exit_after_requests") is not None
            else None
        ),
    )


def resolve_fault_config(args: argparse.Namespace) -> FaultConfig:
    """Combine --fault / --fault-delay-ms / --fault-config into one config."""
    if args.fault_config is not None:
        return load_fault_config(args.fault_config)
    delay = int(args.fault_delay_ms)
    presets = {
        "delayed_response": FaultConfig(response_delay_ms=delay),
        "dropped_connection": FaultConfig(drop_connection=True),
        "malformed_response": FaultConfig(malformed_response=True),
        "startup_delay": FaultConfig(startup_delay_ms=delay),
        "process_termination": FaultConfig(exit_after_requests=1),
    }
    if args.fault is None:
        return FaultConfig()
    return presets[str(args.fault)]


def build_response(message: object) -> dict[str, Any]:
    if not isinstance(message, dict):
        return {
            "status": "error",
            "error_code": "INVALID_MESSAGE_TYPE",
            "message": "Request must be a JSON object",
        }

    sequence = message.get("sequence")
    command = message.get("command")

    if command is None:
        return {
            "status": "error",
            "error_code": "MISSING_COMMAND",
            "sequence": sequence,
        }
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
        server = self.server
        faults = (
            server.fault_config
            if isinstance(server, ThreadingDutServer)
            else FaultConfig()
        )
        LOGGER.info("client_connected peer=%s:%s", peer[0], peer[1])

        while line := self.rfile.readline():
            raw_line = line.decode("utf-8", errors="replace").rstrip("\r\n")
            LOGGER.info("request peer=%s:%s payload=%s", peer[0], peer[1], raw_line)

            if faults.exit_after_requests is not None and isinstance(
                server, ThreadingDutServer
            ):
                with server.request_lock:
                    server.request_count += 1
                    count = server.request_count
                if count >= faults.exit_after_requests:
                    LOGGER.warning(
                        "fault_injected fault=process_termination after_requests=%d",
                        count,
                    )
                    os._exit(1)

            if faults.drop_connection:
                LOGGER.warning(
                    "fault_injected fault=drop_connection peer=%s:%s", peer[0], peer[1]
                )
                return

            try:
                message = json.loads(raw_line)
            except json.JSONDecodeError:
                response: dict[str, Any] = {
                    "status": "error",
                    "error_code": "INVALID_JSON",
                }
            else:
                response = build_response(message)

            if faults.response_delay_ms > 0:
                LOGGER.warning(
                    "fault_injected fault=delayed_response delay_ms=%d",
                    faults.response_delay_ms,
                )
                time.sleep(faults.response_delay_ms / 1000)

            if faults.malformed_response:
                LOGGER.warning(
                    "fault_injected fault=malformed_response peer=%s:%s",
                    peer[0],
                    peer[1],
                )
                self.wfile.write(b"this-is-not-json\n")
                self.wfile.flush()
                continue

            self.wfile.write(
                (json.dumps(response, sort_keys=True) + "\n").encode("utf-8")
            )
            self.wfile.flush()
            LOGGER.info("response peer=%s:%s payload=%s", peer[0], peer[1], response)

        LOGGER.info("client_disconnected peer=%s:%s", peer[0], peer[1])


class ThreadingDutServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    fault_config: FaultConfig
    request_count: int
    request_lock: threading.Lock


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
    parser.add_argument("--fault", choices=FAULT_NAMES, default=None)
    parser.add_argument("--fault-delay-ms", type=int, default=400)
    parser.add_argument("--fault-config", type=Path, default=None)
    return parser.parse_args()


def run_server(host: str, port: int, fault_config: FaultConfig) -> None:
    with ThreadingDutServer((host, port), DutRequestHandler) as server:
        server.fault_config = fault_config
        server.request_count = 0
        server.request_lock = threading.Lock()
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
    fault_config = resolve_fault_config(args)
    if fault_config.startup_delay_ms > 0:
        LOGGER.warning(
            "fault_injected fault=startup_delay delay_ms=%d",
            fault_config.startup_delay_ms,
        )
        time.sleep(fault_config.startup_delay_ms / 1000)
    run_server(args.host, args.port, fault_config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
