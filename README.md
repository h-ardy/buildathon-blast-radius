# Blast Radius selector (Builders Buildathon, RWX track)

For each patch against sqlglot at `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, `select.py` prints the
tests that can fail and splits them across workers.

## How it decides

All maps in `map/` were built once from the pinned commit (never from patches):

- `map.json.gz`
  - per-test line coverage of `sqlglot/` (pytest-cov with per-test contexts). The optimizer and
    executor tests normally run sqlglot in spawned worker processes; for mapping, those pools ran
    in-process with the same pickle round trip, so their code is attributed to the right tests.
  - per-test reads of class attributes (`Dialect.WEEK_OFFSET`, `Parser.FUNCTIONS`, ...), recorded
    with a descriptor on every class attribute of every sqlglot class.
  - which tests read each fixture file (recorded opens, setUpClass loads count for the whole
    class, plus fixture names written in test code).
  - which tests instantiate each dialect, the classes each dialect is built from, and the module
    import graph.
- `src.tar.gz`: the pinned `sqlglot/` and `tests/` Python sources, used to parse the old file.

Per changed file:

- `sqlglot/**.py`: same AST before and after (comments, blank lines) means nothing runs. A file
  that no longer compiles runs everything. Otherwise each changed statement maps to the tests
  whose coverage touches it. Statements that run at import time are traced instead: a class
  attribute goes to the tests that read it in that class or a subclass, including attributes
  built from it in the class body or by the metaclass; a module constant goes to the statements
  and class attributes that use it, in its module and in modules importing it.
- `tests/fixtures/**`: tests that read the fixture.
- `tests/**.py`: the tests around the change.
- other files: nothing.

Changes that run at import time (module constants, class bodies, decorators, code that runs while
sqlglot is imported) are also checked by importing the patched sqlglot, every module and every
dialect, in a subprocess. If that fails, every test runs.

## Running tests here

Running sqlglot's tests inside the container is allowed and charged as CPU time, and the result
is exact for the tests that run. `runner.py` runs a list of tests in one process against the
patched sources (sqlglot modules are served from memory, changed test files and fixtures go into
a temporary overlay; the tests' process pools run in-process with the same pickle round trip).
The image installs the starter kit's `requirements.lock`, so the environment matches the one the
commits were labeled in.

- Small selections (estimated run cost under half of what they would cost as CI time) are run
  here and only the failing tests are printed.
- When the selection holds a big test (5 s or more of recorded time, in practice the TPC-DS
  executor test), the cheap selected tests run first. If any fails, everything selected is
  printed. If all of them pass, the big test is left out. The tests that cannot be run faithfully
  in one process (lazy-load subprocess check, memory measurement, sqlglotc build, max-nodes) are
  always printed when selected.

Tests are then packed longest-first onto 1 to 8 workers, picking the count with the lowest
busiest-worker time plus 2 s per worker.

## Building the maps

`tools/` holds the scripts that produced `map/` (pytest plugins `syncpool.py`, `trackplug.py`,
`attrtrack.py`, `importtrace.py` and `build_map.py`). They were run with the starter kit's
`requirements.lock`, Python 3.13 and `SKIP_INTEGRATION=0`. `mutate.py`, `mutate_exp.py` and
`fidelity.py` are the validation harnesses: they make random mutations at the pinned commit, run
the real test suite, and check that the selector never leaves out a failing test.

## Prior code

None. Written during the event. MIT License.
