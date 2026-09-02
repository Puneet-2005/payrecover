# Build roadmap

## Phase 1 — included in this ZIP

- Versioned payment event model
- Cohort identity
- Statistical degradation detector
- Deterministic recovery policy
- Idempotency-key primitive
- FastAPI endpoints, tests, Docker and CI

## Phase 2 — persistence and real ingestion

- SQLAlchemy models and Alembic migrations
- Webhook signature verification
- PostgreSQL event store and append-only audit log
- Redis Streams consumer with dead-letter handling
- Database uniqueness constraint for idempotency

## Phase 3 — incident lifecycle

- Explicit incident/recovery state machine
- Rolling cohort aggregation
- Root-cause evidence ranking
- Recovery executor against a local Razorpay test adapter
- Backoff scheduler, stopping rules and human approval queue

## Phase 4 — evaluation

- Seeded synthetic generator for at least 50,000 events
- Hidden incident manifest and held-out scenarios
- Detection recall, false-positive rate, MTTD, RCA accuracy
- Recovered-revenue and unsafe-attempt metrics
- Failure injection: duplicate webhook, worker crash, DB/API/AI timeout

## Phase 5 — presentation

- Operational dashboard
- Reproducible benchmark report with real outputs
- Five-minute demo script and architecture diagram
- Public-repo cleanup, SECURITY.md and contribution guide

Do not add an LLM until Phases 2 and 3 pass deterministic tests.

