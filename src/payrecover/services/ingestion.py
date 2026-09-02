from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

from payrecover.domain.models import PaymentEvent
from payrecover.domain.records import NewAuditRecord, NewPaymentEvent
from payrecover.domain.repositories import UnitOfWork

NORMALIZED_API_SOURCE = "normalized_api"
MAX_IDEMPOTENCY_KEY_LENGTH = 128


class InvalidIdempotencyKey(ValueError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


class IngestionUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class IngestionResult:
    payment_id: str
    cohort_key: str
    created: bool


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
        provider=event.provider,
        amount_paise=event.amount_paise,
        status=event.status.value,
        error_code=event.error_code,
        latency_ms=event.latency_ms,
        cohort_key=event.cohort_key,
        occurred_at=event.occurred_at,
    )

    try:
        stored, created = unit_of_work.payment_events.add_if_absent(new_event)
        if not created and stored.payload_sha256 != payload_digest:
            raise IdempotencyConflict("Idempotency-Key was already used for another payload")
        if created:
            unit_of_work.audit_records.append(
                NewAuditRecord(
                    correlation_id=uuid4(),
                    payment_event_id=stored.id,
                    event_type="payment_event.ingested",
                    actor_type="normalized_api",
                    actor_ref_digest=None,
                    details={"schema_version": stored.schema_version, "source": stored.source},
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
