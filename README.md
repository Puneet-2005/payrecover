# PayRecover

An engineering-first payment degradation detection and bounded revenue recovery platform for Razorpay AI Buildathon Track 03: AI Revenue Recovery.

## What exists today

This repository contains the Phase 1 deterministic foundation, Phase 2A persistence and the focused Phase 2B Razorpay test-webhook boundary. It stores normalized payment events and append-only application audit records in PostgreSQL through SQLAlchemy repositories, a unit of work and Alembic migrations. Redis remains provisioned but is not wired into the application.

## Safety invariant

> AI failure must never become a financial safety failure.

AI may summarize evidence or recommend an action. Only versioned deterministic policy can authorize a recovery attempt.

## Run in VS Code

1. Install Python 3.12 and Docker Desktop.
2. Open this folder in VS Code.
3. Create the environment:

```bash
python -m venv .venv
```

4. Activate it (PowerShell):

```powershell
.venv\Scripts\Activate.ps1
```

5. Install, migrate and test:

```bash
pip install -e ".[dev]"
docker compose up -d postgres
alembic upgrade head
pytest -q -m "not integration"
pytest -q -m integration
uvicorn payrecover.api.main:app --reload
```

Docker Compose publishes PostgreSQL on `localhost:5433` by default so it does not conflict with a PostgreSQL installation already using the conventional host port 5432. Host-side tools, including Alembic and the local API process, connect through `localhost:5433`. Compose services continue to connect to PostgreSQL internally through `postgres:5432`. Set `POSTGRES_HOST_PORT` if a different host port is required, and keep the host-side `DATABASE_URL` port aligned with it.

6. Open `http://127.0.0.1:8000/docs`.

Alternatively run `docker compose up --build`.

PostgreSQL integration tests use a disposable PostgreSQL 17 Testcontainer. They skip with a clear reason when Docker is unavailable; CI requires them to run.

## Persistent event ingestion

`POST /v1/payments/events` preserves its original `202` response. Clients may send an `Idempotency-Key` header containing 1–128 visible ASCII characters. Reusing a key with the same canonical payload returns the original successful response without a second event or audit row; reusing it with a different payload returns `409 Conflict`. Without a key, every request is a new event and no idempotency guarantee is claimed.

The event row and its audit record commit in one transaction. Database or commit failures return `503` rather than falsely acknowledging an event. Client-provided timezone-aware timestamps are normalized to UTC; naive timestamps are rejected. PostgreSQL `TIMESTAMPTZ` stores instants, and API serialization uses UTC.

Set `PERSISTENCE_ENABLED=true` and provide a valid `postgresql+psycopg://` `DATABASE_URL`. Settings and database engines are loaded lazily, so importing the app does not open a database connection.

## Razorpay test webhooks

`POST /v1/webhooks/razorpay` accepts signed Razorpay test-mode `payment.captured` and `payment.failed` events. Configure `RAZORPAY_WEBHOOK_SECRET` separately from API credentials. The endpoint verifies HMAC-SHA256 against the exact raw request bytes before parsing JSON, requires `x-razorpay-event-id`, and limits bodies to 256 KiB.

Only INR payments using `card`, `emi`, `netbanking`, `upi` or `wallet` are normalized. Unsupported event types, methods and currencies receive `204` and create no payment row. Exact provider-event replays are acknowledged without duplicate event or audit rows. A conflicting signed replay is acknowledged with `200` only after a sanitized conflict audit is committed; this prevents futile provider retries while retaining an internal integrity signal.

Webhook records never contain raw bodies, signatures, customer names, email addresses, phone numbers, card identifiers, UPI handles, addresses, descriptions, arbitrary notes or authorization data. Razorpay does not supply request latency, and its issuer/provider dimensions are method-dependent, so unavailable dimensions remain nullable with explicit availability states.

For a local synthetic request, start the API with a non-production secret:

```powershell
$env:RAZORPAY_WEBHOOK_SECRET="local-only-synthetic-secret"
uvicorn payrecover.api.main:app --reload
```

In another terminal, run this standard-library client. The byte string that is signed is the same byte string sent:

```python
import hashlib
import hmac
import json
import urllib.request

secret = b"local-only-synthetic-secret"
event = {
    "entity": "event",
    "account_id": "acc_LocalSynthetic",
    "event": "payment.failed",
    "created_at": 1700000000,
    "payload": {"payment": {"entity": {
        "id": "pay_LocalSynthetic",
        "entity": "payment",
        "amount": 50000,
        "currency": "INR",
        "status": "failed",
        "method": "netbanking",
        "bank": "HDFC",
        "error_code": "BAD_REQUEST_ERROR"
    }}}
}
body = json.dumps(event, separators=(",", ":")).encode()
signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
request = urllib.request.Request(
    "http://127.0.0.1:8000/v1/webhooks/razorpay",
    data=body,
    method="POST",
    headers={
        "Content-Type": "application/json",
        "X-Razorpay-Signature": signature,
        "x-razorpay-event-id": "evt_LocalSynthetic001",
    },
)
print(urllib.request.urlopen(request).read().decode())
```

## Example degradation request

```bash
curl -X POST http://127.0.0.1:8000/v1/detections/evaluate -H "Content-Type: application/json" -d '{"cohort_key":"upi:bank_x:phonepe:band_3","baseline_success_rate":0.92,"observed_success_rate":0.45,"sample_size":200,"failed_count":110,"failed_amount_paise":38000000,"dominant_error_code":"issuer_unavailable"}'
```

## Continue with Codex

Tell Codex:

> Read CODEX.md and all linked architecture documents. Inspect the current tests. Propose the next reviewable milestone after Phase 2A; wait for my confirmation before editing.

This forces the work into explainable engineering increments instead of uncontrolled vibe coding.

## Scope and claims

- Test-mode and simulated payments only.
- Phase 2B includes secure Razorpay test-webhook ingestion only; it does not create, retry or execute payments.
- No invented evaluation metrics.
- Not production-ready or certified by Razorpay.
- Apache-2.0 licensed; see `LICENSE`.
