# Blast Radius

A test-selection submission for the [Blast Radius buildathon](https://www.joinbuilders.org/buildathon/tracks/blast-radius), targeting SQLGlot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

The repository-root Dockerfile is the entry point. It runs offline, as nobody, with a read-only filesystem, and writes one JSON line per independent patch. Diagnostics go to stderr.

## Selection

The default `optimized` policy combines baseline dependency measurements with guarded source analysis. The plain JSON map records coverage, shared fixtures, class and attribute consumers, operand types, source/code observers, subprocesses and initialization dependencies. It contains no patch identities, mutation outcomes or failing-test labels.

Ordinary body changes retain whole-function coverage unless statement structure, bindings and initialization checks permit using the entry coverage of the changed statement and its enclosing expressions. Primitive-expression equivalence requires measured builtin types and bounded operands; enum comparisons also require canonical members. Unknown or incomplete evidence keeps broader coverage.

Source inspection is recorded separately from ordinary imports. Genuine source readers, cached source snapshots, code-object observers and unverified child programs remain dependencies, including for comment-only changes. Import-time execution, changed compiler diagnostics, unsupported definitions, new bindings and incomplete patch parsing retain the full inventory. Every file section is analyzed.

Shared setup and finalizers expand dependencies to all consuming tests. Class settings retain measured consumers and normally require successful imports and full collection against real patched files. Narrower exceptions cover certified scalar readers and existing immutable dictionary values with **zero** baseline reads; dictionaries are never narrowed to individual key readers. Closure-factory handling permits only literal argument changes to factories whose only work is returning an undecorated closure.

Small selections can undergo complete runtime test checks. Tests are removed only after successful collection, a passing root call and successful teardown. Failed native subtests remain failures; uncertain, skipped, timed-out or unexecuted tests remain selected. Checks never skip subtests or alter multiprocessing behavior. Resource patches and tests with baseline pool tasks bypass these checks.

Each check imports application code from a fresh physical patched directory. Unchanged baseline bytecode is reusable, but every edited module's caches are excluded. On Linux, external dependency imports are shared through a parent that never imports SQLGlot or tests; each request gets a fresh child. CPU accounting includes descendants, checks have deadlines and budgets, and failures retain tests.

The scheduler tries one through eight workers using the published integer-millisecond cost formula.

Diagnostic modes:

- `--selection conservative`: broader whole-function/source-reader selection.
- `--selection complete`: every supplied test for every patch.
- `--no-probe`: disable passing-test checks; required collection guards remain.
- `--selection precise`: legacy research mode, not the submission default.

## Verification

```sh
make test
make sample
make check
python3 tools/regressions.py
python3 tools/benchmark.py
```

Docker and Python are required. Containers use the scorer's 2-CPU, 4-GB, read-only, offline, nobody-user, 256-process and 64-MB temporary-storage limits. The verification workflow requires zero sample misses, exercises import failures, cached callbacks, source readers, shared fixtures, failed subtests and isolation between requests, then measures three runs. See [validation notes](VALIDATION.md) for results and limits.

## Rebuild measurements

The **Rebuild baseline coverage** workflow supports coverage, type/value, dictionary, import, shared-fixture, observer, enum and reflective-scalar passes. Each collector rejects failed baseline runs. [Provenance](maps/provenance.json) records the source pin and collection environments. Source archives are checksum-verified and integration tests are enabled. Raw profiling scratch files are excluded from the image.

For local selection after fetching the pinned baseline and installing `tools/requirements.lock`:

```sh
python3 tools/fetch_baseline.py baseline
python3 selector.py --source baseline --data sample > output.jsonl
python3 tools/validate.py sample output.jsonl --require-no-misses
```

## Submit

Use the public repository URL and full commit SHA of a passing **Verify submission** run. No hosted service, API key or separate release is required. The user-confirmed zero-miss version is preserved on `checkpoint/zero-miss-700-37e9b6b`.

Sample validation cannot establish hidden recall or hidden execution time: a scored practice run is still required. This is measured impact analysis, not a proof that arbitrary programs cannot have unseen dependencies.

The sample data, scoring rules and dependency lock are supplied by the organizers. SQLGlot is fetched from pinned upstream source and retains its upstream license. Sample answers are used only for offline evaluation and are excluded from the Docker build context.
