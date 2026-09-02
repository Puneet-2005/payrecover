from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID


@dataclass(frozen=True, slots=True)
class NewPaymentEvent:
    schema_version: int
    source: str
    source_event_id: str
    payload_sha256: str
    payment_id: str
    merchant_id: str
    method: str
    issuer: str
    provider: str
    amount_paise: int
    status: str
    error_code: str | None
    latency_ms: int
    cohort_key: str
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
    issuer: str
    provider: str
    amount_paise: int
    status: str
    error_code: str | None
    latency_ms: int
    cohort_key: str
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
