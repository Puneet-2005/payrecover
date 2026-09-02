from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from payrecover.api.dependencies import get_unit_of_work_factory
from payrecover.api.main import create_app
from payrecover.domain.models import PaymentEvent
from payrecover.infrastructure.database.models import (
    AuditRecordMutationError,
    AuditRecordRow,
    PaymentEventRow,
)
from payrecover.infrastructure.database.session import build_session_factory
from payrecover.infrastructure.database.uow import SqlAlchemyUnitOfWork
from payrecover.services.ingestion import (
    IdempotencyConflict,
    IngestionUnavailable,
    ingest_payment_event,
)

pytestmark = pytest.mark.integration

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


def row_count(session: Session, row_type: type[PaymentEventRow] | type[AuditRecordRow]) -> int:
    return session.scalar(select(func.count()).select_from(row_type)) or 0


def test_alembic_upgrades_an_empty_postgresql_database(migrated_database_url: str):
    from sqlalchemy import create_engine

    engine = create_engine(migrated_database_url)
    try:
        inspector = inspect(engine)
        assert {"alembic_version", "audit_records", "payment_events"}.issubset(
            inspector.get_table_names()
        )
        id_default = next(
            column["default"]
            for column in inspector.get_columns("payment_events")
            if column["name"] == "id"
        )
        assert "gen_random_uuid()" in id_default
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("changes", "constraint_name"),
    [
        ({"amount_paise": 0}, "ck_payment_events_amount_paise_positive"),
        ({"latency_ms": -1}, "ck_payment_events_latency_ms_nonnegative"),
        ({"error_code": None}, "ck_payment_events_failed_event_has_error_code"),
    ],
)
def test_payment_event_schema_constraints(clean_database, changes, constraint_name):
    values = {
        **VALID_EVENT,
        "schema_version": 1,
        "source": "normalized_api",
        "source_event_id": "constraint-test",
        "payload_sha256": "a" * 64,
        "cohort_key": "upi:bank_x:phonepe:band_3",
        "occurred_at": datetime.now(UTC),
        **changes,
    }
    with Session(clean_database) as session:
        session.add(PaymentEventRow(**values))
        with pytest.raises(IntegrityError, match=constraint_name):
            session.commit()


def test_large_paise_value_and_utc_timestamp_round_trip(clean_database):
    session_factory = build_session_factory(engine=clean_database)
    event = PaymentEvent(
        **{
            **VALID_EVENT,
            "amount_paise": 9_223_372_036_854_775_807,
            "occurred_at": "2026-09-02T17:30:00+05:30",
        }
    )
    with SqlAlchemyUnitOfWork(session_factory) as unit_of_work:
        ingest_payment_event(event, "large-and-utc", unit_of_work)

    with Session(clean_database) as session:
        stored = session.scalar(select(PaymentEventRow))
        assert stored is not None
        assert stored.amount_paise == 9_223_372_036_854_775_807
        assert stored.occurred_at == datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
        assert stored.occurred_at.utcoffset().total_seconds() == 0


def test_new_event_and_audit_commit_atomically(clean_database):
    session_factory = build_session_factory(engine=clean_database)
    with SqlAlchemyUnitOfWork(session_factory) as unit_of_work:
        result = ingest_payment_event(PaymentEvent(**VALID_EVENT), "atomic-success", unit_of_work)
    assert result.created is True

    with Session(clean_database) as session:
        assert row_count(session, PaymentEventRow) == 1
        assert row_count(session, AuditRecordRow) == 1


def test_audit_failure_rolls_back_the_flushed_event(clean_database):
    class FailingAudits:
        def append(self, record):
            raise RuntimeError("synthetic audit failure")

        def list_for_payment_event(self, payment_event_id):
            return []

    session_factory = build_session_factory(engine=clean_database)
    with SqlAlchemyUnitOfWork(session_factory) as unit_of_work:
        unit_of_work.audit_records = FailingAudits()  # type: ignore[assignment]
        with pytest.raises(IngestionUnavailable):
            ingest_payment_event(PaymentEvent(**VALID_EVENT), "atomic-failure", unit_of_work)

    with Session(clean_database) as session:
        assert row_count(session, PaymentEventRow) == 0
        assert row_count(session, AuditRecordRow) == 0


def test_same_key_same_payload_returns_existing_row(clean_database):
    session_factory = build_session_factory(engine=clean_database)
    event = PaymentEvent(**VALID_EVENT)
    with SqlAlchemyUnitOfWork(session_factory) as first_uow:
        first = ingest_payment_event(event, "repeat-key", first_uow)
    with SqlAlchemyUnitOfWork(session_factory) as second_uow:
        second = ingest_payment_event(event, "repeat-key", second_uow)

    assert first.created is True
    assert second.created is False
    with Session(clean_database) as session:
        assert row_count(session, PaymentEventRow) == 1
        assert row_count(session, AuditRecordRow) == 1


def test_same_key_different_payload_returns_conflict(clean_database):
    session_factory = build_session_factory(engine=clean_database)
    with SqlAlchemyUnitOfWork(session_factory) as first_uow:
        ingest_payment_event(PaymentEvent(**VALID_EVENT), "conflict-key", first_uow)
    changed = {**VALID_EVENT, "amount_paise": 999_900}
    with SqlAlchemyUnitOfWork(session_factory) as second_uow:
        with pytest.raises(IdempotencyConflict):
            ingest_payment_event(PaymentEvent(**changed), "conflict-key", second_uow)

    with Session(clean_database) as session:
        assert row_count(session, PaymentEventRow) == 1
        assert row_count(session, AuditRecordRow) == 1


def test_two_concurrent_requests_share_one_database_event(clean_database):
    session_factory = build_session_factory(engine=clean_database)
    app = create_app()
    app.dependency_overrides[get_unit_of_work_factory] = lambda: (
        lambda: SqlAlchemyUnitOfWork(session_factory)
    )

    def send_request() -> tuple[int, dict[str, str]]:
        with TestClient(app) as client:
            response = client.post(
                "/v1/payments/events",
                headers={"Idempotency-Key": "concurrent-key"},
                json=VALID_EVENT,
            )
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: send_request(), range(2)))

    assert responses == [
        (202, {"accepted": "pay_1", "cohort_key": "upi:bank_x:phonepe:band_3"}),
        (202, {"accepted": "pay_1", "cohort_key": "upi:bank_x:phonepe:band_3"}),
    ]
    with Session(clean_database) as session:
        assert row_count(session, PaymentEventRow) == 1
        assert row_count(session, AuditRecordRow) == 1


def test_audit_orm_mutation_and_deletion_are_rejected(clean_database):
    session_factory = build_session_factory(engine=clean_database)
    with SqlAlchemyUnitOfWork(session_factory) as unit_of_work:
        ingest_payment_event(PaymentEvent(**VALID_EVENT), "immutable-audit", unit_of_work)

    with Session(clean_database) as session:
        audit = session.scalar(select(AuditRecordRow))
        assert audit is not None
        audit.actor_type = "changed"
        with pytest.raises(AuditRecordMutationError):
            session.commit()
        session.rollback()

    with Session(clean_database) as session:
        audit = session.scalar(select(AuditRecordRow))
        assert audit is not None
        session.delete(audit)
        with pytest.raises(AuditRecordMutationError):
            session.commit()
