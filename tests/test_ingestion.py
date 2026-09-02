from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from payrecover.domain.models import PaymentEvent
from payrecover.domain.records import (
    NewAuditRecord,
    NewPaymentEvent,
    StoredAuditRecord,
    StoredPaymentEvent,
)
from payrecover.services.ingestion import (
    IdempotencyConflict,
    IngestionUnavailable,
    InvalidIdempotencyKey,
    canonical_payload_sha256,
    ingest_payment_event,
)


def event_data(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
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
    values.update(overrides)
    return values


def stored_event(event: NewPaymentEvent) -> StoredPaymentEvent:
    now = datetime.now(UTC)
    return StoredPaymentEvent(id=uuid4(), received_at=now, **asdict(event))


class FakePaymentEvents:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], StoredPaymentEvent] = {}

    def add_if_absent(
        self, event: NewPaymentEvent, *, identity_scope=None
    ) -> tuple[StoredPaymentEvent, bool]:
        del identity_scope
        identity = (event.source, event.merchant_id, event.source_event_id)
        if identity in self.rows:
            return self.rows[identity], False
        row = stored_event(event)
        self.rows[identity] = row
        return row, True

    def get_by_source_identity(
        self, source: str, merchant_id: str, source_event_id: str
    ) -> StoredPaymentEvent | None:
        return self.rows.get((source, merchant_id, source_event_id))


class FakeAudits:
    def __init__(self, *, fail: bool = False) -> None:
        self.rows: list[StoredAuditRecord] = []
        self.fail = fail

    def append(self, record: NewAuditRecord) -> StoredAuditRecord:
        if self.fail:
            raise RuntimeError("synthetic audit failure")
        stored = StoredAuditRecord(
            id=len(self.rows) + 1,
            recorded_at=datetime.now(UTC),
            **asdict(record),
        )
        self.rows.append(stored)
        return stored

    def list_for_payment_event(self, payment_event_id):
        return [row for row in self.rows if row.payment_event_id == payment_event_id]


class FakeUnitOfWork:
    def __init__(self, *, audit_failure: bool = False) -> None:
        self.payment_events = FakePaymentEvents()
        self.audit_records = FakeAudits(fail=audit_failure)
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_naive_event_timestamp_is_rejected():
    with pytest.raises(ValueError, match="must include a timezone"):
        PaymentEvent(**event_data(occurred_at=datetime(2026, 9, 2, 12, 0)))


def test_aware_event_timestamp_is_normalized_to_utc():
    event = PaymentEvent(**event_data(occurred_at="2026-09-02T17:30:00+05:30"))
    assert event.occurred_at == datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def test_generated_timestamp_is_excluded_from_canonical_digest():
    first = PaymentEvent(**event_data())
    second = PaymentEvent(**event_data())
    assert "occurred_at" not in first.model_fields_set
    assert "occurred_at" not in second.model_fields_set
    assert canonical_payload_sha256(first) == canonical_payload_sha256(second)


def test_same_idempotency_key_and_payload_is_replayed_safely():
    unit_of_work = FakeUnitOfWork()
    event = PaymentEvent(**event_data())

    first = ingest_payment_event(event, "idem-1", unit_of_work)
    second = ingest_payment_event(event, "idem-1", unit_of_work)

    assert first.payment_id == second.payment_id
    assert first.created is True
    assert second.created is False
    assert len(unit_of_work.payment_events.rows) == 1
    assert len(unit_of_work.audit_records.rows) == 1
    assert unit_of_work.commits == 2


def test_same_idempotency_key_and_different_payload_conflicts():
    unit_of_work = FakeUnitOfWork()
    ingest_payment_event(PaymentEvent(**event_data()), "idem-1", unit_of_work)

    with pytest.raises(IdempotencyConflict):
        ingest_payment_event(
            PaymentEvent(**event_data(amount_paise=350_000)), "idem-1", unit_of_work
        )

    assert len(unit_of_work.payment_events.rows) == 1
    assert unit_of_work.rollbacks == 1


def test_missing_idempotency_key_creates_distinct_events():
    unit_of_work = FakeUnitOfWork()
    event = PaymentEvent(**event_data())
    ingest_payment_event(event, None, unit_of_work)
    ingest_payment_event(event, None, unit_of_work)
    assert len(unit_of_work.payment_events.rows) == 2


@pytest.mark.parametrize("key", ["", "x" * 129, "contains a space", "é"])
def test_invalid_idempotency_key_is_rejected(key: str):
    with pytest.raises(InvalidIdempotencyKey):
        ingest_payment_event(PaymentEvent(**event_data()), key, FakeUnitOfWork())


def test_audit_failure_rolls_back_and_is_unavailable():
    unit_of_work = FakeUnitOfWork(audit_failure=True)
    with pytest.raises(IngestionUnavailable):
        ingest_payment_event(PaymentEvent(**event_data()), "idem-1", unit_of_work)
    assert unit_of_work.commits == 0
    assert unit_of_work.rollbacks == 1
