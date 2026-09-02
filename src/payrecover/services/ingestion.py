from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from payrecover.domain.models import FieldAvailability, PaymentEvent
from payrecover.domain.records import (
    NewAuditRecord,
    NewPaymentEvent,
    OperationalPaymentEvent,
    SourceIdentityScope,
    StoredPaymentEvent,
)
from payrecover.domain.repositories import UnitOfWork

NORMALIZED_API_SOURCE = "normalized_api"
MAX_IDEMPOTENCY_KEY_LENGTH = 128


class InvalidIdempotencyKey(ValueError):
    pass


class IdempotencyConflict(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        existing_event: StoredPaymentEvent,
        incoming_payload_sha256: str,
        source_event_id: str,
    ) -> None:
        super().__init__(message)
        self.existing_event = existing_event
        self.incoming_payload_sha256 = incoming_payload_sha256
        self.source_event_id = source_event_id


class IngestionUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IngestionResult:
    payment_id: str
    cohort_key: str | None
    created: bool


@dataclass(frozen=True, slots=True)
class ConflictAuditResult:
    correlation_id: UUID
    provider_event_id_sha256: str


def _validate_idempotency_key(value: str) -> str:
    if not 1 <= len(value) <= MAX_IDEMPOTENCY_KEY_LENGTH:
        raise InvalidIdempotencyKey("Idempotency-Key must contain 1 to 128 characters")
    if any(ord(character) < 0x21 or ord(character) > 0x7E for character in value):
        raise InvalidIdempotencyKey("Idempotency-Key must contain visible ASCII characters")
    return value


def canonical_payload_sha256(event: PaymentEvent) -> str:
    payload: dict[str, object] = {
        "amount_paise": event.amount_paise,
        "error_code": event.error_code,
        "issuer": event.issuer,
        "latency_ms": event.latency_ms,
        "merchant_id": event.merchant_id,
        "method": event.method,
        "payment_id": event.payment_id,
        "provider": event.provider,
        "status": event.status.value,
    }
    if "occurred_at" in event.model_fields_set:
        payload["occurred_at"] = event.occurred_at.isoformat().replace("+00:00", "Z")

    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def canonical_operational_payload_sha256(event: OperationalPaymentEvent) -> str:
    payload: dict[str, object] = {
        "amount_paise": event.amount_paise,
        "cohort_key": event.cohort_key,
        "error_code": event.error_code,
        "error_code_availability": event.error_code_availability.value,
        "error_reason": event.error_reason,
        "error_source": event.error_source,
        "error_step": event.error_step,
        "issuer": event.issuer,
        "issuer_availability": event.issuer_availability.value,
        "latency_availability": event.latency_availability.value,
        "latency_ms": event.latency_ms,
        "merchant_id": event.merchant_id,
        "method": event.method,
        "occurred_at": event.occurred_at.isoformat().replace("+00:00", "Z"),
        "payment_id": event.payment_id,
        "provider": event.provider,
        "provider_availability": event.provider_availability.value,
        "schema_version": event.schema_version,
        "source": event.source,
        "source_event_type": event.source_event_type,
        "status": event.status,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _persist_payment_event(
    new_event: NewPaymentEvent,
    unit_of_work: UnitOfWork,
    *,
    identity_scope: SourceIdentityScope,
    source_event_type: str | None = None,
    conflict_message: str = "Idempotency identity was already used for another payload",
) -> IngestionResult:
    try:
        stored, created = unit_of_work.payment_events.add_if_absent(
            new_event, identity_scope=identity_scope
        )
        if not created and stored.payload_sha256 != new_event.payload_sha256:
            raise IdempotencyConflict(
                conflict_message,
                existing_event=stored,
                incoming_payload_sha256=new_event.payload_sha256,
                source_event_id=new_event.source_event_id,
            )
        if created:
            details: dict[str, object] = {
                "schema_version": stored.schema_version,
                "source": stored.source,
            }
            if source_event_type is not None:
                details["source_event_type"] = source_event_type
            unit_of_work.audit_records.append(
                NewAuditRecord(
                    correlation_id=uuid4(),
                    payment_event_id=stored.id,
                    event_type="payment_event.ingested",
                    actor_type=stored.source,
                    actor_ref_digest=None,
                    details=details,
                )
            )
        unit_of_work.commit()
    except IdempotencyConflict:
        unit_of_work.rollback()
        raise
    except Exception as exc:
        unit_of_work.rollback()
        raise IngestionUnavailable("Payment event could not be committed") from exc

    return IngestionResult(
        payment_id=stored.payment_id,
        cohort_key=stored.cohort_key,
        created=created,
    )


def ingest_payment_event(
    event: PaymentEvent,
    idempotency_key: str | None,
    unit_of_work: UnitOfWork,
) -> IngestionResult:
    source_event_id = (
        _validate_idempotency_key(idempotency_key)
        if idempotency_key is not None
        else f"internal:{uuid4().hex}"
    )
    payload_digest = canonical_payload_sha256(event)
    new_event = NewPaymentEvent(
        schema_version=1,
        source=NORMALIZED_API_SOURCE,
        source_event_id=source_event_id,
        payload_sha256=payload_digest,
        payment_id=event.payment_id,
        merchant_id=event.merchant_id,
        method=event.method,
        issuer=event.issuer,
        issuer_availability=FieldAvailability.PROVIDED.value,
        provider=event.provider,
        provider_availability=FieldAvailability.PROVIDED.value,
        amount_paise=event.amount_paise,
        status=event.status.value,
        error_code=event.error_code,
        error_code_availability=(
            FieldAvailability.PROVIDED.value
            if event.error_code is not None
            else FieldAvailability.NOT_APPLICABLE.value
        ),
        error_source=None,
        error_step=None,
        error_reason=None,
        latency_ms=event.latency_ms,
        latency_availability=FieldAvailability.PROVIDED.value,
        cohort_key=event.cohort_key,
        occurred_at=event.occurred_at,
    )

    return _persist_payment_event(
        new_event,
        unit_of_work,
        identity_scope=SourceIdentityScope.MERCHANT,
        conflict_message="Idempotency-Key was already used for another payload",
    )


def ingest_operational_payment_event(
    event: OperationalPaymentEvent,
    unit_of_work: UnitOfWork,
) -> IngestionResult:
    payload_digest = canonical_operational_payload_sha256(event)
    new_event = NewPaymentEvent(
        schema_version=event.schema_version,
        source=event.source,
        source_event_id=event.source_event_id,
        payload_sha256=payload_digest,
        payment_id=event.payment_id,
        merchant_id=event.merchant_id,
        method=event.method,
        issuer=event.issuer,
        issuer_availability=event.issuer_availability.value,
        provider=event.provider,
        provider_availability=event.provider_availability.value,
        amount_paise=event.amount_paise,
        status=event.status,
        error_code=event.error_code,
        error_code_availability=event.error_code_availability.value,
        error_source=event.error_source,
        error_step=event.error_step,
        error_reason=event.error_reason,
        latency_ms=event.latency_ms,
        latency_availability=event.latency_availability.value,
        cohort_key=event.cohort_key,
        occurred_at=event.occurred_at,
    )
    return _persist_payment_event(
        new_event,
        unit_of_work,
        identity_scope=event.identity_scope,
        source_event_type=event.source_event_type,
    )


def append_idempotency_conflict_audit(
    conflict: IdempotencyConflict,
    unit_of_work: UnitOfWork,
    *,
    detected_at: datetime | None = None,
) -> ConflictAuditResult:
    detection_time = (detected_at or datetime.now(UTC)).astimezone(UTC)
    correlation_id = uuid4()
    provider_event_id_sha256 = sha256(conflict.source_event_id.encode("utf-8")).hexdigest()
    try:
        unit_of_work.audit_records.append(
            NewAuditRecord(
                correlation_id=correlation_id,
                payment_event_id=conflict.existing_event.id,
                event_type="payment_event.idempotency_conflict",
                actor_type=conflict.existing_event.source,
                actor_ref_digest=provider_event_id_sha256,
                details={
                    "detected_at": detection_time.isoformat().replace("+00:00", "Z"),
                    "existing_payload_sha256": conflict.existing_event.payload_sha256,
                    "incoming_payload_sha256": conflict.incoming_payload_sha256,
                    "provider_event_id_sha256": provider_event_id_sha256,
                    "source": conflict.existing_event.source,
                },
            )
        )
        unit_of_work.commit()
    except Exception as exc:
        unit_of_work.rollback()
        raise IngestionUnavailable("Idempotency conflict audit could not be committed") from exc
    return ConflictAuditResult(
        correlation_id=correlation_id,
        provider_event_id_sha256=provider_event_id_sha256,
    )
