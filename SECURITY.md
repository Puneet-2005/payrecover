# Security policy

Never commit Razorpay keys, webhook secrets, real customer data or production payment identifiers. This repository defaults to test/simulated transactions. Report vulnerabilities privately to the repository owner rather than opening a public issue.

Phase 2A persists only allowlisted normalized payment fields and a SHA-256 payload digest. It does not store raw HTTP payloads, credentials, authorization headers or customer contact/card data. Database URLs are treated as secrets and must not be logged.

Audit records are append-only through repository APIs and normal ORM sessions, but bulk SQL and privileged database users can bypass application guards. A production deployment must give the runtime database role only the required `SELECT` and `INSERT` audit permissions and reserve migration/administrative privileges for a separate role. Phase 2A does not claim tamper-proof auditing.
