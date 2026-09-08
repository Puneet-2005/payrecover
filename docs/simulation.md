# Phase 4: synthetic-only evaluation

No real money is recovered, charged or authorized. Existing recommendations remain immutable and
`execution_authorized=false`. The harness uses existing ingestion, analytics, scanning, diagnosis
and planning services. It never sends provider calls, notifications or simulated outcomes into
payment history. No new infrastructure, background scheduler, AI or dashboard is involved.

## Exact dedicated-database setup (PowerShell)

Use the existing Compose PostgreSQL 17 service, not the unrelated local port-5432 installation:

```powershell
docker compose exec postgres psql -U payrecover -d postgres -c "CREATE DATABASE payrecover_sim_demo;"
$env:PAYRECOVER_SIMULATION_DATABASE_URL = 'postgresql+psycopg://payrecover:payrecover@localhost:5433/payrecover_sim_demo'
python -m payrecover.simulation_cli initialize
python -m payrecover.simulation_cli run --run-key demo-42 --seed 42
```

These are the repository's local-development placeholder credentials, not real payment secrets.
Do not modify `.env`. Supply your explicitly provisioned disposable credentials if different.
`initialize` verifies `current_database()`, passes the explicit URL to Alembic Config, upgrades to
head, then inserts the `simulation-only-v1` marker. It refuses a database with existing payment
events. Migrations create an empty marker table, never its row. Ordinary `run` cannot migrate or
initialize and refuses missing configuration or marker.

Only `postgresql+psycopg`, database names matching `payrecover_sim_[a-z0-9_]+`, and the corresponding
marker are accepted. Loopback port 5432 is forbidden. There is no normal DATABASE_URL, settings-cache
or `.env` fallback. These are accidental-use safeguards, not authentication against privileged SQL.
Do not run any other writer or expose an API against this database.

For the built image, explicitly set the variable using `host.docker.internal:5433` instead of
localhost, then run:

```powershell
docker run --rm -e PAYRECOVER_SIMULATION_DATABASE_URL payrecover:phase4 python -m payrecover.simulation_cli run --run-key image-demo --seed 42
```

The `initialize` command is available in the image too. Both commands use the same safeguards.

## Versioned scenario and logical clock

`recovery-demo-v1` generates 50,000 base unique events plus contradictory-history events and explicit
duplicate deliveries. `--size 1000` uses the same generator for routine tests. Seven days of baseline
precede four 15-minute windows: normal, labelled degradation, normal, normal. A control cohort
remains normal and a sparse cohort deliberately yields insufficient classifications. Inputs contain
transient, issuer, authentication, funding and unmapped signals, duplicate identities, inconsistent
amounts and stored success, including success delivered after planning.

Seeded SHA-256 choices determine integer amounts, stable identities and fixtures. Latency zero is
an explicit synthetic fixture, not a measured or inferred provider latency. All events pass through
`ingest_payment_event` with explicit occurrence timestamps and idempotency keys; there is no bulk
insertion shortcut. A stage becomes available at window end plus seven minutes: five minutes of
completion delay and two minutes of synthetic ingestion delay. This injected time drives services
and scheduling without sleeping. Existing database receipt/audit timestamps remain operational only.

The manifest records generator, fixture, eligibility, detector, diagnosis, planning, runner/report
versions, seed, size, epoch and a normalized source-tree digest. Resume rejects a mismatch. Compatible
completed replay returns its historical stored report. Compare fresh runs by logical identities and
metrics, excluding random database IDs and operational timestamps.

## Run ownership, transactions and resume

A dedicated `NullPool` engine owns a physical autocommit connection with a session-level
`pg_try_advisory_lock` derived from the run UUID. Existing services never use this connection, so
their independent commits cannot release its lock. Completion/exception cleanup explicitly unlocks
and closes it; process death closes the session and PostgreSQL releases the lock. A second active
coordinator fails instead of waiting. Payment reservations use short transactions and row locks.

Database constraints enforce run/merchant/event/plan associations, unique attempt ordinals, at most
two attempts, at most one outstanding attempt and at most one confirmed success per payment.
Immutable simulation steps preserve outcomes and transitions separately from mutable attempt-state
projections. Normal ORM mutation/deletion of step history is rejected; privileged SQL is outside
the application append-only boundary. Downgrade refuses before DDL while a run has history.

Deterministic crash injection covers ingestion commit before checkpoint, planning commit before
checkpoint, reservation commit, model evaluation before persistence, and outcome commit before
checkpoint. Resume uses the same event identity, historical plan, attempt ordinal, reservation time
and model fixture. It never consumes another attempt or audit for a replay. Injected duplicate
metrics count manifest deliveries and explicit duplicate dispatch keys, not resume retries.

Candidate pagination is live. Ingestion is frozen for the complete stage while analytics, scanning
and all candidate pages finish. Resume re-enumerates the same stage using existing exact replay;
later-stage ingestion cannot begin until its stage checkpoint commits. This is an isolated
single-writer contract, not snapshot reconstruction under external concurrent ingestion.

## Eligibility, attempts and hidden truth

The simulation gate consumes only visible consent/flow/status fixtures, the immutable plan, fresh
authoritative stored history, and prior observed attempt states. Missing prerequisites, unplanned
payments, stored success, contradictions, terminal/unknown signals, prior simulated success,
outstanding attempts and exhausted budgets block. Current policy can tighten but never loosen the
original recommendation's delay/ceiling. It does not change the plan's blocked prerequisites.

First attempts respect the minimum recommendation delay; second attempts wait at least twice that
delay after the first reservation and at least 60 seconds for reconciliation. No attempt is allowed
while a previous outcome is uncertain. Reconciliation does not consume an attempt.

Hidden fixtures contain restoration time (epoch + 2220..2339 seconds), persistent failure,
response-loss and reconcilability flags, chosen independently of recommendations. The pure model
succeeds only after restoration and only for nonpersistent failures. Response loss returns uncertain
regardless of underlying success/failure. Truth and observed results are recorded separately.
After 60 seconds reconciliation reveals that same truth when reconcilable, otherwise stays uncertain.
It never draws a new outcome. Eligibility/scheduling do not inspect future restoration or truth.

## Persisted report semantics

Default output is readable, labelled JSON with a simulation-only banner; `--json` omits the banner.
The report is rebuilt from persisted records before completion, not runtime counters.

- Detection unit: three cohorts times four windows. Positive labels are affected and sparse cohorts
  in the degradation window. Insufficient is separate AND is a false negative on positive labels.
  On normal labels it is a non-alert/TN with insufficient coverage separately visible.
- Recall denominator includes all positive labels. Detection delay uses actual logical emission
  minus labelled onset, including completion and ingestion delays; undetected positives remain FN.
- Eligibility counts initial decisions per population payment; later decisions remain in history.
- Population includes every unique payment failing in the labelled window, including control,
  sparse and unplanned payments. Multiple event rows never multiply an amount.
- Conflicting amounts exclude the identity from monetary totals with count, null value and explicit
  indeterminate-value reason. No arbitrary first/last amount is selected.
- Mutually exclusive horizon precedence: any stored success is already-successful; otherwise any
  confirmed simulated success is simulated-recovered; otherwise unresolved. A stored success arriving
  after planning or simulation removes that payment from simulated attribution. Uncertain, blocked,
  exhausted and unplanned payments remain unresolved unless stored success resolves them.
- Exact integer-paise assertion: total = already-successful + simulated-recovered + unresolved.
  Confirmed simulated recovery counts once per payment, never once per event or attempt.

This is descriptive evaluation of a declared synthetic world, not real recovery effectiveness,
calibrated diagnosis confidence or causal uplift. No throughput target is assumed. Held-out campaigns,
causal comparisons and external fault adapters remain deferred. A later Razorpay adapter requires
official product-specific consent, flow, idempotency and reconciliation documentation; no generic
failed-payment retry API is assumed.
