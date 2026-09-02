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

