# Threat model (starter)

Protected assets: merchant revenue, payment credentials, customer identifiers, recovery authority and audit integrity.

| Threat | Control |
|---|---|
| Forged webhook | HMAC-SHA256 over bounded, unmodified raw bytes before JSON parsing; constant-time digest comparison |
| Duplicate webhook | Provider event ID plus a PostgreSQL partial unique index; exact replays create no duplicate audit |
| Conflicting signed replay | Preserve the original row, append a digest-only conflict audit in a new transaction, then acknowledge to prevent futile retries |
| Payload body exhaustion | Validate content type and declared length before reading; enforce an independent 256 KiB streamed-byte limit |
| Webhook PII leakage | Strict nested allowlist; never persist or log raw bodies, contact data, card identifiers, UPI handles, notes or unknown fields |
| Misclassified payment dimensions | Nullable values plus explicit provided/missing/not-applicable/redacted states; unsupported methods and non-INR currencies create no row |
| Duplicate normalized request | Optional client idempotency key plus PostgreSQL uniqueness; no idempotency claim without a key |
| Unsafe retry loop | Versioned policy and hard attempt ceiling |
| Prompt injection in payment metadata | Structured evidence only; AI output never grants authority |
| Secret leakage | Environment/secret manager; redacted structured logs |
| Audit tampering | Application append-only guard, restricted runtime database role, chained hashes later |
| False anomaly | Minimum sample size, statistical threshold and human escalation |

This is a living engineering document, not a security certification.

Phase 3A2 merchant query parameters are scope filters, not authentication. Incident read routes must remain local-development interfaces until real access control exists. Merchant advisory transaction locks serialize lifecycle updates, and unique scan/open-incident constraints suppress duplicates. Evidence and audits commit atomically; error distributions are bounded to 20 codes and contain no payment IDs or customer payloads. Unsigned pagination cursors are strictly validated but never treated as authorization. Resolution is a relative detector result, not proof of outage recovery.

Phase 2A audit append-only checks cover repository usage and normal ORM mutation/deletion. They do not stop bulk SQL or privileged database access; restricted production database roles are required. Chained hashes remain future work.

Phase 2B is a test-webhook boundary, not a production security certification. Repeated invalid deliveries and conflict-audit failures require operational monitoring. Secret rotation and connected-account tenancy require separate production design.
