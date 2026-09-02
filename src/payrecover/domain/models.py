from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256

from pydantic import BaseModel, Field, field_validator, model_validator


class PaymentStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


class FieldAvailability(StrEnum):
    PROVIDED = "provided"
    MISSING = "missing"
    NOT_APPLICABLE = "not_applicable"
    REDACTED = "redacted"


class RecoveryAction(StrEnum):
    RETRY_NOW = "retry_now"
    WAIT_AND_RETRY = "wait_and_retry"
    NOTIFY_CUSTOMER = "notify_customer"
    ESCALATE = "escalate"
    NO_ACTION = "no_action"


class PaymentEvent(BaseModel):
    payment_id: str = Field(min_length=3, max_length=100)
    merchant_id: str = Field(min_length=3, max_length=100)
    method: str
    issuer: str
    provider: str
    amount_paise: int = Field(gt=0)
    status: PaymentStatus
    error_code: str | None = None
    latency_ms: int = Field(ge=0)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator("occurred_at")
    @classmethod
    def require_timezone_and_normalize_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_failure_reason(self) -> PaymentEvent:
        if self.status == PaymentStatus.FAILED and not self.error_code:
            raise ValueError("failed payments require error_code")
        return self

    @property
    def cohort_key(self) -> str:
        band = min(self.amount_paise // 100_000, 10)
        return f"{self.method}:{self.issuer}:{self.provider}:band_{band}"


class CohortSnapshot(BaseModel):
    cohort_key: str
    baseline_success_rate: float = Field(ge=0, le=1)
    observed_success_rate: float = Field(ge=0, le=1)
    sample_size: int = Field(gt=0)
    failed_count: int = Field(ge=0)
    failed_amount_paise: int = Field(ge=0)
    dominant_error_code: str | None = None


class DetectionResult(BaseModel):
    degraded: bool
    severity: str
    absolute_drop: float
    z_score: float
    reason: str


class RecoveryDecision(BaseModel):
    action: RecoveryAction
    permitted: bool
    retry_after_seconds: int | None = None
    max_attempts: int = Field(ge=0, le=3)
    reason: str
    policy_version: str = "recovery-policy-v1"


def recovery_idempotency_key(payment_id: str, action: RecoveryAction, attempt: int) -> str:
    raw = f"{payment_id}:{action.value}:{attempt}".encode()
    return sha256(raw).hexdigest()
