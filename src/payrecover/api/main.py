from fastapi import FastAPI

from payrecover.domain.models import CohortSnapshot, PaymentEvent, RecoveryDecision
from payrecover.services.detection import detect_degradation
from payrecover.services.policy import decide_recovery

app = FastAPI(title="PayRecover API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "payrecover"}


@app.post("/v1/payments/events", status_code=202)
def ingest_event(event: PaymentEvent) -> dict[str, str]:
    # Persistence and queue publication are intentionally Phase 2 boundaries.
    return {"accepted": event.payment_id, "cohort_key": event.cohort_key}


@app.post("/v1/detections/evaluate")
def evaluate(snapshot: CohortSnapshot):
    return detect_degradation(snapshot)


@app.get("/v1/recovery/decision/{error_code}", response_model=RecoveryDecision)
def recovery_decision(error_code: str, attempt: int = 0) -> RecoveryDecision:
    return decide_recovery(error_code, attempt)

