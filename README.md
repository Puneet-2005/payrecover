# PayRecover

An engineering-first payment degradation detection and bounded revenue recovery platform for Razorpay AI Buildathon Track 03: AI Revenue Recovery.

## What exists today

This repository is an honest Phase 1 foundation, not a falsely finished hackathon product. It contains validated payment-event contracts, cohort calculation, a guarded statistical detector, deterministic recovery rules, idempotency-key generation, API endpoints, tests, Docker and CI. PostgreSQL and Redis are provisioned for the next implementation phase but are not yet wired into the application.

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

5. Install and test:

```bash
pip install -e ".[dev]"
pytest -q
uvicorn payrecover.api.main:app --reload
```

6. Open `http://127.0.0.1:8000/docs`.

Alternatively run `docker compose up --build`.

## Example degradation request

```bash
curl -X POST http://127.0.0.1:8000/v1/detections/evaluate -H "Content-Type: application/json" -d '{"cohort_key":"upi:bank_x:phonepe:band_3","baseline_success_rate":0.92,"observed_success_rate":0.45,"sample_size":200,"failed_count":110,"failed_amount_paise":38000000,"dominant_error_code":"issuer_unavailable"}'
```

## Continue with Codex

Tell Codex:

> Read CODEX.md and all linked architecture documents. Inspect the current tests. Implement only Phase 2 PostgreSQL persistence with Alembic migrations, repository interfaces and integration tests. First give me the proposed schema and explain every table; wait for my confirmation before editing.

This forces the work into explainable engineering increments instead of uncontrolled vibe coding.

## Scope and claims

- Test-mode and simulated payments only.
- No invented evaluation metrics.
- Not production-ready or certified by Razorpay.
- Apache-2.0 licensed; see `LICENSE`.

