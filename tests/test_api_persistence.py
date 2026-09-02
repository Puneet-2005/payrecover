from contextlib import AbstractContextManager
from dataclasses import asdict
from datetime import UTC, datetime
from types import TracebackType
from uuid import uuid4

from fastapi.testclient import TestClient

from payrecover.api.dependencies import get_unit_of_work_factory
from payrecover.api.main import create_app
from payrecover.domain.records import StoredAuditRecord, StoredPaymentEvent
from payrecover.services.ingestion import IngestionUnavailable

VALID_EVENT = {
    "payment_id": "pay_1",
    "merchant_id": "mer_1",
    "method": "upi",
    "issuer": "bank_x",
    "provider": "phonepe",
    "amount_paise": 349_900,
    "status": "failed",
    "error_code": "network_timeout",
    "latency_ms": 100,
}


class FailingUnitOfWork:
    @property
    def payment_events(self):
        raise IngestionUnavailable("synthetic database failure")

    def commit(self):
        raise AssertionError("commit must not be reached")

    def rollback(self):
        pass


class SuccessfulPaymentEvents:
    def add_if_absent(self, event, *, identity_scope=None):
        del identity_scope
        return (
            StoredPaymentEvent(
                id=uuid4(),
                received_at=datetime.now(UTC),
                **asdict(event),
            ),
            True,
        )


class SuccessfulAudits:
    def append(self, record):
        return StoredAuditRecord(
            id=1,
            recorded_at=datetime.now(UTC),
            **asdict(record),
        )


class SuccessfulUnitOfWork:
    def __init__(self) -> None:
        self.payment_events = SuccessfulPaymentEvents()
        self.audit_records = SuccessfulAudits()
        self.committed = False

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


class UnitOfWorkContext(AbstractContextManager):
    def __init__(self, unit_of_work: object) -> None:
        self.unit_of_work = unit_of_work

    def __enter__(self):
        return self.unit_of_work

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def test_database_failure_returns_503():
    app = create_app()
    app.dependency_overrides[get_unit_of_work_factory] = lambda: (
        lambda: UnitOfWorkContext(FailingUnitOfWork())
    )
    response = TestClient(app).post("/v1/payments/events", json=VALID_EVENT)
    assert response.status_code == 503
    assert response.json() == {"detail": "Payment event could not be persisted"}


def test_successful_persistence_preserves_original_202_response():
    app = create_app()
    unit_of_work = SuccessfulUnitOfWork()
    app.dependency_overrides[get_unit_of_work_factory] = lambda: (
        lambda: UnitOfWorkContext(unit_of_work)
    )
    response = TestClient(app).post("/v1/payments/events", json=VALID_EVENT)
    assert response.status_code == 202
    assert response.json() == {
        "accepted": "pay_1",
        "cohort_key": "upi:bank_x:phonepe:band_3",
    }
    assert unit_of_work.committed is True


def test_invalid_idempotency_header_is_rejected_before_opening_a_unit_of_work():
    app = create_app()

    def must_not_open():
        raise AssertionError("invalid request must not open a unit of work")

    app.dependency_overrides[get_unit_of_work_factory] = lambda: must_not_open
    response = TestClient(app).post(
        "/v1/payments/events",
        headers={"Idempotency-Key": "contains a space"},
        json=VALID_EVENT,
    )
    assert response.status_code == 422
