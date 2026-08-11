"""Parity tests for the C++ DUT (REQ-CPP-001).

The point of a second implementation is that the harness should not care which
one it is talking to.  These tests prove that by driving both DUTs with the
same bytes and comparing the bytes that come back — not by comparing decoded
dicts, which would hide a formatting difference that a stricter client, or a
log-diffing tool, would trip over.

Every test here skips (never fails) when cpp/build has not been built, so the
suite stays green on a machine without a C++ toolchain.
"""

from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from conftest import cpp_dut_path, reserve_local_port
from testlab.client import LabClient

REPO_ROOT = Path(__file__).resolve().parents[1]

# Requests chosen to hit every branch of build_response, plus the shapes that
# tempt an implementation into diverging: absent vs. explicitly-null command,
# a non-integer sequence, a nested object, and malformed input.
PARITY_REQUESTS = (
    '{"command": "status", "sequence": 1}',
    '{"command": "launch", "sequence": 7}',
    '{"sequence": 2}',
    '{"command": null, "sequence": 2}',
    '{"command": "status"}',
    '{"command": 5, "sequence": 3}',
    '{"command": "status", "sequence": "abc"}',
    '{"command": "status", "sequence": 1, "extra": {"a": [1, 2]}}',
    "{not json",
    "",
    "[1, 2, 3]",
    # Malformed numbers: a scanner that is merely permissive (or that leans on
    # strtod, which stops at the first bad character and reports success)
    # accepts these while Python rejects the whole document.
    '{"command": "status", "sequence": 1+2}',
    '{"command": "status", "sequence": 1.2.3}',
    '{"command": "status", "sequence": 1e}',
    '{"command": "status", "sequence": 01}',
    '{"command": "status", "sequence": .5}',
    '{"command": "status", "sequence": 1.}',
    '{"command": "status", "sequence": +1}',
    # Well-formed numbers that must survive intact, including the ones whose
    # *formatting* differs between a naive C++ serialiser and Python's float
    # repr (1.5e3 must print as 1500.0, not 1500), overflow/underflow, and
    # integers beyond int64 — Python has bignums and echoes them exactly.
    '{"command": "status", "sequence": -12}',
    '{"command": "status", "sequence": 1.5e3}',
    '{"command": "status", "sequence": 0}',
    '{"command": "status", "sequence": 1.0}',
    '{"command": "status", "sequence": -0.0}',
    '{"command": "status", "sequence": 0.1}',
    # Python's repr switches to scientific notation outside -4 <= exp < 16.
    # Shortest-round-trip digits alone do not reproduce that choice: a plain
    # to_chars prints 1e6 as "1e+06" and expands 1.2345678901234567e20.
    '{"command": "status", "sequence": 1e6}',
    '{"command": "status", "sequence": -1e6}',
    '{"command": "status", "sequence": 1e15}',
    '{"command": "status", "sequence": 1e16}',
    '{"command": "status", "sequence": 1e-4}',
    '{"command": "status", "sequence": 1e-5}',
    '{"command": "status", "sequence": 123456789012345678.0}',
    '{"command": "status", "sequence": 1.2345678901234567e20}',
    '{"command": "status", "sequence": 5e-324}',
    '{"command": "status", "sequence": 1.7976931348623157e308}',
    '{"command": "status", "sequence": 3.141592653589793}',
    '{"command": "status", "sequence": 1E3}',
    '{"command": "status", "sequence": 1e400}',
    '{"command": "status", "sequence": 1e-400}',
    '{"command": "status", "sequence": 9223372036854775807}',
    '{"command": "status", "sequence": 9223372036854775808}',
    '{"command": "status", "sequence": -99999999999999999999}',
    # Duplicate members: Python keeps the last occurrence, so a permissive
    # parser that keeps the first answers a different request entirely.
    '{"command": "launch", "command": "status", "sequence": 1}',
    '{"command": "status", "sequence": 1, "sequence": 2}',
    '{"command": "status", "sequence": {"a": 1, "a": 2}}',
    # Structural edge cases.
    '{"command": "status", "sequence": 1,}',
    '{"command": "status" "sequence": 1}',
    '{"command": "sta\\u0074us", "sequence": 1}',
)

# Commands the course has the learner add to the Python DUT (Day 2 adds ping,
# Day 9 adds counter).  They are not part of the shipped protocol, so on an
# untouched checkout both DUTs reject them identically.
COURSE_ADDED_COMMANDS = ("ping", "counter")

# Payloads that are not valid UTF-8, or that exercise string rules a text-level
# test cannot express.  Python decodes each line with errors="replace" before
# parsing, so the native DUT has to do the same to stay byte-identical.
PARITY_BYTE_REQUESTS = (
    pytest.param(b'{"command":"status","sequence":"\xff"}', id="invalid-utf8-byte"),
    pytest.param(b'{"command":"status","sequence":"\xc3"}', id="truncated-sequence"),
    pytest.param(b'{"command":"status","sequence":"\xc0\xaf"}', id="overlong"),
    pytest.param(b'{"command":"status","sequence":"\xed\xa0\x80"}', id="raw-surrogate"),
    pytest.param(b'{"command":"status","sequence":"\xf4\x90\x80\x80"}', id="above-max"),
    pytest.param(b'{"command":"status","sequence":"\\ud800"}', id="lone-surrogate"),
    pytest.param(b'{"command":"status","sequence":"\\ud83d\\ude00"}', id="pair"),
    pytest.param(b'{"command":"status","sequence":"a\tb"}', id="raw-tab-in-string"),
    pytest.param(b'{"command":"status","sequence":"a\x01b"}', id="raw-control-char"),
    pytest.param(b'{"command":"status","sequence":"a\\tb"}', id="escaped-tab"),
    pytest.param(b'{"command":"status","sequence":"a\x7fb"}', id="del-char"),
    # A truncated-but-valid prefix is one maximal subpart, so it yields one
    # replacement character, not one per byte.
    pytest.param(b'{"command":"status","sequence":"\xe1\x80"}', id="truncated-3byte"),
    pytest.param(
        b'{"command":"status","sequence":"\xf0\x9f\x98"}', id="truncated-4byte"
    ),
    pytest.param(b'{"command":"status","sequence":"\xe0\x80"}', id="overlong-3byte"),
    # Nesting: both DUTs accept up to the documented limit and reject beyond,
    # so a payload cannot mean different things to the two implementations.
    pytest.param(
        ('{"command":"status","sequence":' + "[" * 100 + "]" * 100 + "}").encode(),
        id="nested-at-limit",
    ),
    pytest.param(
        ('{"command":"status","sequence":' + "[" * 101 + "]" * 101 + "}").encode(),
        id="nested-over-limit",
    ),
    pytest.param(
        ('{"command":"status","sequence":' + "[" * 5000 + "]" * 5000 + "}").encode(),
        id="nested-absurd",
    ),
    pytest.param('{"command":"status","sequence":"é😀"}'.encode(), id="non-ascii"),
    # Integer literals at and past MAX_INT_DIGITS. CPython caps
    # integer-string conversion, and the ValueError that raises is not a
    # JSONDecodeError — so this used to unwind the Python DUT's connection
    # thread while the C++ DUT echoed the number back.
    pytest.param(
        ('{"command":"status","sequence":' + "1" * 4300 + "}").encode(),
        id="int-at-digit-limit",
    ),
    pytest.param(
        ('{"command":"status","sequence":' + "1" * 4301 + "}").encode(),
        id="int-over-digit-limit",
    ),
    pytest.param(
        ('{"command":"status","sequence":-' + "1" * 4301 + "}").encode(),
        id="negative-int-over-digit-limit",
    ),
)


def _wait_ready(host: str, port: int, deadline_seconds: float = 10.0) -> None:
    client = LabClient(host, port)
    try:
        client.wait_until_ready(deadline_seconds=deadline_seconds)
    finally:
        client.close()


def _wait_for_accept(host: str, port: int, deadline_seconds: float = 10.0) -> None:
    """Wait until the port accepts, without sending anything.

    wait_until_ready sends status requests, which the DUT counts — so it
    cannot be used to reach a known request number.
    """
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError(f"port {port} never accepted a connection")


def _start(argv: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        argv,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5.0)


def _ask_raw(port: int, request: str, timeout: float = 5.0) -> str:
    """Send one raw line and return the raw reply line, bytes unmodified."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(request.encode("utf-8") + b"\n")
        return sock.makefile("rb").readline().decode("utf-8").rstrip("\n")


@pytest.fixture()
def cpp_dut_binary() -> Path:
    """The built binary, for tests that need their own DUT arguments."""
    binary = cpp_dut_path()
    if binary is None:
        pytest.skip(
            "C++ DUT not built: cmake -S cpp -B cpp/build && cmake --build cpp/build"
        )
    return binary


@pytest.fixture(scope="module")
def cpp_dut(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    binary = cpp_dut_path()
    if binary is None:
        pytest.skip(
            "C++ DUT not built: cmake -S cpp -B cpp/build && cmake --build cpp/build"
        )
    port = reserve_local_port()
    log_path = tmp_path_factory.mktemp("cpp-dut") / "dut.log"
    process = _start(
        [
            str(binary),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-file",
            str(log_path),
        ]
    )
    try:
        _wait_ready("127.0.0.1", port)
        yield port
    finally:
        _stop(process)


@pytest.fixture(scope="module")
def python_dut(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    port = reserve_local_port()
    log_path = tmp_path_factory.mktemp("py-dut") / "dut.log"
    process = _start(
        [
            sys.executable,
            "-m",
            "simulator.simulator",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-file",
            str(log_path),
        ]
    )
    try:
        _wait_ready("127.0.0.1", port)
        yield port
    finally:
        _stop(process)


@pytest.mark.requirement("REQ-CPP-001")
@pytest.mark.parametrize("request_line", PARITY_REQUESTS)
def test_cpp_dut_is_byte_identical_to_python(
    request_line: str, cpp_dut: int, python_dut: int
) -> None:
    """Both implementations must answer identically, byte for byte."""
    assert _ask_raw(cpp_dut, request_line) == _ask_raw(python_dut, request_line)


def _ask_bytes(port: int, payload: bytes, timeout: float = 5.0) -> bytes:
    """Send raw bytes and return the raw reply line, undecoded."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(payload + b"\n")
        return sock.makefile("rb").readline().rstrip(b"\n")


@pytest.mark.requirement("REQ-CPP-001")
@pytest.mark.parametrize("payload", PARITY_BYTE_REQUESTS)
def test_cpp_dut_matches_python_on_raw_bytes(
    payload: bytes, cpp_dut: int, python_dut: int
) -> None:
    """Malformed UTF-8 and string edge cases must agree at the byte level.

    Comparing decoded objects would hide the two things that actually differ
    between implementations here: how many U+FFFD replacements a bad sequence
    produces, and whether non-ASCII is escaped on the way out.
    """
    assert _ask_bytes(cpp_dut, payload) == _ask_bytes(python_dut, payload)


@pytest.mark.requirement("REQ-CPP-001")
@pytest.mark.port_parity
@pytest.mark.parametrize("command", COURSE_ADDED_COMMANDS)
def test_course_added_commands_do_not_diverge(
    command: str, cpp_dut: int, python_dut: int
) -> None:
    """If the course had you extend one DUT, the other must follow.

    Day 2 adds `ping` to the Python DUT and Day 9 adds `counter`.  Nothing
    stops a learner from doing that and then certifying "parity" against a C++
    DUT that never grew those branches, so the divergence is checked directly.
    On an untouched checkout both reject the command identically and this
    passes; once one side implements it, this fails until the other does too.

    That failure is the expected state for most of the course: Day 2 adds
    `ping` and Day 11 ports it. It carries the `port_parity` marker so the
    gates in between can run everything else -- without it, Day 10's
    full-suite gate is unpassable on a checkout with a built C++ DUT, and
    the lesson that fixes it sits behind that gate.
    """
    request = json.dumps({"command": command, "sequence": 1}, sort_keys=True)
    python_reply = _ask_raw(python_dut, request)
    cpp_reply = _ask_raw(cpp_dut, request)
    assert python_reply == cpp_reply, (
        f"the two DUTs disagree about {command!r}: the course adds it to the "
        f"Python DUT, so it has to be ported to cpp/ as well.\n"
        f"  python: {python_reply}\n  cpp:    {cpp_reply}"
    )


def _counter_branch(source: str) -> str | None:
    """The body of the `"counter"` branch, by brace matching.

    A fixed-size window after the string literal was wrong in a way that only
    shows up on a *correct* implementation: `std::atomic<int> g_counter` is
    declared at namespace scope, above the branch, so the window contained the
    increment and none of the synchronisation.
    """
    marker = source.find('"counter"')
    if marker < 0:
        return None
    opening = source.find("{", marker)
    if opening < 0:
        return None
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening : index + 1]
    return source[opening:]


# Names on the left of ++/--/+=/-=/=, or the receiver of .fetch_add/.fetch_sub.
_MUTATED = re.compile(
    r"(?:\+\+|--)\s*([A-Za-z_]\w*)"
    r"|([A-Za-z_]\w*)\s*(?:\+\+|--|\+=|-=|=(?!=))"
    r"|([A-Za-z_]\w*)\s*\.\s*(?:fetch_add|fetch_sub|exchange|store)"
)


def _synchronisation_in(branch: str, source: str) -> tuple[bool, str]:
    """Is the counter's read-modify-write actually synchronised?

    Two accepted shapes, which is what the lesson teaches: a lock held across
    the branch, or a counter whose *declaration* is atomic — wherever in the
    file that declaration happens to live.
    """
    for token in ("lock_guard", "scoped_lock", "unique_lock", "std::mutex"):
        if token in branch:
            return True, f"holds a lock ({token})"

    mutated = {name for match in _MUTATED.findall(branch) for name in match if name}
    for name in sorted(mutated):
        atomic = re.compile(
            rf"(?:std::)?atomic(?:_\w+)?\s*(?:<[^>]*>)?\s*{re.escape(name)}\b"
        )
        if atomic.search(source):
            return True, f"{name} is declared atomic"
    if not mutated:
        return False, "nothing in the branch looks like an increment"
    return False, (
        "no lock, and none of " + ", ".join(sorted(mutated)) + " is declared atomic"
    )


# The shapes a learner can legitimately write, and the ones that are the race.
# These need no C++ toolchain: the point is that the gate finds a declaration
# wherever it lives, rather than whatever happens to sit near the branch.
_COUNTER_SOURCE = """#include "dut/protocol.hpp"
{declaration}
namespace dut {{
Value build_response(const Value& request) {{
  if (command->as_string() == "counter") {{
{body}
  }}
  return Object{{{{"status", Value("error")}}}};
}}
}}
"""

_ATOMIC = """
#include <atomic>
namespace {
std::atomic<int> g_counter{0};
}
"""

_PLAIN = """
namespace {
int g_counter = 0;
}
"""

_MUTEX = """
#include <mutex>
namespace {
int g_counter = 0;
std::mutex g_m;
}
"""

_ATOMIC_BYSTANDER = """
#include <atomic>
namespace {
std::atomic<int> g_other{0};
int g_counter = 0;
}
"""

_LOCKED_BODY = """    std::lock_guard<std::mutex> guard(g_m);
    g_counter = g_counter + 1;"""

_INCREMENT = '    return Object{{"count", Value(++g_counter)}};'


@pytest.mark.parametrize(
    ("label", "declaration", "body", "synchronised"),
    [
        # The lesson's own hint: an atomic at namespace scope, incremented in
        # the branch. A fixed-size window after the string literal saw the
        # increment and none of the declaration, so it rejected a correct
        # implementation — and that gate is mandatory for finishing Day 11.
        ("namespace atomic", _ATOMIC, _INCREMENT, True),
        ("fetch_add", _ATOMIC, "    const int n = g_counter.fetch_add(1) + 1;", True),
        ("lock held in the branch", _MUTEX, _LOCKED_BODY, True),
        ("plain read-modify-write", _PLAIN, "    g_counter = g_counter + 1;", False),
        ("plain increment", _PLAIN, _INCREMENT, False),
        ("an atomic that is not the counter", _ATOMIC_BYSTANDER, _INCREMENT, False),
    ],
)
def test_counter_synchronisation_is_recognised_not_pattern_matched(
    label: str, declaration: str, body: str, synchronised: bool
) -> None:
    source = _COUNTER_SOURCE.format(declaration=declaration, body=body)
    branch = _counter_branch(source)
    assert branch is not None
    held, reason = _synchronisation_in(branch, source)
    assert held is synchronised, f"{label}: {reason}"


@pytest.mark.requirement("REQ-CPP-001")
def test_ported_counter_is_safe_under_concurrency(cpp_dut: int) -> None:
    """If counter was ported, it must survive concurrent clients.

    The Day 11 porting lesson tells the learner the C++ counter needs a mutex
    because the server is thread-per-connection. A single sequential request
    cannot tell a locked implementation from an unlocked one — both answer 1
    — so the gate would certify exactly the race the lesson warns about.
    Skips when counter is not implemented, which is the shipped state.
    """
    probe = json.loads(_ask_raw(cpp_dut, '{"command": "counter", "sequence": 0}'))
    if probe.get("error_code") == "UNSUPPORTED_COMMAND":
        pytest.skip("counter is not implemented in the C++ DUT (Day 9 extension)")

    # 1. Deterministic: the implementation must actually synchronise.
    #
    # This assertion carries the weight, because the behavioural one below
    # cannot. Measured against a deliberately unsynchronised `count = count +
    # 1`, 16 concurrent clients detected the lost update in only 4 of 5 runs —
    # a black-box race detector has false negatives by nature, and a gate that
    # misses one time in five certifies the bug it exists to catch.
    source = (REPO_ROOT / "cpp" / "src" / "protocol.cpp").read_text(
        encoding="utf-8", errors="replace"
    )
    branch = _counter_branch(source)
    assert branch is not None, (
        'no `"counter"` branch found in cpp/src/protocol.cpp — the concurrency '
        "check cannot read an implementation it cannot locate"
    )
    held, reason = _synchronisation_in(branch, source)
    assert held, (
        f"the C++ counter branch shows no synchronisation ({reason}). The "
        "server is thread-per-connection, so an unguarded read-modify-write is "
        "the race Day 9 taught you to find — this time in a language with no "
        "GIL to hide it."
    )

    # 2. Behavioural, best effort: with real contention, a lost update shows up
    #    as a duplicate count or a final value below the request total.
    threads_count, per_thread = 16, 25
    total = threads_count * per_thread
    counts: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(threads_count)

    def hammer() -> None:
        try:
            barrier.wait(timeout=15)
            local = [
                json.loads(_ask_raw(cpp_dut, '{"command": "counter", "sequence": 1}'))[
                    "count"
                ]
                for _ in range(per_thread)
            ]
            with lock:
                counts.extend(local)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"concurrent counter requests raised: {errors[:3]}"
    assert len(counts) == total
    # Assert the shape of the result, not absolute values: earlier tests (and
    # the probe above) have already advanced the counter, so anchoring on a
    # starting point would fail a correct implementation. N increments must
    # yield N distinct values forming one contiguous run — a lost update
    # shows up as a duplicate, which breaks both.
    assert len(set(counts)) == total, (
        f"{total} concurrent increments produced only {len(set(counts))} "
        "distinct values — updates were lost, so the counter's "
        "read-modify-write is not atomic"
    )
    assert max(counts) - min(counts) == total - 1, (
        f"counts spanned {min(counts)}..{max(counts)} for {total} increments, "
        "which is not a contiguous run — increments were interleaved or lost"
    )


@pytest.mark.requirement("REQ-CPP-001")
def test_cpp_dut_answers_status_within_deadline(cpp_dut: int) -> None:
    """REQ-COM-002's timing budget must hold for the native DUT too."""
    with LabClient("127.0.0.1", cpp_dut, response_timeout=0.5) as client:
        response = client.request({"command": "status", "sequence": 1})
    assert response.payload["state"] == "READY"
    assert response.elapsed_seconds <= 0.250


@pytest.mark.requirement("REQ-CPP-001")
def test_cpp_dut_survives_malformed_input(cpp_dut: int) -> None:
    """A bad line must not take the connection, or the process, down."""
    with LabClient("127.0.0.1", cpp_dut, response_timeout=0.5) as client:
        assert (
            client.request({"command": "status", "sequence": 1}).payload["status"]
            == "ok"
        )
    assert json.loads(_ask_raw(cpp_dut, "{not json"))["error_code"] == "INVALID_JSON"
    with LabClient("127.0.0.1", cpp_dut, response_timeout=0.5) as client:
        assert (
            client.request({"command": "status", "sequence": 2}).payload["status"]
            == "ok"
        )


@pytest.mark.requirement("REQ-CPP-001")
def test_cpp_dut_handles_concurrent_clients(cpp_dut: int) -> None:
    """One thread per connection: several clients are served at once."""
    clients = [LabClient("127.0.0.1", cpp_dut, response_timeout=1.0) for _ in range(5)]
    try:
        for client in clients:
            client.connect()
        for index, client in enumerate(clients):
            response = client.request({"command": "status", "sequence": index})
            assert response.payload["sequence"] == index
    finally:
        for client in clients:
            client.close()


@pytest.mark.requirement("REQ-CPP-001")
def test_cpp_dut_frames_on_newlines_not_packets(cpp_dut: int) -> None:
    """Two requests in one write must produce two replies: TCP is a stream."""
    with socket.create_connection(("127.0.0.1", cpp_dut), timeout=5.0) as sock:
        sock.settimeout(5.0)
        sock.sendall(
            b'{"command": "status", "sequence": 1}\n'
            b'{"command": "status", "sequence": 2}\n'
        )
        reader = sock.makefile("rb")
        first = json.loads(reader.readline())
        second = json.loads(reader.readline())
    assert (first["sequence"], second["sequence"]) == (1, 2)


@pytest.mark.requirement("REQ-CPP-001")
def test_cpp_dut_writes_the_same_evidence_lines(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The native log must carry the lines the evidence checks look for."""
    binary = cpp_dut_path()
    if binary is None:
        pytest.skip("C++ DUT not built")
    port = reserve_local_port()
    log_path = tmp_path_factory.mktemp("cpp-log") / "dut.log"
    process = _start(
        [
            str(binary),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-file",
            str(log_path),
        ]
    )
    try:
        _wait_ready("127.0.0.1", port)
        with LabClient("127.0.0.1", port, response_timeout=0.5) as client:
            client.request({"command": "status", "sequence": 1})
        deadline = time.monotonic() + 5.0
        text = ""
        while time.monotonic() < deadline:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            if "response peer=" in text:
                break
            time.sleep(0.05)
    finally:
        _stop(process)

    assert f"dut_ready host=127.0.0.1 port={port}" in text
    assert "client_connected peer=" in text
    assert "request peer=" in text
    assert "response peer=" in text
    # Same field layout as the Python DUT: an evidence parser written for one
    # must work on the other.
    assert " level=INFO message=" in text


@pytest.mark.requirement("REQ-CPP-001")
def test_cpp_dut_fires_a_nonterminal_fault_once(cpp_dut_binary: Path) -> None:
    """--fault-after N means "on request N", not "from request N onwards".

    Measured before the fix: requests 2, 3 and 4 all took ~400 ms. A fault
    injector that keeps firing is not producing the one controlled event an
    experiment is built around.
    """
    port = reserve_local_port()
    process = _start(
        [
            str(cpp_dut_binary),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--fault",
            "slow",
            "--fault-after",
            "2",
            "--fault-delay-ms",
            "400",
        ]
    )
    try:
        # Wait by connecting, not by asking: wait_until_ready sends status
        # requests, and those count. Measured [405, 0, 0, 0] the first time —
        # the readiness probe had consumed request 1 and the fault fired
        # correctly on request 2.
        _wait_for_accept("127.0.0.1", port)
        elapsed = []
        with socket.create_connection(("127.0.0.1", port), timeout=10) as connection:
            reader = connection.makefile("rb")
            for sequence in range(1, 5):
                started = time.monotonic()
                request = json.dumps(
                    {"command": "status", "sequence": sequence}, sort_keys=True
                )
                connection.sendall(request.encode() + b"\n")
                reader.readline()
                elapsed.append(int((time.monotonic() - started) * 1000))
    finally:
        _stop(process)

    assert elapsed[0] < 250, elapsed
    assert elapsed[1] >= 350, elapsed
    assert all(value < 250 for value in elapsed[2:]), (
        f"the fault fired more than once: {elapsed}"
    )


@pytest.mark.requirement("REQ-CPP-001")
def test_cpp_dut_accepts_a_hostname(cpp_dut_binary: Path) -> None:
    """The Python DUT binds `--host localhost`; so must this one.

    inet_pton takes numeric literals only, so the same harness configuration
    worked or failed depending on which implementation it pointed at.
    """
    port = reserve_local_port()
    process = _start([str(cpp_dut_binary), "--host", "localhost", "--port", str(port)])
    try:
        _wait_ready("127.0.0.1", port)
        assert process.poll() is None
    finally:
        _stop(process)


@pytest.mark.requirement("REQ-CPP-001")
@pytest.mark.parametrize("flag", ["--log-file", "--host", "--fault"])
def test_cpp_dut_rejects_an_option_missing_its_value(
    cpp_dut_binary: Path, flag: str
) -> None:
    """`--log-file --verbose` used to create a file named "--verbose".

    "Another token exists" is not "a value was given": consuming the next
    option silently misconfigures the run instead of reporting the usage
    error it plainly is.
    """
    result = subprocess.run(
        [str(cpp_dut_binary), flag, "--verbose"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
        cwd=str(REPO_ROOT),
    )
    assert result.returncode == 2
    assert f"{flag} requires a value" in result.stderr


@pytest.mark.requirement("REQ-CPP-001")
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="terminate() is TerminateProcess on Windows, so the graceful "
    "shutdown path cannot be reached from here; CI exercises it on Linux",
)
@pytest.mark.parametrize("fault", [None, "hang"])
def test_cpp_dut_shuts_down_with_a_client_connected(
    cpp_dut_binary: Path, fault: str | None
) -> None:
    """Handlers must not outlive the resources they use.

    Detached threads let a handler sit in recv() while run_server returned and
    WinsockGuard — plus the process-wide logging globals — began destruction.
    They are joined now, with a grace period: the hang fault blocks a handler
    forever on purpose, so waiting is not an option and neither is destroying
    Winsock underneath it. Both endings have to be prompt.
    """
    port = reserve_local_port()
    argv = [str(cpp_dut_binary), "--host", "127.0.0.1", "--port", str(port)]
    if fault:
        argv += ["--fault", fault, "--fault-after", "1"]
    process = subprocess.Popen(
        argv,
        cwd=str(REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _wait_for_accept("127.0.0.1", port)
        connection = socket.create_connection(("127.0.0.1", port), timeout=5)
        connection.sendall(b'{"command": "status", "sequence": 1}\n')
        if not fault:
            connection.makefile("rb").readline()
        else:
            time.sleep(0.5)  # let the handler reach the deadlock

        started = time.monotonic()
        process.terminate()
        exit_code = process.wait(timeout=20)
        elapsed = time.monotonic() - started
        stderr = process.stderr.read() if process.stderr else ""
    finally:
        connection.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)

    assert elapsed < 10, f"shutdown took {elapsed:.1f}s"
    assert exit_code == 0, stderr[-400:]
    assert "dut_stopped" in stderr
    if fault == "hang":
        assert "handler_still_running" in stderr
