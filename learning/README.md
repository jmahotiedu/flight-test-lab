# Learning environment — internals

A local, browser-based study environment that teaches this repository's own
code. It is a normal part of the project: stdlib only, strict-typed, linted and
tested by the same gates as `src/` and `simulator/`.

```powershell
python -m learning              # dashboard on 127.0.0.1, random free port
python -m learning --port 8899  # fixed port
python -m learning --no-browser # do not auto-open a browser
```

This document is for maintaining the learning system. Learners never need it —
the dashboard is self-explanatory.

## Layout

```text
learning/
├── __main__.py            # entry point: argparse, bind, print URL, serve
├── server/
│   ├── app.py             # HTTP handler, routing, JSON API
│   ├── curriculum.py      # loader + schema validation (fails loudly)
│   ├── progress.py        # atomic learner state, completion rules, mastery
│   ├── toolchain.py       # detects cmake/ctest/compiler/gdb for native modules
│   └── validators.py      # allowlisted validator registry + result shape
├── checks/                # the six validator implementations
│   ├── python_probe.py    # run a snippet, match stdout against a regex
│   ├── pytest_check.py    # run node IDs in a sandbox, parse exit + JUnit
│   ├── behavior_probe.py  # launch either DUT, speak the protocol, assert behavior
│   ├── source_check.py    # AST/text assertions on learner-edited files
│   ├── artifact_check.py  # logs, CSV rows, JUnit testcases on disk
│   └── toolchain_check.py # cmake/ctest/gdb, and gdb attached to a live DUT
├── curriculum/
│   ├── curriculum.json    # 14 days, concept taxonomy, module order
│   ├── modules/dayNN.json # lesson definitions (the actual content)
│   └── interview.json     # 30 concept-tagged interview questions
├── flows/*.json           # clickable execution-path diagrams
├── static/                # index.html, app.js, style.css (no build step)
└── .progress.json         # learner state — gitignored, never committed
```

## Data model

A **lesson** is a JSON object in `curriculum/modules/dayNN.json`:

| Field | Meaning |
|---|---|
| `id`, `title`, `objective` | identity and one-line purpose |
| `estimated_minutes` | shown before the learner commits attention |
| `status` | `available` (default) or `unavailable` |
| `unavailable_reason` | required when `unavailable`; surfaced in the UI |
| `requires` | native capabilities (`cpp-build`, `gdb`); unmet ones force `unavailable` with a generated reason |
| `concepts[]` | mastery categories this lesson feeds |
| `prerequisites[]` | lesson IDs; must resolve and point backwards |
| `source_files[]` | real repo files the lesson is about |
| `flow` | optional key into `flows/` for the diagram panel |
| `requirements[]` | REQ IDs this lesson touches |
| `blocks[]` | the ordered lesson cycle (below) |
| `hints[]` | `{level, text}`, revealed one at a time on request |

**Blocks** implement Learn → Predict → Do → Verify → Explain:

| `type` | Required keys | Behavior |
|---|---|---|
| `learn` | `text` (+ optional `more`) | short explanation, expandable |
| `predict` | `question`, `options`, `answer_index`, `reveal` | answered before the truth is shown |
| `do` | `instructions` (+ optional `command`) | real work in the real repo |
| `verify` | `validator` (+ `args`, `mandatory`) | runs a registry validator |
| `quiz` | `question`, `options`, `answer_index` | scored server-side |
| `explain` | `question`, `keywords`, `sample_answer` | free-text, keyword-scored |

The loader (`server/curriculum.py`) validates all of this at startup and raises
`CurriculumError` with the offending file path. A typo in a lesson file stops
the server with a readable message rather than producing a half-broken UI.

## Completion rules

`POST /api/complete` returns **409** unless, for that lesson:

- every `mandatory` verify block has a **passing** validation on record, and
- every `quiz` block has been answered correctly, and
- every `explain` block has been answered.

Completion is a server-side derivation over recorded evidence
(`ProgressStore.lesson_completion`), not a flag the browser can set. Clicking
through cannot finish a lesson; `tests/learning/test_progress.py` pins this.

## API

Everything the browser can do:

| Method | Path | Purpose |
|---|---|---|
| GET | `/`, `/static/*` | SPA shell and assets (path-traversal guarded) |
| GET | `/api/state` | progress summary + resume target |
| GET | `/api/curriculum` | roadmap: days, lessons, locked/unavailable flags |
| GET | `/api/lesson/{id}` | one lesson's content |
| GET | `/api/flow/{name}` | flow-diagram data |
| GET | `/api/mastery` | per-concept strengths and weaknesses |
| GET | `/api/interview` | next interview question (weak concepts first) |
| POST | `/api/step` | record a predict/quiz/explain answer |
| POST | `/api/hint` | reveal one hint level, record usage |
| POST | `/api/validate` | run **this lesson's** validator |
| POST | `/api/complete` | complete a lesson, or 409 with the missing gates |
| POST | `/api/interview/answer` | score an interview answer |

## Safety properties

These are requirements, and `tests/learning/test_server.py` enforces them:

- The server binds `127.0.0.1` only.
- **There is no command-execution endpoint.** `POST /api/validate` carries a
  lesson ID and nothing else; the validator name and its arguments come from
  the curriculum files on disk. The browser cannot supply a command, a path,
  or code.
- Validators are a fixed registry of six names. An unknown name is a `KeyError`
  on the server, never a subprocess. `toolchain_check` narrows further to four
  allowlisted executables, so even a curriculum edit cannot make it launch an
  arbitrary program.
- Every subprocess: argv arrays (never `shell=True`), `cwd` = repo root,
  bounded timeout (default 30 s, hard cap 120 s), killed and reaped on timeout.
- Destructive lessons (Day 6 broken cleanup, Day 8 faults, Day 10 red test) run
  against generated sandboxes with `EVIDENCE_DIR` redirected — the learner's
  real `evidence/` is never clobbered.
- Static file serving resolves paths and rejects anything outside
  `learning/static`.
- Progress writes are atomic (temp + `os.replace`); a corrupt file is backed up
  to `.corrupt.json` and reset instead of crashing the server.

## Validator results

Every validator returns the same structure, rendered verbatim in the UI:

```json
{
  "name": "pytest_check", "passed": false, "exit_status": 1,
  "stdout": "...", "stderr": "...", "duration_ms": 2143,
  "timed_out": false, "interpretation": "pytest exited 1 — 1 test failed",
  "details": {}
}
```

Failures show the real stderr. A validator never reports success on the basis
of an exit code alone when an artifact can be checked as well — `pytest_check`
parses the JUnit XML, `behavior_probe` asserts on protocol responses,
`artifact_check` reads the file that was supposed to be produced.

## Adding a lesson

1. Add the lesson object to the right `curriculum/modules/dayNN.json`.
2. Point `prerequisites` at an earlier lesson ID.
3. Give it at least one gate: a `mandatory` verify block, a quiz, or an explain
   block (`tests/learning/test_curriculum.py::test_every_lesson_gates_on_real_work`).
4. Use an existing validator name; add a new one only by extending
   `_build_registry()` in `server/validators.py`.
5. Run `python -m pytest tests/learning` — the curriculum tests validate schema,
   ID uniqueness, prerequisite resolution, and validator existence.

## Native modules and capability gating

Days 11–12 teach the C++ DUT in `cpp/` and debugging it with GDB, so they need
tools the platform cannot provide for itself. Rather than a hard-coded status,
those lessons declare what they need:

```json
"requires": ["cpp-build", "gdb"]
```

At load time `server/toolchain.py` looks for `cmake`, `ctest`, a C++ compiler
and `gdb` — PATH first, then the active virtualenv (`pip install cmake ninja`
puts them there), then the usual Windows install locations, with `FTL_CMAKE` /
`FTL_CTEST` / `FTL_CXX` / `FTL_GDB` as overrides.

The compiler probe looks for `g++`, `clang++` or `c++` specifically, not `cl`.
That is deliberate: Day 12 reads DWARF debug info with GDB, which an MSVC build
does not produce. An MSVC-only machine can build `cpp/` by hand perfectly well
(`cmake -S cpp -B cpp/build` will find Visual Studio), but the native lessons
stay locked until a GNU-style toolchain is present, because that is what their
validators actually exercise. On Windows,
`winget install BrechtSanders.WinLibs.POSIX.UCRT` provides both g++ and gdb.

One Windows caveat worth knowing: MinGW's linker breaks on toolchain paths
containing spaces, so a MinGW install under `C:\Users\First Last\...` fails to
link with `file format not recognized`. Install it somewhere space-free
(`C:\dev\mingw64`) if you hit that. A lesson whose requirements
are unmet is marked unavailable **with the install command in its reason**, and
`POST /api/validate` refuses it with 409. Install a toolchain, restart
`python -m learning`, and the same lessons unlock with no content change.

This is why the curriculum tests inject a `Toolchain` rather than reading the
machine: both directions — offered with a compiler, withheld and explained
without one — are pinned in `tests/learning/test_toolchain.py`.

The native validators run through the same allowlist discipline as everything
else. `checks/toolchain_check.py` will start `cmake`, `ctest`, `gdb` or the
built `dut` binary and nothing else; the tool name comes from the lesson file,
never the request, and `gdb-attach` (which needs a live wedged process to
inspect) always kills its child in a `finally` block.

## Tests

`tests/learning/` covers curriculum integrity, progress persistence and
completion rules, HTTP API behavior and safety, validator semantics (timeout,
sandboxing, truthful failures), toolchain detection and capability gating in
both directions, and a frontend contract check that every endpoint `app.js`
calls actually exists on the server — plus a rendering check that the code
fences in lesson text survive the client's markdown handling intact.
