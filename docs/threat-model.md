# Threat model (starter)

Protected assets: merchant revenue, payment credentials, customer identifiers, recovery authority and audit integrity.

| Threat | Control |
|---|---|
| Forged webhook | Signature verification, timestamp tolerance, replay cache |
| Duplicate delivery/worker retry | Database-enforced idempotency key |
| Unsafe retry loop | Versioned policy and hard attempt ceiling |
| Prompt injection in payment metadata | Structured evidence only; AI output never grants authority |
| Secret leakage | Environment/secret manager; redacted structured logs |
| Audit tampering | Append-only records plus chained hashes in later phase |
| False anomaly | Minimum sample size, statistical threshold and human escalation |

This is a living engineering document, not a security certification.

