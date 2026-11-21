# Blast Radius

A test-selection submission for the [Blast Radius buildathon](https://www.joinbuilders.org/buildathon/tracks/blast-radius), targeting sqlglot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

The repository-root Dockerfile is the submission entry point. It runs without a network, as nobody, with a read-only filesystem. Output is one JSON line per patch; diagnostics go to stderr.

## Selection

The committed plain-JSON map contains measurements from the unmodified baseline: per-test and per-task coverage, class and attribute usage, subprocess coverage, initializer dependencies, operand types and bounded primitive values. It contains no patch IDs, mutation outcomes, or failing-test labels.

The default selection policy prioritizes avoiding omissions. It reconstructs each independent patch and keeps the entire changed function's baseline coverage for ordinary body replacements. All baseline source-reading tests are retained as well, because inspecting a function or cached metadata need not execute its edited body. It does not narrow to only the edited statement.

Source-file reads are recorded separately from execution coverage. A test that reads a module's source can inspect metadata from an already cached import without executing the changed statement. These dependencies are always retained, including for AST-equivalent edits, and cannot be cleared by probes whose virtual loader leaves the original files on disk.

Known fixtures retain every recorded reader, including all members of a class whose setup loads that fixture. A separate baseline pass records shared pytest fixture setup and finalizers in their actual class/module/session scope; this corrects dependencies previously attributed only to the first consuming test. The same expansion applies to function coverage, class users and attribute users. Without these scope measurements, whole-function and fixture reductions stay disabled. Unknown or empty fixture mappings keep the full inventory. A scalar class setting can use all class/attribute users only when both old and new values are literals of the same type, the attribute was observed at runtime, and initialization measurements show no import-time read. Unmapped settings, dictionaries, expressions, changed annotations and special attributes stay broad.

Lambda definitions and calls can share a source line. Separate callable-entry measurements from baseline collection/imports distinguish them; callbacks actually invoked during initialization, and callbacks without complete call measurements, keep the full inventory.

The full provided test inventory is selected for source-only/AST-equivalent edits, unknown resources, definitions, import-sensitive configuration, import-time execution, new bindings, inserted code, incomplete patch parsing and missing coverage. This preserves the organizers' warning that a comment-only change is not promised to be harmless. Every git file section must be accounted for; duplicate file sections and unsupported file-mode/copy changes are treated as uncertainty.

The default does **not** remove tests based on observed operand values, supposedly unread dictionary values, or passing runtime probes. These analyses from the earlier submission remain available only through `--selection precise` for research and comparison; they are disabled in the submission's default mode.

`--selection complete` is an independent diagnostic control. It schedules every number in the supplied test inventory for every patch, without reading patch contents or requiring a baseline checkout. Under the published scoring rules this cannot omit a listed failing test, but its simulated cost is substantially higher. It is intended to distinguish selector omissions from a submission or scoring problem.

The scheduler tries one through eight workers and minimizes the scoring formula in integer milliseconds. Unknown inventory entries are retained. The full-inventory fallbacks and broader dependencies deliberately trade simulated speed for recall; a zero-miss result on the hidden set still requires a new practice run.

## Verification

```sh
make test
make sample
make check
python3 tools/regressions.py
python3 tools/benchmark.py
```

Docker and Python are required. `make sample` builds `linux/amd64` and uses the scorer's no-network, read-only, nobody-user, 2-CPU, 4-GB, 256-process and 64-MB-temporary-storage restrictions.

The verification workflow requires valid output with zero sample misses, checks whole-file import failures, failed subtests, metadata and callback regressions, shared fixture readers and comment-only coverage, and measures three runs. The official score uses the median total.

The confirmed zero-miss submission is preserved on `checkpoint/zero-miss-e2f23d8`. The first optimization checkpoint restores known-fixture readers, whole-function coverage and measured runtime-only scalar settings. It keeps comment-only and import-time fallbacks and leaves passing-test probes disabled. Local independent audits also ran 5,801 omitted tests against actual patched source/data with no failures; hidden correctness must be confirmed by another scored run before further narrowing.

The sample kit has 30 patches. The starter's simulated cost on this same set is **1,210.470 seconds**. Latest measured results are available in the workflow's `sample-output` artifact. These are sample measurements, not a guarantee about hidden practice or final scores.

## Rebuild baseline maps

The **Rebuild baseline coverage** workflow supports separate coverage, type/value, import, shared fixture and dictionary collection. The Docker image used for collection is the organizer's pinned Python image, with the supplied dependency lock. All collectors reject failed baseline runs.

```sh
python3 tools/fetch_baseline.py baseline
python3.13 -m venv .venv
.venv/bin/python -m pip install -r tools/requirements.lock coverage==7.13.5
.venv/bin/python tools/collect.py baseline maps/coverage.json
.venv/bin/python tools/collect_types.py baseline profiles/types.json
.venv/bin/python tools/collect_imports.py baseline profiles/imports.json
.venv/bin/python tools/collect_children.py baseline profiles/children.json
.venv/bin/python tools/collect_dicts.py baseline profiles/dicts.json
.venv/bin/python tools/collect_shared.py baseline profiles/shared.json
.venv/bin/python tools/merge_maps.py maps/coverage.json profiles/types.json profiles/imports.json profiles/children.json profiles/dicts.json profiles/shared.json
```

Create `profiles/` before running these local commands. [Provenance](maps/provenance.json) records the source pin and collection environments. The source archive is checksum-verified. Integration tests are enabled during collection. Raw coverage databases and profiling scratch files are not included in the submission image.

To run selection locally, use the same Python environment:

```sh
.venv/bin/python selector.py --source baseline --data sample > output.jsonl
.venv/bin/python tools/score.py sample sample/answers.json output.jsonl
```

## Submit

Use the public repository URL and the full commit SHA of a passing **Verify submission** run. No API key, hosted service or separate release is required.

Hidden runs reveal a miss count, not the failing tests. The independent regression audit found and repaired import-time and cached-callback gaps, but only a new practice run can establish the hidden score. The approach is conservative impact analysis, not a proof of global optimality for arbitrary programs.

The sample data, scorer, scoring rules and dependency lock come from the organizers. SQLGlot is fetched from its pinned upstream repository and retains its upstream license. `sample/answers.json` is used only for offline evaluation and is excluded from the Docker build context.
