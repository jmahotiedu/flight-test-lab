# flight-test-lab

A small, synthetic software-test laboratory for practicing the core skills used in test automation and hardware-in-the-loop-style workflows:

- Python test-harness engineering
- Reliable process lifecycle management
- TCP/JSON protocol testing
- Requirements-linked pytest tests
- Timing verification and failure evidence
- Cross-platform execution on Linux and Windows
- Repeatable CI validation

This project does **not** reproduce any real aircraft or proprietary interface. The device under test (DUT) is a deliberately simple simulator used to learn disciplined automation practices.

## Two ways to use this repository

1. **As a lab** — run the DUT, run the pytest harness, read the evidence. Start
   at [Setup](#setup).
2. **As a course** — a local browser-based learning environment that teaches
   this repository's own simulator, client, tests, evidence, and CI over 14 days.
   Start at [Learning environment](#learning-environment).

## What the starter system does

The repository contains a TCP server that behaves like a synthetic embedded component. The pytest harness:

1. Selects an available local port.
2. Launches the DUT as a child process.
3. Polls until the DUT reports `READY`.
4. Executes nominal, timing, malformed-input, and recovery tests.
5. Writes DUT logs and JUnit test results into `evidence/`.
6. Terminates or force-kills the DUT during cleanup.

## Requirements

- Python 3.11 or newer
- Git

## Setup

### PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
pytest -v
```

### Bash

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
pytest -v
```

## Learning environment

```powershell
python -m learning
```

Starts a server on `127.0.0.1` (random free port, `--port` to fix it) and opens
a dashboard that answers one question: **what should I do right now?**

Each lesson runs a short cycle — read a concise explanation, predict an outcome,
do real work in this repository, press **Verify**, then explain what happened.
Verification is not a checkbox: the server runs an allowlisted validator
(pytest node IDs, a protocol probe against a live DUT, an AST check on the file
you edited, a JUnit/CSV/log artifact check) and shows you the real command name,
stdout, stderr, exit status and duration. A lesson cannot complete while a
mandatory check is failing.

Progress is stored in `learning/.progress.json` (gitignored) and the next
`python -m learning` resumes exactly where you stopped.

| | |
|---|---|
| Days 1–14 | 40 lessons, ~9 h of guided work, every one validated by real behavior |
| Days 11–12 (C++ DUT, CMake/CTest, GDB) | need a C++ toolchain; the app detects one at startup and, if it is missing, shows the module as unavailable with install instructions rather than failing later |
| Interview mode | 35 concept-tagged questions, weighted toward your weak areas |
| Flow diagrams | simulator execution path, TCP request loop, requirement→evidence chain, CI pipeline, HWIL loop |

Maintainer documentation lives in [learning/README.md](learning/README.md).
The learning system adds **no runtime dependencies** — standard library, plain
HTML/CSS/JS, no build step — and is linted, type-checked and tested by the same
gates as the rest of the project.

## Run ten consecutive validation cycles

### PowerShell

```powershell
.\scripts\run-tests.ps1 -Repeat 10
```

### Bash

```bash
./scripts/run-tests.sh 10
```

The command should complete with no failed tests, no orphaned DUT process, and no occupied test port.

## Run the DUT manually

```bash
python -m simulator.simulator --host 127.0.0.1 --port 9000
```

In another terminal:

```python
from testlab.client import LabClient

with LabClient("127.0.0.1", 9000) as client:
    response = client.request({"command": "status", "sequence": 1})
    print(response.payload)
```

Expected response:

```json
{"sequence": 1, "state": "READY", "status": "ok"}
```

## Fault injection

The DUT can misbehave on purpose, which is what makes failure-diagnosis
practice possible. Faults are off by default — the nominal behavior above is
unchanged.

```bash
python -m simulator.simulator --port 9000 --fault delayed_response --fault-delay-ms 400
python -m simulator.simulator --port 9000 --fault-config faults.json
```

Presets: `delayed_response`, `dropped_connection`, `malformed_response`,
`startup_delay`, `process_termination`. A `--fault-config` JSON file takes the
same behavior field by field:

```json
{"response_delay_ms": 400, "drop_connection": false, "malformed_response": false,
 "startup_delay_ms": 0, "exit_after_requests": null}
```

Every engaged fault writes a `fault_injected` line to the DUT log, so evidence
always explains the failure. `tests/test_faults.py` verifies each mode against
`REQ-FAULT-001`.

## The C++ DUT

`cpp/` holds a second device under test that speaks the identical protocol, so
the harness can drive either implementation — the simulated-versus-real split
you meet in a hardware lab, in miniature.

```bash
python -m pip install -e ".[dev,cpp]"   # cmake + ninja from PyPI
cmake -S cpp -B cpp/build
cmake --build cpp/build
ctest --test-dir cpp/build -C Debug --output-on-failure
```

The compiler and debugger come from the platform (`g++`/`clang++`/MSVC and
`gdb`); on Windows, `winget install BrechtSanders.WinLibs.POSIX.UCRT` provides
both. Then point the existing suite at it:

```bash
pytest --dut cpp                        # or set FTL_DUT=cpp
```

`tests/test_cpp_dut.py` proves the two agree (REQ-CPP-001): the same bytes go
to both DUTs and the replies must match **byte for byte**, not merely decode to
equal objects. Those tests skip, rather than fail, when `cpp/build` is absent.

For debugging practice the C++ DUT can fail on demand — `--fault crash`,
`bad-access`, `hang`, `slow`, with `--fault-after N` (use `0` to fire during
startup, which makes a crash reproducible under `gdb -batch -ex run -ex bt`).

## Repository layout

```text
flight-test-lab/
├── .github/workflows/ci.yml
├── .gitlab-ci.yml
├── cpp/                       # the C++ DUT (same protocol, native)
│   ├── CMakeLists.txt
│   ├── include/dut/           # declarations: json, protocol, server, logging
│   ├── src/                   # definitions + main()
│   └── tests/                 # CTest cases for the protocol core
├── learning/                  # the learning environment (see learning/README.md)
│   ├── __main__.py            # python -m learning
│   ├── server/                # HTTP API, curriculum loader, progress store
│   ├── checks/                # allowlisted lesson validators
│   ├── curriculum/            # curriculum.json, modules/dayNN.json, interview.json
│   ├── flows/                 # execution-path diagram data
│   └── static/                # index.html, app.js, style.css
├── requirements/
│   ├── software_requirements.csv
│   └── traceability.csv
├── scripts/
│   ├── run-tests.ps1
│   └── run-tests.sh
├── simulator/
│   └── simulator.py
├── src/testlab/
│   └── client.py
├── tests/
│   ├── conftest.py
│   ├── test_protocol.py
│   ├── test_status.py
│   ├── test_cleanup.py
│   ├── test_faults.py
│   ├── test_cpp_dut.py        # Python ↔ C++ protocol parity
│   └── learning/              # tests for the learning environment
├── pyproject.toml
└── README.md
```

## Initial requirements

| ID | Requirement |
|---|---|
| REQ-COM-001 | The DUT shall respond to a valid status request. |
| REQ-COM-002 | The DUT shall respond to a valid status request within 250 ms. |
| REQ-PROTO-001 | The DUT shall reject an unsupported command without terminating. |
| REQ-PROTO-002 | The DUT shall reject malformed JSON without terminating. |
| REQ-REC-001 | The test harness shall terminate the DUT after the test session. |
| REQ-FAULT-001 | The DUT shall support configuration-driven fault injection. |
| REQ-CPP-001 | The C++ DUT shall implement the same protocol as the Python DUT. |

## Evidence

A normal test run creates:

```text
evidence/
├── junit/test-results.xml
└── logs/dut.log
```

Generated evidence is intentionally excluded from Git, except for placeholder files that preserve the directory structure.

## Validation

The gates CI runs, in order — all must exit 0:

```powershell
ruff check src simulator tests learning
ruff format --check src simulator tests learning
mypy src simulator learning
python -m pytest
ctest --test-dir cpp/build -C Debug --output-on-failure
python -m pytest --dut cpp tests/test_status.py tests/test_protocol.py
```

The last two need `cpp/build` to exist; without a C++ toolchain the Python
suite still passes in full and the C++ parity tests skip.

## Recommended next milestones

1. ~~Add configuration-driven fault injection~~ — done (`--fault`, `--fault-config`).
2. ~~Add a GitLab pipeline~~ — done (`.gitlab-ci.yml`).
3. ~~Add a C++ DUT with CMake/CTest that the harness can drive~~ — done (`cpp/`,
   `--dut cpp`, REQ-CPP-001).
4. Run the suite 100 consecutive times and inspect cleanup reliability.
5. Build the C++ DUT under AddressSanitizer so the `bad-access` fault is caught
   at the moment of the bad write rather than whenever it happens to crash.
6. Add structured anomaly reports and a formal requirement-to-result evidence
   manifest (configuration identity, tool versions, DUT implementation).
