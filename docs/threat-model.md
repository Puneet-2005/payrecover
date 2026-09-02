# Threat model (starter)

Protected assets: merchant revenue, payment credentials, customer identifiers, recovery authority and audit integrity.

| Threat | Control |
|---|---|
| Forged webhook | No webhook endpoint in Phase 2A; signature verification is required before one is enabled |
| Duplicate normalized request | Optional client idempotency key plus PostgreSQL uniqueness; no idempotency claim without a key |
| Unsafe retry loop | Versioned policy and hard attempt ceiling |
| Prompt injection in payment metadata | Structured evidence only; AI output never grants authority |
| Secret leakage | Environment/secret manager; redacted structured logs |
| Audit tampering | Application append-only guard, restricted runtime database role, chained hashes later |
| False anomaly | Minimum sample size, statistical threshold and human escalation |

This is a living engineering document, not a security certification.

Phase 2A audit append-only checks cover repository usage and normal ORM mutation/deletion. They do not stop bulk SQL or privileged database access; restricted production database roles are required. Chained hashes remain future work.
