# Architecture

PayRecover receives immutable payment events, aggregates rolling cohort health, detects statistically significant degradation, diagnoses evidence, and selects a bounded recovery action. A deterministic policy engine—not an LLM—authorizes every money-related operation.

## Trust boundaries

1. Merchant/webhook input is untrusted and validated against versioned contracts.
2. Detection produces evidence; it cannot execute recovery.
3. AI diagnosis is optional advice and cannot bypass policy.
4. Execution requires an idempotency key and an authorized state transition.
5. Audit records contain the evidence, policy version, actor, decision and outcome.

## Intended production flow

`Webhook -> durable stream -> event store -> cohort processor -> detector -> diagnosis -> policy -> executor -> outcome -> audit`

See `docs/roadmap.md` for the order in which these boundaries should be implemented.

## Phase 2A persistence boundary

The normalized API flow is currently:

`validated request -> ingestion service -> unit of work -> payment event + audit -> commit -> 202`

An optional client `Idempotency-Key` is stored as the source event identity. PostgreSQL uniqueness is authoritative: the same key and canonical payload returns the stored event, while the same key with different content is rejected. Requests without a key receive a new internal identity and are not idempotent. Generated timestamps and receipt metadata are excluded from the canonical payload digest.

SQLAlchemy models and sessions live under `payrecover.infrastructure.database`. Domain services depend on repository and unit-of-work protocols and do not import SQLAlchemy. Repositories may flush to obtain database-generated IDs, but only the unit of work commits or rolls back. The payment event and its audit record are one atomic transaction.

All event and audit timestamps use timezone-aware PostgreSQL `TIMESTAMPTZ`. Connections use UTC, API input is normalized to UTC, and naive API timestamps are invalid. Monetary values are integer paise in `BIGINT` columns.

There is no durable stream, Razorpay webhook adapter, signature verification or recovery execution in Phase 2A.

## Phase 2B webhook boundary

The Razorpay test-mode flow is:

`bounded raw bytes -> HMAC verification -> strict allowlist normalization -> Phase 2A ingestion service -> payment event + audit -> commit -> 200`

Headers and declared body size are validated before reading the body. The actual streamed byte count is independently limited to 256 KiB. JSON is parsed only after HMAC-SHA256 verification over the original bytes. The provider event ID uses a PostgreSQL partial unique index as the concurrency authority.

Razorpay events use normalized schema version 2. Missing, not-applicable and privacy-redacted issuer/provider/latency/error-code dimensions are represented explicitly rather than with invented values. Non-INR currency subunits are not stored as paise. Rows lacking the complete Phase 1 dimensions have a null full cohort key and remain available through merchant/method/time fields.

An exact replay performs no second audit insert. A conflicting replay first rolls back event ingestion, then uses a new unit of work to append `payment_event.idempotency_conflict` against the original event. Only after that audit commits does the endpoint acknowledge the conflict. No webhook path authorizes or executes money movement.
