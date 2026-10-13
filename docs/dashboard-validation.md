# Phase 5 validation — 2026-09-09

Simulation only. No execution authorization or real money recovered.

## Interrupted work recovered

The retained `payrecover-phase5-demo-db` container had stopped, and no Python setup coordinator
remained. Its logs showed the early missing-marker refusal and the later successful dashboard
startup. After restarting only this disposable database, PostgreSQL showed:

- `simulation_marker`: `payrecover_sim_dashboard`, `simulation-only-v1`.
- Alembic revision `20260907_0006`.
- Run `ebce7978-4c1e-538d-8a6a-4e82df1347b5`: `complete`, with a stored report.
- 1,026 delivery steps, 12 evaluations, 72 reservations, 72 outcomes, 26 reconciliations and
  72 suppression records.

The prior documented CLI initialization and small scenario had completed after their last reported
progress. No marker was manually inserted, no populated database was reinitialized, and no second
scenario coordinator or 50,000-event run was launched. The small scenario's original wall-clock
runtime was not captured and is not claimed here. The previous interrupted full-suite run was not
counted as a success.

## Genuine final checks

Commands use `.venv/Scripts` executables on this Windows workspace.

| Check | Result |
| --- | --- |
| `PAYRECOVER_REQUIRE_POSTGRES_TESTS=1 pytest -q -p no:cacheprovider` | 324 passed in 227.75 seconds; 0 failed, skipped or deselected |
| Focused dashboard regressions after cursor/classification strengthening | 13 passed in 20.79 seconds |
| `ruff check .` | All checks passed |
| `mypy src` | Success: no issues found in 52 source files |
| `docker compose config --quiet` | Exit 0 |
| `docker build -t payrecover:phase5 .` | Exit 0 |
| `git diff --check` | Exit 0; LF-to-CRLF notices only |

All 311 baseline tests remain unchanged. The full suite includes the disposable PostgreSQL 17
migration-cycle and history-preserving refusal tests. One existing Starlette/httpx deprecation
warning remains; no dependency was added to suppress it. Untracked source/document files were
also checked with `git diff --no-index --check` (no whitespace diagnostics; diff status indicates
new content, not a failed test).

Final image ID: `sha256:44aefb091dab408cd44044f975347225ff39f6ff4a3d523b7bb145355630055c`.
Its installed wheel metadata contains all three assets: `index.html`, `dashboard.css`,
`dashboard.js` under `payrecover/dashboard/static/`. The actual image serves them successfully.
Startup logs contain `Application startup complete`; the server is published on loopback only.

PostgreSQL regression coverage includes independent run/merchant rejection in nested repository
reads; canonical scoped cursors and SQL-level limits; database write rejection through the actual
dashboard engine; unchanged audit/history counts during browsing; unplanned/excluded payment
navigation; hidden-truth field exclusion; exact `9223372036854775807` paise serialization and INR
formatting; and later stored success changing current classification without changing the stored
report or historical recommendation. Missing markers refuse startup without initialization.

## Browser walkthrough

Completed in Brave against the built image and retained small scenario:

`overview → detection → incident 1 → opening diagnosis 1 → payments → p000551 → attempts/history → overview`

Verified the labelled false negative/insufficient coverage, opening evidence, resolved lifecycle,
58 supporting failures out of 83, immutable blocked retry recommendation with 30-second minimum
backoff and `execution_authorized=false`, uncertain outcomes reconciled to failed/succeeded,
duplicate suppression, and two pages of distinct logical payment identities.

All walkthrough API and static-resource requests returned 200. The initial automatic favicon 404
was corrected with a 204 response, tested in the final suite. Browser console inspection returned
no warning/error entries. A final-image reload and payment pagination/detail reads succeeded.
Screenshots are local artifacts in ignored `dist/phase5/`: `overview.png`, `recommendation.png`,
`history.png`. Full-page capture timed out; viewport captures succeeded and were inspected.

The stored small-scenario report shows:

```text
804951 total paise = 93949 already-successful + 219708 simulated-recovered + 491294 unresolved
83 identities = 9 already-successful + 21 simulated-recovered + 47 unresolved + 6 excluded
```

The excluded identities have indeterminate value. These are historical synthetic outcomes, not
real money recovered or a causal uplift estimate. Current classifications are labelled separately.

## Running the retained local demo

The final server is left running at http://127.0.0.1:8001 in `payrecover-phase5-final`.
The retained database is `payrecover-phase5-demo-db`, currently published at `127.0.0.1:58889`.
To run the host application instead, from the repository root:

```powershell
docker stop payrecover-phase5-final
$env:PAYRECOVER_SIMULATION_DATABASE_URL = 'postgresql+psycopg://demo:simulation_only@127.0.0.1:58889/payrecover_sim_dashboard'
.\.venv\Scripts\python.exe -m uvicorn payrecover.dashboard.app:create_app --factory --host 127.0.0.1 --port 8001
```

These are disposable synthetic database credentials. `.env` was not changed. After a Docker restart,
check `docker port payrecover-phase5-demo-db 5432`: randomly assigned host ports may change. Update
only the process environment URL to the reported port. General fresh-database/image instructions
are in [dashboard.md](dashboard.md); never initialize this already-populated retained database.

## Files

Modified: `CODEX.md`, `README.md`, `docs/roadmap.md`, `src/payrecover/simulation/reporting.py`.

Created:

```text
docs/dashboard.md
docs/dashboard-validation.md
src/payrecover/dashboard/__init__.py
src/payrecover/dashboard/app.py
src/payrecover/dashboard/static/index.html
src/payrecover/dashboard/static/dashboard.css
src/payrecover/dashboard/static/dashboard.js
src/payrecover/domain/dashboard.py
src/payrecover/infrastructure/database/dashboard_reads.py
src/payrecover/simulation/classification.py
tests/test_dashboard.py
tests/integration/test_dashboard_reads.py
```

## Remaining boundaries

This is a local unauthenticated demo, not a production tenancy/authentication boundary. Read-only
startup defaults protect against accidental writes, not privileged SQL deliberately changing those
defaults. Live evidence lists are not historical accounting snapshots. Equal-time history entries
are ordered by stable key, not a claimed causal execution order. Desktop Brave was visually checked;
cross-browser/mobile and formal accessibility certification are not claimed. AI integration,
broader held-out evaluation and provider execution remain pending. No real credentials, customer
data, new dependencies, commits, tags or pushes were added.
