# PayRecover

An engineering-first payment degradation detection and bounded revenue recovery platform for Razorpay AI Buildathon Track 03: AI Revenue Recovery.

## What exists today

This repository contains the Phase 1 deterministic foundation, Phase 2A persistence, the Phase 2B Razorpay test-webhook boundary, the Phase 3A1 analytics kernel and Phase 3A2 persistent incident scanning. PostgreSQL stores normalized events, incident evidence and application append-only audits through SQLAlchemy repositories, a unit of work and Alembic migrations. Redis remains provisioned but is not wired into the application.

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

## Phase 3A1 analytics kernel

Phase 3A1 adds a typed, read-only analytics service for future incident scanning. It groups normalized database fields into deterministic `cohort-v2` identities containing merchant, method, issuer/provider values and availability states, and one of six fixed integer-paise amount bands. The legacy `payment_events.cohort_key` is not an authority for this identity.

Given a caller-supplied aware timestamp, analytics uses the latest completed 15-minute UTC observation window after a five-minute completion delay. Its baseline is the immediately preceding seven days, and both intervals are half-open. A PostgreSQL aggregate query calculates counts, integer-paise failure totals and a deterministic top-20 error-code distribution without loading individual payment rows into Python.

Detection version `degradation-v2` requires at least 100 baseline events and 30 observation events. It combines a success-rate drop of at least 0.10 with a Laplace-smoothed two-sample score of at least 3.0. Eligible results are `healthy`, `watch` or `degraded`; ineligible results are explicitly `insufficient`. All rates and detector calculations use deterministic `Decimal` arithmetic.

The analytics kernel remains independent of HTTP and background tasks. Phase 3A2 calls it within an explicit incident-scan transaction; the kernel itself does not persist or authorize actions.

## Phase 3A2 local incident scans

After applying Alembic migrations to your configured development PostgreSQL database:

```powershell
python -m payrecover.cli --merchant-id acc_LocalSynthetic
python -m payrecover.cli --merchant-id acc_LocalSynthetic --as-of 2026-09-05T12:20:00Z
```

The CLI reads its clock once, rejects future or naive timestamps, and prints a safe JSON scan summary. Repeating a committed merchant/window scan returns the stored summary with `replayed: true`. An unseen older window is rejected. The first successful commit freezes the window: late events cannot rewrite its incident results, but can enter subsequent baselines.

Open incidents resolve after two healthy observations in immediately adjacent 15-minute windows. Watch, insufficient samples, completely absent cohorts and gaps reset consecutiveness. Resolution means the detector's relative health criterion was met; it does not prove that the original underlying outage ended. A later degradation opens a new historical incident.

Local read interfaces:

```text
GET /v1/incidents?merchant_id=acc_LocalSynthetic&limit=20
GET /v1/incidents/1?merchant_id=acc_LocalSynthetic&limit=20
```

Use the returned `next_cursor` as `cursor` to continue. Limits are 1–100; detail pagination applies to observations and always includes the immutable opening observation. Rates and scores serialize as decimal strings, and paise totals as exact integers.

These interfaces have **no authentication**. Merchant filtering is not access control. Bind the API to loopback for local development; do not expose it publicly as a production multi-tenant service. See [incident design and schema](docs/incidents.md) for transaction, pagination, migration and retention details.

## Continue with Codex

Tell Codex:

> Read CODEX.md and all linked architecture documents. Inspect the current tests. Propose the next reviewable milestone after Phase 2A; wait for my confirmation before editing.

This forces the work into explainable engineering increments instead of uncontrolled vibe coding.

## Scope and claims

- Test-mode and simulated payments only.
- Phase 2B includes secure Razorpay test-webhook ingestion only; it does not create, retry or execute payments.
- Phase 3A1 is observational analytics only; it does not persist incidents or create, retry or execute payments.
- No invented evaluation metrics.
- Not production-ready or certified by Razorpay.
- Apache-2.0 licensed; see `LICENSE`.
