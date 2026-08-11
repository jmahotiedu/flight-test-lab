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

# Maximum container nesting accepted in a request.  A protocol needs an
# explicit bound: without one, a deeply nested line makes json.loads raise
# RecursionError — which is not a JSONDecodeError, so it would escape the
# handler and kill the connection thread instead of returning INVALID_JSON.
# The C++ DUT enforces the same number (cpp/src/json.cpp, kMaxDepth) so a
# payload cannot mean different things to the two implementations.
MAX_NESTING_DEPTH = 100

# Maximum digits in an integer literal.  CPython 3.11 caps integer-string
# conversion (4300 digits by default) because the conversion is quadratic, and
# the ValueError it raises is not a JSONDecodeError — so an oversized number
# escaped the handler entirely.  Making it an explicit protocol bound means the
# answer does not depend on the interpreter's setting, and the C++ DUT
# (cpp/src/json.cpp, kMaxIntDigits) enforces the same number.
MAX_INT_DIGITS = 4300

# Upper bound for any injected delay.  time.sleep() raises OverflowError on a
# large-but-valid integer, which in the request path means the client gets no
# response at all while the log already claims fault_injected — a fault that
# reports itself as injected and then does something else is the one thing a
# fault injector must never do.  A day is far beyond any test's patience.
MAX_DELAY_MS = 86_400_000


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
    # Every way json.loads can fail, not just the obvious one. JSONDecodeError
    # and UnicodeDecodeError are both ValueError subclasses; so is the
    # oversized-integer error CPython raises past its digit limit. Deep nesting
    # raises RecursionError, which is not a ValueError at all. Each of these
    # used to end the run with a traceback instead of the same one-line
    # diagnostic every other malformed config gets.
    except (OSError, ValueError, RecursionError) as exc:
        raise SystemExit(f"--fault-config: cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit("--fault-config must contain a JSON object")
    unknown = set(data) - _FAULT_CONFIG_KEYS
    if unknown:
        raise SystemExit(f"--fault-config: unknown keys: {sorted(unknown)}")

    def _bool(key: str) -> bool:
        # bool("false") is True, so coercing here would silently *enable* the
        # fault an operator wrote the config to disable — and the run would
        # look like a product failure. Require a real JSON boolean.
        value = data.get(key, False)
        if not isinstance(value, bool):
            raise SystemExit(
                f"--fault-config: {key} must be true or false, got {value!r}"
            )
        return value

    def _int(key: str, *, allow_none: bool = False) -> int | None:
        value = data.get(key)
        if value is None:
            return None if allow_none else 0
        # bool is a subclass of int; {"response_delay_ms": true} is a mistake,
        # not a 1 ms delay.
        if isinstance(value, bool) or not isinstance(value, int):
            raise SystemExit(f"--fault-config: {key} must be an integer, got {value!r}")
        if value < 0:
            raise SystemExit(f"--fault-config: {key} must not be negative")
        if key.endswith("_ms") and value > MAX_DELAY_MS:
            raise SystemExit(
                f"--fault-config: {key} must be at most {MAX_DELAY_MS} ms "
                "(24 hours); larger values overflow time.sleep()"
            )
        return value

    # A count of zero is ambiguous — "disabled" by the manifest's own
    # convention (every other field treats 0 as off), or "terminate before
    # serving anything"? Rejecting it means a manifest cannot silently select
    # a third behaviour: today 0 would arm the fault and kill the DUT on the
    # first request.
    exit_after = _int("exit_after_requests", allow_none=True)
    if exit_after is not None and exit_after < 1:
        raise SystemExit(
            "--fault-config: exit_after_requests must be 1 or more "
            "(omit it, or use null, to disable the fault)"
        )

    return FaultConfig(
        response_delay_ms=_int("response_delay_ms") or 0,
        drop_connection=_bool("drop_connection"),
        malformed_response=_bool("malformed_response"),
        startup_delay_ms=_int("startup_delay_ms") or 0,
        exit_after_requests=exit_after,
    )


def resolve_fault_config(args: argparse.Namespace) -> FaultConfig:
    """Combine --fault / --fault-delay-ms / --fault-config into one config."""
    if args.fault_config is not None:
        # Silently letting the manifest win would mean the command line no
        # longer describes the experiment that ran — the worst property for
        # something whose output is evidence.
        if args.fault is not None:
            raise SystemExit(
                "--fault and --fault-config both select the fault behaviour; "
                "pass only one so the command line describes the run"
            )
        return load_fault_config(args.fault_config)
    delay = int(args.fault_delay_ms)
    # A negative delay silently disables the fault at both runtime guards
    # (`> 0` checks), so the run looks clean while the requested fault never
    # engaged. The config-file path already rejects negatives; the CLI has to
    # agree, or the same mistake is fatal in one place and invisible in the
    # other.
    if delay < 0:
        raise SystemExit("--fault-delay-ms must not be negative")
    if delay > MAX_DELAY_MS:
        raise SystemExit(
            f"--fault-delay-ms must be at most {MAX_DELAY_MS} ms (24 hours); "
            "larger values overflow time.sleep()"
        )
    # Zero is equally useless for the two timing presets: both runtime guards
    # are `> 0`, so the fault would neither delay anything nor log
    # fault_injected, and the run would look like a clean experiment that
    # exercised a fault. Zero stays legal when no timing fault is requested.
    if args.fault in ("delayed_response", "startup_delay") and delay == 0:
        raise SystemExit(
            f"--fault {args.fault} needs a positive --fault-delay-ms "
            "(0 would disable the fault it requests)"
        )
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


def exceeds_nesting_limit(value: object, limit: int = MAX_NESTING_DEPTH) -> bool:
    """True when ``value`` nests deeper than the protocol allows.

    Walked iteratively rather than recursively: a recursive check on a
    hostile payload would hit the same RecursionError this limit exists to
    prevent.
    """
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > limit:
            return True
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    return False


def _parse_int(token: str) -> int:
    """json's integer hook, bounded by MAX_INT_DIGITS.

    CPython 3.11 refuses to convert integer strings past a per-interpreter
    limit (4300 digits by default), and the ValueError it raises is not a
    JSONDecodeError — so a long-but-valid number used to unwind the connection
    thread.  Enforcing the bound here makes the answer INVALID_JSON, and makes
    it the *same* answer on 3.10, on an interpreter started with a different
    limit, and in the C++ DUT, which enforces the identical constant.
    """
    if len(token.lstrip("-")) > MAX_INT_DIGITS:
        raise ValueError(f"integer exceeds {MAX_INT_DIGITS} digits")
    return int(token)


def decode_request(raw_line: str) -> tuple[object | None, bool]:
    """Decode one request line; return (message, ok).

    ``ok`` is False for anything the protocol rejects outright — malformed
    JSON, nesting past MAX_NESTING_DEPTH, or an integer past MAX_INT_DIGITS.
    RecursionError is caught alongside ValueError because json.loads raises it
    on deeply nested input, and an uncaught one would take the connection
    thread down.  ValueError covers JSONDecodeError (a subclass) and the
    oversized-integer case above.
    """
    try:
        message = json.loads(raw_line, parse_int=_parse_int)
    except (ValueError, RecursionError):
        return None, False
    if exceeds_nesting_limit(message):
        return None, False
    return message, True


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

            # DEBUG records exist so --verbose has something to show: the
            # byte-level detail you want when a request looks wrong on the
            # wire but fine in the summary line above.
            LOGGER.debug(
                "request_bytes peer=%s:%s length=%d raw=%r",
                peer[0],
                peer[1],
                len(line),
                line,
            )

            message, decoded = decode_request(raw_line)
            if not decoded:
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
            LOGGER.debug(
                "response_detail peer=%s:%s error_code=%s keys=%s",
                peer[0],
                peer[1],
                response.get("error_code"),
                sorted(response),
            )

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
    # Resolved before the log is opened. configure_logging creates the parent
    # directory and the file, so validating afterwards left an evidence
    # artifact behind for a run that never started — and the whole point of
    # evidence/logs/dut.log existing is that an experiment ran.
    fault_config = resolve_fault_config(args)
    configure_logging(args.log_file, args.verbose)
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
