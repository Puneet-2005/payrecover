from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from payrecover.domain.models import FieldAvailability


class SourceIdentityScope(StrEnum):
    MERCHANT = "merchant"
    PROVIDER = "provider"


@dataclass(frozen=True, slots=True)
class OperationalPaymentEvent:
    schema_version: int
    source: str
    source_event_id: str
    source_event_type: str
    identity_scope: SourceIdentityScope
    payment_id: str
    merchant_id: str
    method: str
    issuer: str | None
    issuer_availability: FieldAvailability
    provider: str | None
    provider_availability: FieldAvailability
    amount_paise: int
    status: str
    error_code: str | None
    error_code_availability: FieldAvailability
    error_source: str | None
    error_step: str | None
    error_reason: str | None
    latency_ms: int | None
    latency_availability: FieldAvailability
    cohort_key: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class NewPaymentEvent:
    schema_version: int
    source: str
    source_event_id: str
    payload_sha256: str
    payment_id: str
    merchant_id: str
    method: str
    issuer: str | None
    issuer_availability: str
    provider: str | None
    provider_availability: str
    amount_paise: int
    status: str
    error_code: str | None
    error_code_availability: str
    error_source: str | None
    error_step: str | None
    error_reason: str | None
    latency_ms: int | None
    latency_availability: str
    cohort_key: str | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class StoredPaymentEvent:
    id: UUID
    schema_version: int
    source: str
    source_event_id: str
    payload_sha256: str
    payment_id: str
    merchant_id: str
    method: str
    issuer: str | None
    issuer_availability: str
    provider: str | None
    provider_availability: str
    amount_paise: int
    status: str
    error_code: str | None
    error_code_availability: str
    error_source: str | None
    error_step: str | None
    error_reason: str | None
    latency_ms: int | None
    latency_availability: str
    cohort_key: str | None
    occurred_at: datetime
    received_at: datetime


@dataclass(frozen=True, slots=True)
class NewAuditRecord:
    correlation_id: UUID
    payment_event_id: UUID | None
    event_type: str
    actor_type: str
    actor_ref_digest: str | None
    details: dict[str, Any]
    incident_id: int | None = None


@dataclass(frozen=True, slots=True)
class StoredAuditRecord:
    id: int
    correlation_id: UUID
    payment_event_id: UUID | None
    event_type: str
    actor_type: str
    actor_ref_digest: str | None
    details: dict[str, Any]
    recorded_at: datetime
    incident_id: int | None = None
