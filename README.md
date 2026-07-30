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

## Repository layout

```text
flight-test-lab/
├── .github/workflows/ci.yml
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
│   └── test_status.py
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

## Evidence

A normal test run creates:

```text
evidence/
├── junit/test-results.xml
└── logs/dut.log
```

Generated evidence is intentionally excluded from Git, except for placeholder files that preserve the directory structure.

## Recommended next milestones

1. Add configuration-driven fault injection such as delayed responses and dropped connections.
2. Run the suite 100 consecutive times and inspect cleanup reliability.
3. Replace the Python DUT with a C++ implementation while preserving the protocol.
4. Add CMake, CTest, GDB exercises, and structured anomaly reports.
5. Add a GitLab pipeline and a formal requirement-to-result evidence manifest.
