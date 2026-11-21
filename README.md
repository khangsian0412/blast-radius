# Blast Radius

A test-selection submission for the [Blast Radius buildathon](https://www.joinbuilders.org/buildathon/tracks/blast-radius), targeting sqlglot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

The repository-root Dockerfile is the submission entry point. It runs without a network, as nobody, with a read-only filesystem. Output is one JSON line per patch; diagnostics go to stderr.

## Selection

The committed plain-JSON map contains measurements from the unmodified baseline: per-test and per-task coverage, class and attribute usage, subprocess coverage, initializer dependencies, operand types and bounded primitive values. It contains no patch IDs, mutation outcomes, or failing-test labels.

The selector reconstructs each independent patch in memory. It combines changed-statement coverage with enclosing Boolean expressions, inherited class usage, and initialization dependencies. Lambda bodies are analyzed as executable code rather than configuration reads. New bindings, generator conversions, new definitions and unsupported changes receive conservative coverage.

Two additional analyses avoid unnecessary work:

- An abstract interpreter proves certain comparison rewrites equivalent over the baseline's observed operand domains. Custom equality, unknown values and untraced subprocess execution block those proofs.
- Changes to existing, immutable literal dictionary values can be ignored when the affected entries were never read. Changed keys, observed value reads, mutation and external escapes prevent this optimization.

Import-sensitive changes are checked in an isolated process. **Any import failure retains the complete test inventory**, including tests that would fail before their bodies run. This addresses the coverage gap where a class change breaks module imports but ordinary coverage selects only callers of that class.

Small selected tests can be verified directly. Expensive optimizer tasks can be filtered only when complete task coverage is available and the change has no initializer or parent-process dependency. Unknown tasks execute normally. Failed subtests count as failures even when pytest later reports the root call as passed. Opaque subprocess tests are retained rather than checked against an unpatched child.

Checks have CPU and wall-time budgets. A timeout or incomplete check retains unresolved tests. The scheduler then tries one through eight workers and minimizes the scoring formula in integer milliseconds.

## Verification

```sh
make test
make sample
make check
python3 tools/regressions.py
python3 tools/benchmark.py
```

Docker and Python are required. `make sample` builds `linux/amd64` and uses the scorer's no-network, read-only, nobody-user, 2-CPU, 4-GB, 256-process and 64-MB-temporary-storage restrictions.

The verification workflow requires valid output with zero sample misses, checks whole-file import failures and failed subtests, and measures three runs. CPU-bounded verification may retain different unresolved tests under different timing conditions; every run must remain valid. The official score uses the median total.

The sample kit has 30 patches. The starter's simulated cost on this same set is **1,210.470 seconds**. Latest measured results are available in the workflow's `sample-output` artifact. These are sample measurements, not a guarantee about hidden practice or final scores.

## Rebuild baseline maps

The **Rebuild baseline coverage** workflow supports separate coverage, type/value, import, and dictionary collection. The Docker image used for collection is the organizer's pinned Python image, with the supplied dependency lock. All collectors reject failed baseline runs.

```sh
python3 tools/fetch_baseline.py baseline
python3.13 -m venv .venv
.venv/bin/python -m pip install -r tools/requirements.lock coverage==7.13.5
.venv/bin/python tools/collect.py baseline maps/coverage.json
.venv/bin/python tools/collect_types.py baseline profiles/types.json
.venv/bin/python tools/collect_imports.py baseline profiles/imports.json
.venv/bin/python tools/collect_children.py baseline profiles/children.json
.venv/bin/python tools/collect_dicts.py baseline profiles/dicts.json
.venv/bin/python tools/merge_maps.py maps/coverage.json profiles/types.json profiles/imports.json profiles/children.json profiles/dicts.json
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
