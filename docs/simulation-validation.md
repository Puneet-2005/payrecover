# Phase 4 acceptance record — 2026-09-08

Simulation only. No real payment authorization, money movement or causal uplift claim.

The interrupted turn's process handle and disposable acceptance database were no longer available.
A new isolated PostgreSQL 17 Testcontainer database ran the unchanged harness with run key
`acceptance-50000`, scenario `recovery-demo-v1`, seed 42 and size 50,000. No duplicate coordinator
was launched against an active run. The acceptance container was removed after successful completion.

Source digest: `98014630ea34f936a48ea8011220ded23cfae4c6bafacc007973b7deaecc1ec6`.
Measured end-to-end scenario runtime, excluding database setup/migration: **552.935 seconds**.
Other validation ran concurrently; this is an observed wall-clock duration, not a throughput target
or an isolated performance benchmark. Every delivery used the existing ingestion service.

## Recorded results

| Measure | Result |
| --- | ---: |
| Base generated events | 50,000 |
| Unique persisted events, including history fixtures | 50,016 |
| Deliveries | 50,512 |
| Explicit duplicate deliveries | 496 |
| Detection TP / FN / FP / TN | 1 / 1 / 0 / 10 |
| Insufficient classifications (included above) | 4 |
| Recall | 1 / 2 |
| False-positive rate | 0 / 10 |
| Detected episode delay, including completion and ingestion delays | 1,320 seconds |
| Initial eligible / blocked population payments | 44 / 39 |
| Simulated attempts | 68 |
| Explicit duplicate dispatches suppressed | 68 |
| Uncertain attempts remaining | 8 |
| Population payments | 83 |
| Conflicting-amount identities excluded from monetary totals | 6 |
| Already-successful / simulated-recovered / unresolved payments | 9 / 26 / 42 |

Initial blockers: integrity 5, missing consent 5, stored success 5, terminal/unknown 12,
unplanned 5, unresolved status 4, unsupported flow 3. These total 39. Stored-success classification
at the final horizon includes later history and is therefore distinct from initial eligibility.

Exact accounting in integer paise:

```text
805622 total = 92325 already-successful + 274356 simulated-recovered + 438941 unresolved
```

The six conflicting-amount identities have indeterminate value, not zero value. They are counted
but excluded from the monetary identity. Unplanned payments remain in the population. The sparse
labelled degradation was not detected and remains a false negative, rather than being dropped from
recall. Completed-run replay returned the identical historical report (`COMPLETED_REPLAY_EQUAL True`).

## Built-image verification retained from the uninterrupted validation output

`payrecover:phase4` image ID:
`sha256:73a8e92233fa954e8d78de17dd03cb2cb604732f2b437a7d7b07f1c263277724`.
Its manifest source digest matches the acceptance digest above. On a separate disposable database,
`python -m payrecover.simulation_cli initialize` exited 0. Both the 1,000-event scenario command and
completed replay exited 0 and produced equal JSON reports with `execution_authorized=false`.
These results were reused because application source and image content remained unchanged.

## Final validation

- `PAYRECOVER_REQUIRE_POSTGRES_TESTS=1 pytest -q -p no:cacheprovider`: **311 passed**, 0 failed,
  0 skipped, 0 deselected, in 241.91 seconds. The single existing Starlette/httpx deprecation warning
  remains. All original 285 tests are retained.
- The suite includes five deterministic interruption boundaries with equal per-payment step histories
  and audit counts after resume, independent fresh-run reports, concurrent reservation, run-lock
  exclusion/release, isolation refusal, migration cycle and schema/row-preserving downgrade refusal.
- Strengthened reservation/downgrade checks were also run first: 2 passed, 6 deselected.
- `ruff check .`: All checks passed.
- `mypy src`: Success: no issues found in 47 source files.
- `docker compose config --quiet`: exit 0.
- `docker build -t payrecover:phase4 .`: exit 0; unchanged image verification reused as above.
- `git diff --check`: exit 0, with Git's existing LF-to-CRLF notices only. New untracked files were
  separately checked with `git diff --no-index --check`.

## Files created

```text
alembic/versions/20260907_0006_simulation.py
docs/simulation.md
docs/simulation-validation.md
src/payrecover/infrastructure/database/simulation.py
src/payrecover/infrastructure/database/simulation_models.py
src/payrecover/simulation/__init__.py
src/payrecover/simulation/eligibility.py
src/payrecover/simulation/reporting.py
src/payrecover/simulation/runner.py
src/payrecover/simulation/scenario.py
src/payrecover/simulation_cli.py
tests/integration/test_phase4_simulation.py
tests/test_simulation.py
```

## Files modified

```text
CODEX.md
README.md
alembic/env.py
docs/roadmap.md
src/payrecover/infrastructure/database/planning_models.py
tests/integration/test_phase3a2_incidents.py
tests/integration/test_phase3b_planning.py
```

Existing migration tests retain their refusal guarantees and now explicitly target the relevant
older revision instead of assuming head minus one is always the planning migration. No existing
business service behavior, dependencies or credentials were changed. Nothing was committed, tagged
or pushed. This remains a single-writer, local simulation: no real execution, production access
control, provider adapter, held-out effectiveness campaign or causal comparison is claimed.
