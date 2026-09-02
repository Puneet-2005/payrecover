from __future__ import annotations

import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from alembic import command
from payrecover.api.dependencies import get_settings, get_unit_of_work_factory
from payrecover.api.main import create_app
from payrecover.config import Settings
from payrecover.infrastructure.database.models import AuditRecordRow, PaymentEventRow
from payrecover.infrastructure.database.session import build_engine, build_session_factory
from payrecover.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SECRET = "synthetic-integration-webhook-secret"
EVENT_ID = "evt_integration_001"


def payload(
    *,
    amount: int = 50_000,
    account_id: str = "acc_IntegrationMerchant",
    payment_id: str = "pay_IntegrationPayment",
) -> dict[str, object]:
    return {
        "entity": "event",
        "account_id": account_id,
        "event": "payment.failed",
        "created_at": 1_700_000_000,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount,
                    "currency": "INR",
                    "status": "failed",
                    "method": "upi",
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_source": "bank",
                    "error_step": "payment_authorization",
                    "error_reason": "payment_failed",
                    "email": "synthetic@example.invalid",
                    "contact": "+910000000000",
                    "vpa": "synthetic@invalid",
                    "notes": {"private": "must not survive"},
                }
            }
        },
    }


def body(**overrides: object) -> bytes:
    return json.dumps(payload(**overrides), separators=(",", ":")).encode()


def signature(raw_body: bytes) -> str:
    return hmac.new(SECRET.encode(), raw_body, hashlib.sha256).hexdigest()


def webhook_client(database_engine) -> TestClient:
    session_factory = build_session_factory(engine=database_engine)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        persistence_enabled=False,
        database_url=None,
        razorpay_webhook_secret=SecretStr(SECRET),
    )
    app.dependency_overrides[get_unit_of_work_factory] = lambda: (
        lambda: SqlAlchemyUnitOfWork(session_factory)
    )
    return TestClient(app)


def post(client: TestClient, raw_body: bytes, *, event_id: str = EVENT_ID):
    return client.post(
        "/v1/webhooks/razorpay",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature(raw_body),
            "x-razorpay-event-id": event_id,
        },
    )


def counts(database_engine) -> tuple[int, int]:
    with Session(database_engine) as session:
        events = session.scalar(select(func.count()).select_from(PaymentEventRow)) or 0
        audits = session.scalar(select(func.count()).select_from(AuditRecordRow)) or 0
    return events, audits


def test_valid_signed_event_persists_allowlisted_event_and_audit(clean_database):
    raw_body = body()
    response = post(webhook_client(clean_database), raw_body)
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}

    with Session(clean_database) as session:
        event = session.scalar(select(PaymentEventRow))
        audit = session.scalar(select(AuditRecordRow))
        assert event is not None
        assert audit is not None
        assert event.schema_version == 2
        assert event.source == "razorpay_webhook"
        assert event.source_event_id == EVENT_ID
        assert event.method == "upi"
        assert event.issuer is None
        assert event.issuer_availability == "missing"
        assert event.provider is None
        assert event.provider_availability == "redacted"
        assert event.latency_ms is None
        assert event.latency_availability == "missing"
        assert event.cohort_key is None
        assert not hasattr(event, "error_description")
        assert audit.event_type == "payment_event.ingested"
        serialized = json.dumps(audit.details)
        for forbidden in (
            "synthetic@example.invalid",
            "+910000000000",
            "synthetic@invalid",
            "must not survive",
        ):
            assert forbidden not in serialized


def test_exact_replay_creates_no_duplicate_or_second_audit(clean_database):
    client = webhook_client(clean_database)
    raw_body = body()
    assert post(client, raw_body).json() == {"status": "accepted"}
    assert post(client, raw_body).json() == {"status": "duplicate"}
    assert counts(clean_database) == (1, 1)


def test_conflicting_replay_preserves_original_and_appends_sanitized_audit(clean_database):
    client = webhook_client(clean_database)
    original_body = body(amount=50_000)
    conflicting_body = body(amount=60_000)
    post(client, original_body)
    response = post(client, conflicting_body)

    assert response.status_code == 200
    assert response.json() == {"status": "conflict_acknowledged"}
    with Session(clean_database) as session:
        events = list(session.scalars(select(PaymentEventRow)))
        audits = list(session.scalars(select(AuditRecordRow).order_by(AuditRecordRow.id)))
        assert len(events) == 1
        assert events[0].amount_paise == 50_000
        assert len(audits) == 2
        conflict_audit = audits[-1]
        assert conflict_audit.event_type == "payment_event.idempotency_conflict"
        assert conflict_audit.payment_event_id == events[0].id
        assert set(conflict_audit.details) == {
            "detected_at",
            "existing_payload_sha256",
            "incoming_payload_sha256",
            "provider_event_id_sha256",
            "source",
        }
        audit_text = json.dumps(conflict_audit.details)
        for forbidden in (
            EVENT_ID,
            "synthetic@example.invalid",
            "+910000000000",
            "synthetic@invalid",
            "must not survive",
            "signature",
        ):
            assert forbidden not in audit_text


def test_provider_event_id_is_globally_unique_across_account_ids(clean_database):
    client = webhook_client(clean_database)
    first_body = body(account_id="acc_IntegrationMerchantA")
    conflicting_body = body(account_id="acc_IntegrationMerchantB")
    assert post(client, first_body).json() == {"status": "accepted"}
    assert post(client, conflicting_body).json() == {"status": "conflict_acknowledged"}
    assert counts(clean_database) == (1, 2)


def test_concurrent_duplicate_deliveries_create_one_event_and_audit(clean_database):
    raw_body = body()

    def send(_: int) -> tuple[int, dict[str, str]]:
        with webhook_client(clean_database) as client:
            response = post(client, raw_body)
            return response.status_code, response.json()

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(send, range(2)))

    assert sorted(status for status, _ in responses) == [200, 200]
    assert sorted(result["status"] for _, result in responses) == ["accepted", "duplicate"]
    assert counts(clean_database) == (1, 1)


def test_invalid_signature_creates_no_database_rows(clean_database):
    raw_body = body()
    response = webhook_client(clean_database).post(
        "/v1/webhooks/razorpay",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": "0" * 64,
            "x-razorpay-event-id": EVENT_ID,
        },
    )
    assert response.status_code == 401
    assert counts(clean_database) == (0, 0)


def test_conflict_audit_database_failure_returns_503_without_false_acknowledgement(
    clean_database,
):
    raw_original = body(amount=50_000)
    assert post(webhook_client(clean_database), raw_original).status_code == 200
    session_factory = build_session_factory(engine=clean_database)

    class FailingConflictAudits:
        def append(self, record):
            raise RuntimeError("synthetic conflict-audit database failure")

        def list_for_payment_event(self, payment_event_id):
            return []

    class FailingConflictAuditUnitOfWork(SqlAlchemyUnitOfWork):
        def __enter__(self):
            entered = super().__enter__()
            entered.audit_records = FailingConflictAudits()  # type: ignore[assignment]
            return entered

    calls = 0

    def factory():
        nonlocal calls
        calls += 1
        if calls == 1:
            return SqlAlchemyUnitOfWork(session_factory)
        return FailingConflictAuditUnitOfWork(session_factory)

    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        persistence_enabled=False,
        database_url=None,
        razorpay_webhook_secret=SecretStr(SECRET),
    )
    app.dependency_overrides[get_unit_of_work_factory] = lambda: factory
    conflicting_body = body(amount=60_000)
    response = post(TestClient(app), conflicting_body)
    assert response.status_code == 503
    assert response.json() == {"detail": "Webhook event could not be persisted"}
    assert counts(clean_database) == (1, 1)


def test_phase2b_migration_upgrade_downgrade_upgrade_cycle(
    clean_database, migrated_database_url: str
):
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", migrated_database_url)
    cycle_engine = build_engine(migrated_database_url)
    try:
        assert "issuer_availability" in {
            column["name"] for column in inspect(cycle_engine).get_columns("payment_events")
        }
        command.downgrade(config, "-1")
        assert "issuer_availability" not in {
            column["name"] for column in inspect(cycle_engine).get_columns("payment_events")
        }
        command.upgrade(config, "head")
        assert "issuer_availability" in {
            column["name"] for column in inspect(cycle_engine).get_columns("payment_events")
        }
    finally:
        command.upgrade(config, "head")
        cycle_engine.dispose()
