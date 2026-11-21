# Blast Radius — baseline impact analysis

A submission for the [Blast Radius buildathon track](https://www.joinbuilders.org/buildathon/tracks/blast-radius). The Docker image selects tests for independent patches to sqlglot commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` and assigns them to at most eight workers.

The runtime is Python standard library only. It does not execute patches, install packages, access the network, or write to disk. All logs go to stderr; stdout contains only the required JSON lines.

## Validation results

All 30 supplied sample commits are accepted by the official scorer with **zero missed commits**.

| Measure | Result |
| --- | ---: |
| Starter simulated cost | 1,614.120 seconds |
| This selector's simulated cost | **186.316 seconds** |
| Reduction in simulated cost | **88.5%** |
| Unit tests | 20 passed |
| Three container runs | Identical JSON output |
| Container CPU per sample batch | About 0.8 seconds |
| Peak memory | About 47 MiB |
| JSON output | 4,399 bytes |

CPU and memory come from the Linux verification workflow's measurement wrapper. The official score adds runtime CPU and is evaluated on hidden patches; **186.316 is the sample simulated cost**, not an official leaderboard score.

## Approach

1. Collect per-test line coverage, fixture reads and class-construction dependencies from the unmodified pinned baseline. Record unittest class setup dependencies for every test in that class. Inline process-pool tasks during collection with their pickle round trips preserved, so spawned work and serialization do not disappear from coverage.
2. Parse each unified diff independently and reconstruct changed Python files in memory. Python AST equality filters syntax-equivalent edits. SQL fixtures use observed file-read dependencies.
3. Select tests covering the changed statements and the entry to enclosing short-circuit expressions. Class-level changes include tests using the class and its subclasses. Import-time changes use a reverse import graph. Unrecognized resources, syntax errors, missing coverage and unsupported diffs fall back conservatively. Unknown inventory tests and tests launching untraced subprocesses are retained when relevant.
4. Translate coverage by pytest node ID to the supplied test numbers. Try one through eight workers using longest-processing-time-first scheduling, then choose the smallest `max(load) + 2 seconds × workers` cost in integer milliseconds.

The maps are baseline measurements, not answers for patches. There are no patch IDs, patch hashes, mutation-to-test tables, or sample answer lookups in the selector. `sample/answers.json` is used only by offline validation and is excluded from the Docker build context.

## Run

Docker and Python 3.9+ are needed for the sample check. The image itself uses the organizer's pinned Python 3.13 image.

```sh
make test
make sample
make check
```

`make sample` builds for `linux/amd64`, runs with the scorer's read-only filesystem, no network, 2 CPUs, 4 GB RAM, 64 MB temporary storage and nobody user, then evaluates the output with the official scorer.

To run without Docker, fetch the baseline and use Python 3.10+:

```sh
python3 tools/fetch_baseline.py baseline
make local SOURCE=baseline
```

No dependencies are required for selection or the unit tests. The exact scoring rules are in [SCORING.md](SCORING.md). Sample scores are validation results, not predictions of the hidden final score.

## Rebuild the coverage map

The **Rebuild baseline coverage** GitHub Actions workflow collects coverage inside the organizer's exact Linux image and uploads `baseline-coverage`. The committed map was collected in that environment: **1,620 tests and 25,440 subtests passed, with one test skipped**. [Collection provenance](maps/provenance.json) records the source, image, generating commit, workflow run and artifact hash. This workflow is separate from image builds: normal image builds download only the pinned sqlglot source and use the committed map.

Locally, using Python 3.13:

```sh
python3 tools/fetch_baseline.py baseline
python3 -m venv .venv
.venv/bin/python -m pip install -r tools/requirements.lock coverage==7.13.5
.venv/bin/python tools/collect.py baseline maps/coverage.json
```

Collection must finish without baseline test failures. Do not collect from patched source. Baseline dependencies, including integration tests, are included; `SKIP_INTEGRATION=0` is set by the collector. The source archive's SHA-256 is verified before extraction.

## Submission

Submit this public repository URL and the full commit SHA of a green **Verify submission** run. The repository-root Dockerfile is the submission entry point. No release, hosted service, API key or secret is needed.

The verification workflow runs unit tests and the actual Linux container under runtime limits, then requires valid output and zero missed sample commits. It also checks three deterministic runs within the 180-second limit. Its artifact includes the output, validation report and runtime measurements.

## Limitations

Coverage measures paths exercised by the baseline tests. It cannot prove that arbitrary new behavior is harmless. Conservative fallbacks trade extra work for recall; hidden patches may expose dependencies absent from baseline measurements. Selection never reads sample answers at runtime.

The sample kit, requirements lock, scoring rules and scorer are supplied by the challenge organizers. sqlglot source is fetched from its pinned upstream repository and retains its upstream license. The selection implementation and collection tooling are original to this repository.
