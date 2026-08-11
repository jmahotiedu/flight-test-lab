"""TCP client used by tests to communicate with the synthetic DUT."""

from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self


class LabCommunicationError(RuntimeError):
    """Raised when communication with the DUT fails."""


@dataclass(frozen=True, slots=True)
class LabResponse:
    """Decoded DUT response and measured request duration."""

    payload: dict[str, Any]
    elapsed_seconds: float


class LabClient:
    """Line-delimited JSON client for the synthetic DUT."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        connect_timeout: float = 1.0,
        response_timeout: float = 0.5,
    ) -> None:
        if not host:
            raise ValueError("host must not be empty")
        if not 1 <= port <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if connect_timeout <= 0 or response_timeout <= 0:
            raise ValueError("timeouts must be positive")

        self._host = host
        self._port = port
        self._connect_timeout = connect_timeout
        self._response_timeout = response_timeout
        self._socket: socket.socket | None = None
        self._reader: Any = None

    @property
    def is_connected(self) -> bool:
        return self._socket is not None and self._reader is not None

    def connect(self) -> None:
        if self.is_connected:
            return

        try:
            connection = socket.create_connection(
                (self._host, self._port), timeout=self._connect_timeout
            )
            connection.settimeout(self._response_timeout)
            reader = connection.makefile("r", encoding="utf-8", newline="\n")
        except OSError as exc:
            self.close()
            raise LabCommunicationError(
                f"Unable to connect to {self._host}:{self._port}"
            ) from exc

        self._socket = connection
        self._reader = reader

    def wait_until_ready(
        self,
        *,
        deadline_seconds: float = 5.0,
        poll_seconds: float = 0.05,
    ) -> None:
        if deadline_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("deadline and polling interval must be positive")

        deadline = time.monotonic() + deadline_seconds
        last_error: Exception | None = None

        while time.monotonic() < deadline:
            try:
                if not self.is_connected:
                    self.connect()
                response = self.request({"command": "status", "sequence": 0})
                if response.payload.get("state") == "READY":
                    return
            except LabCommunicationError as exc:
                last_error = exc
                self.close()
            time.sleep(poll_seconds)

        raise TimeoutError(
            f"DUT did not report READY within {deadline_seconds:.2f} seconds"
        ) from last_error

    def request(self, message: dict[str, Any]) -> LabResponse:
        if not self.is_connected or self._socket is None or self._reader is None:
            raise LabCommunicationError("Client is not connected")

        encoded = (json.dumps(message, sort_keys=True) + "\n").encode("utf-8")
        started = time.monotonic()

        try:
            self._socket.sendall(encoded)
            line = self._reader.readline()
        except (TimeoutError, OSError) as exc:
            raise LabCommunicationError(f"Request failed: {message!r}") from exc

        elapsed = time.monotonic() - started
        if not line:
            raise LabCommunicationError("DUT closed the connection")

        try:
            payload = json.loads(line)
        # The DUT is the thing under test, so its reply is untrusted: an
        # oversized integer raises ValueError and deep nesting
        # RecursionError. A learner's test should see this library's
        # documented error, not a decoder exception from inside it.
        except (ValueError, RecursionError) as exc:
            raise LabCommunicationError(f"DUT returned invalid JSON: {line!r}") from exc

        if not isinstance(payload, dict):
            raise LabCommunicationError(
                f"Expected a JSON object, received {type(payload).__name__}"
            )

        return LabResponse(payload=payload, elapsed_seconds=elapsed)

    def close(self) -> None:
        if self._reader is not None:
            try:
                self._reader.close()
            finally:
                self._reader = None
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
