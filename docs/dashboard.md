# Phase 5: local simulation explorer

This separate, read-only FastAPI application serves packaged HTML/CSS/JavaScript. No frontend
framework, CDN, new dependency, migration or background task is required. Deterministic diagnosis
is not an LLM feature. AI integration and broader held-out evaluation remain pending.

## Local walkthrough (PowerShell)

Activate the repository virtual environment first. For an existing marked simulation database:

```powershell
$env:PAYRECOVER_SIMULATION_DATABASE_URL = 'postgresql+psycopg://payrecover:payrecover@localhost:5433/payrecover_sim_demo'
python -m uvicorn payrecover.dashboard.app:create_app --factory --host 127.0.0.1 --port 8001
```

Open http://127.0.0.1:8001. The credentials above are local placeholders, not payment credentials.
Do not edit ignored `.env`. There is no normal `DATABASE_URL` or settings fallback.

If no populated dedicated database exists, follow [simulation setup](simulation.md) to explicitly
create and initialize one, then run a small scenario **outside** the dashboard:

```powershell
python -m payrecover.simulation_cli run --run-key dashboard-demo --seed 42 --size 1000
```

Do not reinitialize a populated database. Do not rerun the 50,000-event acceptance merely for UI
testing. [Recorded Phase 4 acceptance](simulation-validation.md) is the source of prior measured
results; the UI reads the selected database's stored report, never those document values.

1. Select a completed run; inspect its manifest, traffic and historical outcome totals.
2. Open Detection: inspect the confusion matrix, denominators, insufficient coverage and window labels.
3. Open Incidents, then an incident: inspect immutable opening evidence and the observation timeline.
4. Select an opening observation to read its persisted diagnosis. Healthy observations may have no
   diagnosis; this is a controlled empty/not-found state, not an inferred diagnosis.
5. Open Payments & outcomes; select one logical payment. Read its historical recommendation,
   individual signal, blockers, minimum backoff and `execution_authorized=false`.
6. Inspect observed attempts and paginated outcome/reconciliation/suppression history. Unplanned and
   conflicting-amount identities remain navigable. Return to the overview for historical accounting.

Image startup (host database already initialized/populated):

```powershell
docker build -t payrecover:phase5 .
$env:PAYRECOVER_SIMULATION_DATABASE_URL = 'postgresql+psycopg://payrecover:payrecover@host.docker.internal:5433/payrecover_sim_demo'
docker run --rm -p 127.0.0.1:8001:8001 -e PAYRECOVER_SIMULATION_DATABASE_URL payrecover:phase5 python -m uvicorn payrecover.dashboard.app:create_app --factory --host 0.0.0.0 --port 8001
```

The container listens internally on all interfaces, but its published port is loopback only.
There is **no authentication**. Merchant filtering is not access control: anyone reaching the demo
can browse all simulation runs. Public deployment requires authentication/authorization and a
separate security review. Do not expose this application publicly.

## Read contracts and isolation

All JSON reads are under `/demo/v1/runs`. Routes include the run overview; `/evaluations`;
`/incidents` and `/incidents/{id}`; `/observations/{id}/diagnosis`; `/payments`;
`/payments/{logical_payment}` and `/payments/{logical_payment}/history`. These are separate from
the existing application/API and mount none of its mutation endpoints.

Lists accept `limit=1..100` (default 20) and a canonical opaque `cursor`; responses contain `items`
and `next_cursor`. Incident detail contains `incident`, `opening_observation`, `observations`,
and `next_cursor`. Cursors bind the resource, run and authoritative merchant; malformed/cross-scope
cursors return 422. Missing scoped evidence returns 404 and database errors return sanitized 503.
Lists use SQL keyset ordering/limits, not in-memory pagination. They are live views, not snapshots
of an incomplete run. Attempt retrieval is capped by the existing two-attempt invariant.

Payment navigation uses the persisted `(simulation_runs.id, simulation_payments.payment_id)`
identity, not an event UUID. Internal joins resolve recommendations and all related event history.
Every nested query resolves the run's persisted merchant, with corresponding run/merchant joins.
The unique run merchant prevents incidents from another run being included. No event body, provider
secret, customer information, hidden model fixture, restoration time or underlying attempt truth
is serialized. Only observed attempt state and allowlisted history fields are exposed.

The connection factory accepts only an explicit PostgreSQL simulation URL, the simulation database
name pattern and its existing `simulation-only-v1` marker. Loopback port 5432 and URL query overrides
are rejected. It neither imports normal application settings nor initializes the database.
Libpq startup options set `default_transaction_read_only=on` and UTC **before** dialect/application
queries. Each request owns one Session, validates the marker, reads and rolls back on exit. There
is no competing explicit `begin()` after autobegin. Writes on this actual connection path fail in
PostgreSQL. This protects against accidental application writes; it is not a replacement for a
least-privilege database role against malicious SQL that deliberately changes session settings.

## Historical versus current evidence

Completed totals come directly from the stored, versioned Phase 4 report. Wire serialization
represents all integers (including counts, BIGINT identifiers and paise) and Decimal values as
strings. INR formatting uses integer quotient/remainder, never floating point. The browser does
not recalculate totals, percentages or payment categories. Unknown report versions are labelled;
incomplete runs show available evidence without fabricated final metrics.

Payment rows are labelled **Current evidence classification**. They reuse the reporting backend's
existing precedence: conflicting/missing amount identity excluded; stored success first; then
confirmed simulated success; otherwise unresolved. This is not a historical reconciliation and
may differ from the completed report after evidence changes. Unplanned payments remain in scope.
Historical recommendations never refresh prerequisites or authorize execution. All recovered
amounts are explicitly simulated; no causal uplift or real money recovered is claimed.

Assets use safe DOM text rendering, no inline scripts or external resources, a restrictive CSP,
no-store caching and accessible focus/navigation. Loading, empty, incomplete and error states are
provided. The application does not run ingestion, scans, planning, simulations, retries, migrations,
notifications or provider calls, and browsing creates no records or audits.
