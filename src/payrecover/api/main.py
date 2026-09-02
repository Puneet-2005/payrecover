import logging
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, status

from payrecover.api.dependencies import UnitOfWorkFactory, get_unit_of_work_factory
from payrecover.api.razorpay import router as razorpay_router
from payrecover.domain.models import CohortSnapshot, PaymentEvent, RecoveryDecision
from payrecover.services.detection import detect_degradation
from payrecover.services.ingestion import (
    IdempotencyConflict,
    IngestionUnavailable,
    InvalidIdempotencyKey,
    ingest_payment_event,
)
from payrecover.services.policy import decide_recovery

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    application = FastAPI(title="PayRecover API", version="0.1.0")
    application.include_router(razorpay_router)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "payrecover"}

    @application.post("/v1/payments/events", status_code=status.HTTP_202_ACCEPTED)
    def ingest_event(
        event: PaymentEvent,
        unit_of_work_factory: Annotated[UnitOfWorkFactory, Depends(get_unit_of_work_factory)],
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=128,
                pattern=r"^[\x21-\x7e]+$",
            ),
        ] = None,
    ) -> dict[str, str | None]:
        try:
            with unit_of_work_factory() as unit_of_work:
                result = ingest_payment_event(event, idempotency_key, unit_of_work)
        except InvalidIdempotencyKey as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except IdempotencyConflict as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except IngestionUnavailable as exc:
            logger.error("Payment-event transaction failed")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Payment event could not be persisted",
            ) from exc
        return {"accepted": result.payment_id, "cohort_key": result.cohort_key}

    @application.post("/v1/detections/evaluate")
    def evaluate(snapshot: CohortSnapshot):
        return detect_degradation(snapshot)

    @application.get("/v1/recovery/decision/{error_code}", response_model=RecoveryDecision)
    def recovery_decision(error_code: str, attempt: int = 0) -> RecoveryDecision:
        return decide_recovery(error_code, attempt)

    return application


app = create_app()
