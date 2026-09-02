from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from contextlib import AbstractContextManager
from dataclasses import asdict
from datetime import UTC, datetime
from types import TracebackType
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.requests import Request

from payrecover.api.dependencies import get_settings, get_unit_of_work_factory
from payrecover.api.main import create_app
from payrecover.api.razorpay import _read_limited_body, _validate_request_headers
from payrecover.config import Settings
from payrecover.domain.records import (
    NewAuditRecord,
    NewPaymentEvent,
    SourceIdentityScope,
    StoredAuditRecord,
    StoredPaymentEvent,
)
from payrecover.webhooks.razorpay import (
    MAX_WEBHOOK_BODY_BYTES,
    MalformedSupportedWebhook,
    UnsupportedWebhook,
    normalize_webhook,
    verify_webhook_signature,
)

SECRET = "synthetic-test-webhook-secret"
EVENT_ID = "evt_synthetic_001"


def payload(
    *,
    event: str = "payment.failed",
    status: str = "failed",
    method: str = "netbanking",
    currency: str = "INR",
    amount: int = 50_000,
    account_id: str = "acc_TestMerchant1",
    payment_id: str = "pay_TestPayment1",
) -> dict[str, object]:
    return {
        "entity": "event",
        "account_id": account_id,
        "event": event,
        "created_at": 1_700_000_000,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount,
                    "currency": currency,
                    "status": status,
                    "method": method,
                    "bank": "HDFC",
                    "wallet": "payzapp",
                    "card": {
                        "issuer": "HDFC",
                        "network": "Visa",
                        "name": "Synthetic Customer",
                        "last4": "1111",
                    },
                    "error_code": "BAD_REQUEST_ERROR",
                    "error_source": "bank",
                    "error_step": "payment_authorization",
                    "error_reason": "payment_failed",
                    "error_description": "Sensitive free-form description",
                    "email": "synthetic@example.invalid",
                    "contact": "+910000000000",
                    "vpa": "synthetic@invalid",
                    "notes": {"private": "must not survive"},
                }
            }
        },
    }


def raw_payload(**overrides: object) -> bytes:
    return json.dumps(payload(**overrides), separators=(",", ":")).encode()


def signature(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def settings() -> Settings:
    return Settings(
        _env_file=None,
        persistence_enabled=False,
        database_url=None,
        razorpay_webhook_secret=SecretStr(SECRET),
    )


class FakePaymentEvents:
    def __init__(self, *, fail: bool = False) -> None:
        self.rows: dict[tuple[str, ...], StoredPaymentEvent] = {}
        self.fail = fail

    def add_if_absent(
        self,
        event: NewPaymentEvent,
        *,
        identity_scope: SourceIdentityScope = SourceIdentityScope.MERCHANT,
    ) -> tuple[StoredPaymentEvent, bool]:
        if self.fail:
            raise RuntimeError("synthetic payment-event database failure")
        key = (
            (event.source, event.source_event_id)
            if identity_scope == SourceIdentityScope.PROVIDER
            else (event.source, event.merchant_id, event.source_event_id)
        )
        existing = self.rows.get(key)
        if existing is not None:
            return existing, False
        stored = StoredPaymentEvent(
            id=uuid4(), received_at=datetime.now(UTC), **asdict(event)
        )
        self.rows[key] = stored
        return stored, True

    def get_by_source_identity(self, source, merchant_id, source_event_id):
        return self.rows.get((source, merchant_id, source_event_id))


class FakeAudits:
    def __init__(self, *, fail_conflicts: bool = False) -> None:
        self.rows: list[StoredAuditRecord] = []
        self.fail_conflicts = fail_conflicts

    def append(self, record: NewAuditRecord) -> StoredAuditRecord:
        if self.fail_conflicts and record.event_type == "payment_event.idempotency_conflict":
            raise RuntimeError("synthetic conflict-audit failure")
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
    def __init__(self, payment_events: FakePaymentEvents, audits: FakeAudits) -> None:
        self.payment_events = payment_events
        self.audit_records = audits
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class FakeContext(AbstractContextManager[FakeUnitOfWork]):
    def __init__(self, unit_of_work: FakeUnitOfWork) -> None:
        self.unit_of_work = unit_of_work

    def __enter__(self) -> FakeUnitOfWork:
        return self.unit_of_work

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class FakeUnitOfWorkFactory:
    def __init__(self, *, fail_conflicts: bool = False, fail_events: bool = False) -> None:
        self.payment_events = FakePaymentEvents(fail=fail_events)
        self.audits = FakeAudits(fail_conflicts=fail_conflicts)
        self.units: list[FakeUnitOfWork] = []

    def __call__(self) -> FakeContext:
        unit_of_work = FakeUnitOfWork(self.payment_events, self.audits)
        self.units.append(unit_of_work)
        return FakeContext(unit_of_work)


def client_with_factory(factory: FakeUnitOfWorkFactory) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_settings] = settings
    app.dependency_overrides[get_unit_of_work_factory] = lambda: factory
    return TestClient(app)


def post_webhook(
    client: TestClient,
    body: bytes,
    *,
    event_id: str | None = EVENT_ID,
    supplied_signature: str | None = None,
):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if event_id is not None:
        headers["x-razorpay-event-id"] = event_id
    if supplied_signature is not None:
        headers["X-Razorpay-Signature"] = supplied_signature
    return client.post("/v1/webhooks/razorpay", content=body, headers=headers)


def request_with_headers(headers: list[tuple[bytes, bytes]]) -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": headers})


def test_valid_signature_uses_exact_raw_bytes():
    body = raw_payload()
    assert verify_webhook_signature(body, signature(body), SECRET) is True
    assert verify_webhook_signature(body + b" ", signature(body), SECRET) is False


def test_invalid_and_missing_signatures_are_rejected_before_database_use():
    factory = FakeUnitOfWorkFactory()
    client = client_with_factory(factory)
    body = raw_payload()
    assert post_webhook(client, body, supplied_signature="0" * 64).status_code == 401
    assert post_webhook(client, body).status_code == 401
    assert factory.units == []


def test_missing_event_id_is_rejected_before_database_use():
    factory = FakeUnitOfWorkFactory()
    body = raw_payload()
    response = post_webhook(
        client_with_factory(factory), body, event_id=None, supplied_signature=signature(body)
    )
    assert response.status_code == 400
    assert factory.units == []


@pytest.mark.parametrize("content_length", [b"-1", b"not-a-number", b"1.5"])
def test_malformed_or_negative_content_length_is_rejected(content_length: bytes):
    request = request_with_headers(
        [
            (b"content-type", b"application/json"),
            (b"x-razorpay-signature", b"a"),
            (b"x-razorpay-event-id", b"event-1"),
            (b"content-length", content_length),
        ]
    )
    with pytest.raises(HTTPException) as raised:
        _validate_request_headers(request)
    assert raised.value.status_code == 400


def test_duplicate_security_headers_are_rejected():
    request = request_with_headers(
        [
            (b"content-type", b"application/json"),
            (b"x-razorpay-signature", b"a"),
            (b"x-razorpay-signature", b"b"),
            (b"x-razorpay-event-id", b"event-1"),
            (b"x-razorpay-event-id", b"event-2"),
        ]
    )
    with pytest.raises(HTTPException) as raised:
        _validate_request_headers(request)
    assert raised.value.status_code == 401


def test_duplicate_event_id_headers_are_rejected():
    request = request_with_headers(
        [
            (b"content-type", b"application/json"),
            (b"x-razorpay-signature", b"a"),
            (b"x-razorpay-event-id", b"event-1"),
            (b"x-razorpay-event-id", b"event-2"),
        ]
    )
    with pytest.raises(HTTPException) as raised:
        _validate_request_headers(request)
    assert raised.value.status_code == 400


def test_content_type_must_be_json_but_allows_standard_parameters():
    valid = request_with_headers(
        [
            (b"content-type", b"application/json; charset=utf-8"),
            (b"x-razorpay-signature", b"a"),
            (b"x-razorpay-event-id", b"event-1"),
        ]
    )
    assert _validate_request_headers(valid) == ("a", "event-1")
    invalid = request_with_headers(
        [
            (b"content-type", b"text/plain"),
            (b"x-razorpay-signature", b"a"),
            (b"x-razorpay-event-id", b"event-1"),
        ]
    )
    with pytest.raises(HTTPException) as raised:
        _validate_request_headers(invalid)
    assert raised.value.status_code == 415


def test_oversized_declared_body_is_rejected_before_streaming():
    request = request_with_headers(
        [
            (b"content-type", b"application/json"),
            (b"x-razorpay-signature", b"a"),
            (b"x-razorpay-event-id", b"event-1"),
            (b"content-length", str(MAX_WEBHOOK_BODY_BYTES + 1).encode()),
        ]
    )
    with pytest.raises(HTTPException) as raised:
        _validate_request_headers(request)
    assert raised.value.status_code == 413


def test_streamed_body_limit_does_not_depend_on_content_length():
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {
                "type": "http.request",
                "body": b"x" * (MAX_WEBHOOK_BODY_BYTES + 1),
                "more_body": False,
            }
        return {"type": "http.disconnect"}

    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": []}, receive
    )
    with pytest.raises(HTTPException) as raised:
        asyncio.run(_read_limited_body(request))
    assert raised.value.status_code == 413


def test_malformed_json_is_rejected_only_after_valid_signature():
    factory = FakeUnitOfWorkFactory()
    body = b"{not-json"
    response = post_webhook(
        client_with_factory(factory), body, supplied_signature=signature(body)
    )
    assert response.status_code == 400
    assert factory.units == []


def test_captured_card_event_maps_only_operational_fields():
    event = normalize_webhook(
        raw_payload(event="payment.captured", status="captured", method="card"), EVENT_ID
    )
    assert event.status == "success"
    assert event.issuer == "HDFC"
    assert event.provider == "Visa"
    assert event.cohort_key == "card:HDFC:Visa:band_0"
    assert event.error_code is None
    assert event.error_code_availability == "not_applicable"
    assert event.latency_ms is None
    assert event.occurred_at == datetime.fromtimestamp(1_700_000_000, tz=UTC)
    serialized = json.dumps(asdict(event), default=str)
    for forbidden in (
        "Synthetic Customer",
        "synthetic@example.invalid",
        "+910000000000",
        "synthetic@invalid",
        "must not survive",
        "1111",
    ):
        assert forbidden not in serialized


def test_failed_netbanking_event_maps_failure_fields_without_latency():
    event = normalize_webhook(raw_payload(), EVENT_ID)
    assert event.status == "failed"
    assert event.issuer == "HDFC"
    assert event.issuer_availability == "provided"
    assert event.provider is None
    assert event.provider_availability == "not_applicable"
    assert event.error_code == "BAD_REQUEST_ERROR"
    assert event.error_source == "bank"
    assert event.error_step == "payment_authorization"
    assert event.error_reason == "payment_failed"
    assert event.latency_availability == "missing"
    assert event.cohort_key is None


def test_missing_conditional_razorpay_fields_remain_missing():
    value = payload(event="payment.failed", status="failed", method="card")
    entity = value["payload"]["payment"]["entity"]  # type: ignore[index]
    entity["card"] = {"issuer": None, "network": None}  # type: ignore[index]
    entity["error_code"] = None  # type: ignore[index]
    raw_body = json.dumps(value, separators=(",", ":")).encode()
    event = normalize_webhook(raw_body, EVENT_ID)
    assert event.issuer is None
    assert event.issuer_availability == "missing"
    assert event.provider is None
    assert event.provider_availability == "missing"
    assert event.error_code is None
    assert event.error_code_availability == "missing"


def test_upi_and_wallet_availability_states_are_explicit():
    upi = normalize_webhook(raw_payload(method="upi"), EVENT_ID)
    wallet = normalize_webhook(raw_payload(method="wallet"), EVENT_ID)
    assert (upi.issuer_availability, upi.provider_availability) == ("missing", "redacted")
    assert upi.provider is None
    assert wallet.issuer_availability == "not_applicable"
    assert wallet.provider == "payzapp"


@pytest.mark.parametrize(
    "overrides",
    [
        {"event": "refund.created"},
        {"method": "paylater"},
        {"currency": "USD"},
    ],
)
def test_unsupported_inputs_do_not_enter_persistence(overrides: dict[str, object]):
    factory = FakeUnitOfWorkFactory()
    body = raw_payload(**overrides)
    response = post_webhook(
        client_with_factory(factory), body, supplied_signature=signature(body)
    )
    assert response.status_code == 204
    assert factory.units == []


def test_outer_event_and_nested_status_contradiction_is_rejected():
    with pytest.raises(MalformedSupportedWebhook):
        normalize_webhook(
            raw_payload(event="payment.captured", status="failed", method="card"), EVENT_ID
        )


def test_validation_error_does_not_retain_customer_payload_details():
    value = payload()
    entity = value["payload"]["payment"]["entity"]  # type: ignore[index]
    del entity["id"]  # type: ignore[index]
    raw_body = json.dumps(value, separators=(",", ":")).encode()
    with pytest.raises(MalformedSupportedWebhook) as raised:
        normalize_webhook(raw_body, EVENT_ID)
    assert raised.value.__cause__ is None
    assert "synthetic@example.invalid" not in str(raised.value)


def test_valid_event_and_exact_replay_create_one_event_and_audit():
    factory = FakeUnitOfWorkFactory()
    client = client_with_factory(factory)
    body = raw_payload()
    first = post_webhook(client, body, supplied_signature=signature(body))
    replay = post_webhook(client, body, supplied_signature=signature(body))
    assert first.json() == {"status": "accepted"}
    assert replay.json() == {"status": "duplicate"}
    assert len(factory.payment_events.rows) == 1
    assert len(factory.audits.rows) == 1


def test_conflicting_replay_is_acknowledged_and_appends_sanitized_audit(caplog):
    factory = FakeUnitOfWorkFactory()
    client = client_with_factory(factory)
    original_body = raw_payload(amount=50_000)
    conflict_body = raw_payload(amount=60_000)
    post_webhook(client, original_body, supplied_signature=signature(original_body))

    response = post_webhook(client, conflict_body, supplied_signature=signature(conflict_body))

    assert response.status_code == 200
    assert response.json() == {"status": "conflict_acknowledged"}
    assert len(factory.payment_events.rows) == 1
    original = next(iter(factory.payment_events.rows.values()))
    assert original.amount_paise == 50_000
    assert len(factory.audits.rows) == 2
    conflict_audit = factory.audits.rows[-1]
    assert conflict_audit.event_type == "payment_event.idempotency_conflict"
    assert conflict_audit.payment_event_id == original.id
    assert set(conflict_audit.details) == {
        "detected_at",
        "existing_payload_sha256",
        "incoming_payload_sha256",
        "provider_event_id_sha256",
        "source",
    }
    audit_text = json.dumps(conflict_audit.details)
    for forbidden in (EVENT_ID, "signature", "email", "contact", "notes", "raw"):
        assert forbidden not in audit_text
    assert "Razorpay webhook idempotency conflict acknowledged" in caplog.text


def test_each_conflicting_delivery_gets_exactly_one_conflict_audit():
    factory = FakeUnitOfWorkFactory()
    client = client_with_factory(factory)
    original_body = raw_payload(amount=50_000)
    conflict_body = raw_payload(amount=60_000)
    post_webhook(client, original_body, supplied_signature=signature(original_body))
    post_webhook(client, conflict_body, supplied_signature=signature(conflict_body))
    post_webhook(client, conflict_body, supplied_signature=signature(conflict_body))
    assert [row.event_type for row in factory.audits.rows].count(
        "payment_event.idempotency_conflict"
    ) == 2


def test_conflict_audit_database_failure_returns_503():
    factory = FakeUnitOfWorkFactory(fail_conflicts=True)
    client = client_with_factory(factory)
    original_body = raw_payload(amount=50_000)
    conflict_body = raw_payload(amount=60_000)
    post_webhook(client, original_body, supplied_signature=signature(original_body))
    response = post_webhook(client, conflict_body, supplied_signature=signature(conflict_body))
    assert response.status_code == 503
    assert response.json() == {"detail": "Webhook event could not be persisted"}


def test_payment_event_database_failure_returns_503():
    factory = FakeUnitOfWorkFactory(fail_events=True)
    body = raw_payload()
    response = post_webhook(
        client_with_factory(factory), body, supplied_signature=signature(body)
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Webhook event could not be persisted"}


def test_secret_is_not_exposed_in_settings_or_configuration_errors():
    configured = settings()
    assert SECRET not in repr(configured)
    missing = Settings(
        _env_file=None,
        persistence_enabled=False,
        database_url=None,
        razorpay_webhook_secret=None,
    )
    with pytest.raises(RuntimeError) as raised:
        missing.require_razorpay_webhook_secret()
    assert SECRET not in str(raised.value)


def test_unconfigured_secret_returns_safe_503_without_database_use():
    factory = FakeUnitOfWorkFactory()
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(
        _env_file=None,
        persistence_enabled=False,
        database_url=None,
        razorpay_webhook_secret=None,
    )
    app.dependency_overrides[get_unit_of_work_factory] = lambda: factory
    body = raw_payload()
    response = post_webhook(TestClient(app), body, supplied_signature=signature(body))
    assert response.status_code == 503
    assert factory.units == []


def test_normalizer_raises_unsupported_without_exposing_input():
    with pytest.raises(UnsupportedWebhook, match="Unsupported event type"):
        normalize_webhook(raw_payload(event="refund.created"), EVENT_ID)
