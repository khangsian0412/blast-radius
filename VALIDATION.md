# Validation and limits

The reference is the user-confirmed hidden submission at `37e9b6b0c17084270ad74856f328908c8d4ab8e8`: approximately 700 seconds, with no missed commits. That exact tree remains available on `checkpoint/zero-miss-700-37e9b6b`.

Optimization is checked against the organizers' **30-patch public sample** and independent patched-source regressions. Public sample times cannot predict the hidden score.

Latest verified result: **102.846 seconds total**, consisting of **96.953 simulated seconds + 5.893 CPU seconds**. All three runs had zero misses and identical output. [Linux verification and artifacts](https://github.com/khangsian0412/blast-radius/actions/runs/44997431412). The under-100-second total target has not been reached.

## Iteration record

Each row required zero public sample misses before advancing. Static rows exclude verification CPU and are not official total scores.

| Checkpoint | Public simulated seconds | Measured median total seconds |
| --- | ---: | ---: |
| Confirmed conservative policy, public sample | 527.692 | 528.872 locally |
| Preserve actual comment/code observers | 329.528 | — |
| Guard expression-entry coverage | 326.660 | — |
| Validate literal settings against real imports | 243.662 | — |
| Bounded primitive comparisons | 220.885 | — |
| Verify canonical enum operands | 198.593 | — |
| File-specific source dependencies | 180.942 | — |
| Side-effect-free closure factories | 146.712 | — |
| Immutable dictionary zero-read proof | 136.394 | — |
| Verified child observers and unreachable entries | 119.200 | — |
| Reflective scalar-setting consumers | 111.069 | — |
| Complete tests on real patched files | 94.960 | 112.971 |
| Separate source inspection from lazy imports | 96.953 | 107.438 |
| Fresh child checks with shared external imports | 96.953 | 104.237 |
| Avoid syntax copies and combine compatible checks | 99.146 | 103.989 |
| Preload external plugins; retain fresh checks after late imports | 96.953 | 103.592 |
| Collect external startup garbage before forking | 96.953 | **102.846** |

Three-run reports, outputs and resource measurements are published in the **Verify submission** workflow's `sample-output` artifact. Total time is simulated cost plus measured container CPU, including descendants. The wrapper's small measurement overhead is included.

## Independent checks

The optimization audit executed **48,514 omitted-test checks** against actual patched files without a failure:

- 31 independent mutations exercise comments, short-circuit entry coverage, class settings, primitive comparisons, closure factories, unread dictionaries, unreachable entries and reflective scalar settings.
- Source-reader refinements were checked on 36 additional sample-delta cases.

These audits are separate from sample answer scoring. Audit scratch data and mutation outcomes are not runtime inputs and are excluded from the image.

The automated container regressions also check import-time failures, cached callbacks, source inspection, shared fixture consumers, native failed subtests, actual on-disk patched source and isolation between consecutive requests. All checks run with the scorer's offline, read-only, nobody-user and resource restrictions. The unit suite contains 111 tests.

## Lower bound and submission

Offline calculation using the provided public answers gives a **94.351-second simulated lower bound** for this sample. For each patch, a schedule achieves the worker-count lower bound. This excludes all CPU required to determine the tests and does not apply to the hidden patch set. Neither the answers nor this calculation are included in the image.

The final hidden result must be obtained from a scored practice run. A zero-miss public run and extensive mutation checks provide evidence, not a guarantee that every hidden dependency is modeled. The confirmed hidden zero-miss checkpoint remains available for rollback.
