# Security policy

Never commit Razorpay keys, webhook secrets, real customer data or production payment identifiers. This repository defaults to test/simulated transactions. Report vulnerabilities privately to the repository owner rather than opening a public issue.

Phase 2A persists only allowlisted normalized payment fields and a SHA-256 payload digest. It does not store raw HTTP payloads, credentials, authorization headers or customer contact/card data. Database URLs are treated as secrets and must not be logged.

Phase 2B verifies Razorpay webhook signatures over the exact raw body before parsing. `RAZORPAY_WEBHOOK_SECRET` is a lazy `SecretStr` setting and is separate from Razorpay API credentials. Requests require one signature header, one provider event-ID header, JSON content type and a body no larger than 256 KiB. Declared and streamed sizes are checked independently.

Webhook normalization discards customer names, email addresses, phone numbers, card identifiers, UPI handles, addresses, descriptions, arbitrary notes and unknown metadata. Raw bodies and signatures are never logged or persisted. Signed idempotency conflicts preserve the original event and append a new sanitized audit containing only digests, source, correlation and detection time before returning a successful acknowledgement.

Audit records are append-only through repository APIs and normal ORM sessions, but bulk SQL and privileged database users can bypass application guards. A production deployment must give the runtime database role only the required `SELECT` and `INSERT` audit permissions and reserve migration/administrative privileges for a separate role. Phase 2A does not claim tamper-proof auditing.
