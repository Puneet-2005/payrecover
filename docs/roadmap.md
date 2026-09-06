# Build roadmap

## Phase 1 — included in this ZIP

- Versioned payment event model
- Cohort identity
- Statistical degradation detector
- Deterministic recovery policy
- Idempotency-key primitive
- FastAPI endpoints, tests, Docker and CI

## Phase 2A — normalized-event persistence

- Typed configuration and lazy PostgreSQL sessions
- SQLAlchemy 2.x payment-event and audit models
- Alembic initial migration
- PostgreSQL event store and application-layer append-only audit repository
- Optional direct-ingestion idempotency key enforced by database uniqueness
- Unit tests and PostgreSQL 17 Testcontainer acceptance tests

## Phase 2B — secure Razorpay test-webhook ingestion

- Raw-body HMAC-SHA256 verification and bounded request ingestion
- Strict payment webhook allowlist with privacy-safe normalization
- Provider event-ID uniqueness, exact replay handling and sanitized conflict auditing
- Versioned nullable payment dimensions with explicit availability states
- Test-mode only; no payment creation, retry or recovery execution
- Redis Streams consumer with dead-letter handling remains deferred until explicitly approved

## Phase 3A1 — observational analytics kernel

- Versioned cohort-v2 identity with explicit dimension availability
- Completed UTC baseline and observation windows
- Merchant-scoped PostgreSQL aggregation
- Decimal-based deterministic degradation detector-v2
- Typed results only; no incident persistence or recovery action

## Phase 3A2 — incident lifecycle

- Persistent incident state machine
- Idempotent controlled scans and concurrent incident deduplication
- Incident observations, resolution hysteresis and append-only audits
- Merchant-scoped read-only incident APIs
- Local argparse scan CLI and disposable PostgreSQL lifecycle/migration tests
- Implemented for local development; real access control remains required before public exposure

## Later Phase 3 work

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
