from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from payrecover.domain.models import FieldAvailability, PaymentStatus
from payrecover.domain.records import OperationalPaymentEvent, SourceIdentityScope

RAZORPAY_SOURCE = "razorpay_webhook"
MAX_WEBHOOK_BODY_BYTES = 256 * 1024
SUPPORTED_EVENT_TYPES = frozenset({"payment.captured", "payment.failed"})
SUPPORTED_PAYMENT_METHODS = frozenset({"card", "emi", "netbanking", "upi", "wallet"})


class MalformedWebhookJson(ValueError):
    pass


class MalformedSupportedWebhook(ValueError):
    pass


class UnsupportedWebhook(ValueError):
    pass


class _RazorpayModel(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)


class RazorpayEventDiscriminator(_RazorpayModel):
    event: str = Field(min_length=1, max_length=64)


class RazorpayCardFields(_RazorpayModel):
    issuer: str | None = Field(default=None, max_length=100)
    network: str | None = Field(default=None, max_length=100)


class RazorpayPaymentEntity(_RazorpayModel):
    id: str = Field(min_length=3, max_length=100, pattern=r"^pay_[A-Za-z0-9]+$")
    entity: Literal["payment"]
    amount: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    status: str = Field(min_length=1, max_length=16)
    method: str = Field(min_length=1, max_length=32)
    bank: str | None = Field(default=None, max_length=100)
    wallet: str | None = Field(default=None, max_length=100)
    card: RazorpayCardFields | None = None
    error_code: str | None = Field(default=None, max_length=100)
    error_source: str | None = Field(default=None, max_length=64)
    error_step: str | None = Field(default=None, max_length=100)
    error_reason: str | None = Field(default=None, max_length=100)


class RazorpayPaymentContainer(_RazorpayModel):
    entity: RazorpayPaymentEntity


class RazorpayPayload(_RazorpayModel):
    payment: RazorpayPaymentContainer


class RazorpayPaymentWebhook(_RazorpayModel):
    entity: Literal["event"]
    account_id: str = Field(min_length=3, max_length=100, pattern=r"^acc_[A-Za-z0-9]+$")
    event: Literal["payment.captured", "payment.failed"]
    payload: RazorpayPayload
    created_at: int = Field(ge=0, le=253_402_300_799)


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    try:
        supplied_digest = bytes.fromhex(signature)
    except ValueError:
        return False
    if len(supplied_digest) != hashlib.sha256().digest_size:
        return False
    expected_digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    return hmac.compare_digest(expected_digest, supplied_digest)


def _nonblank(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _provided_or_missing(value: str | None) -> tuple[str | None, FieldAvailability]:
    normalized = _nonblank(value)
    if normalized is None:
        return None, FieldAvailability.MISSING
    return normalized, FieldAvailability.PROVIDED


def _method_dimensions(
    payment: RazorpayPaymentEntity,
) -> tuple[str | None, FieldAvailability, str | None, FieldAvailability]:
    if payment.method in {"card", "emi"}:
        card = payment.card
        issuer, issuer_availability = _provided_or_missing(card.issuer if card else None)
        provider, provider_availability = _provided_or_missing(card.network if card else None)
        return issuer, issuer_availability, provider, provider_availability
    if payment.method == "netbanking":
        issuer, issuer_availability = _provided_or_missing(payment.bank)
        return issuer, issuer_availability, None, FieldAvailability.NOT_APPLICABLE
    if payment.method == "wallet":
        provider, provider_availability = _provided_or_missing(payment.wallet)
        return None, FieldAvailability.NOT_APPLICABLE, provider, provider_availability
    if payment.method == "upi":
        return None, FieldAvailability.MISSING, None, FieldAvailability.REDACTED
    raise UnsupportedWebhook("Unsupported payment method")


def _cohort_key(
    payment: RazorpayPaymentEntity,
    issuer: str | None,
    issuer_availability: FieldAvailability,
    provider: str | None,
    provider_availability: FieldAvailability,
) -> str | None:
    if (
        issuer_availability != FieldAvailability.PROVIDED
        or provider_availability != FieldAvailability.PROVIDED
        or issuer is None
        or provider is None
    ):
        return None
    band = min(payment.amount // 100_000, 10)
    return f"{payment.method}:{issuer}:{provider}:band_{band}"


def normalize_webhook(raw_body: bytes, source_event_id: str) -> OperationalPaymentEvent:
    try:
        decoded: Any = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MalformedWebhookJson("Malformed webhook JSON") from None

    try:
        discriminator = RazorpayEventDiscriminator.model_validate(decoded)
    except ValidationError:
        raise MalformedSupportedWebhook("Malformed webhook envelope") from None
    if discriminator.event not in SUPPORTED_EVENT_TYPES:
        raise UnsupportedWebhook("Unsupported event type")

    try:
        webhook = RazorpayPaymentWebhook.model_validate(decoded)
    except ValidationError:
        raise MalformedSupportedWebhook("Malformed supported webhook") from None

    payment = webhook.payload.payment.entity
    if payment.method not in SUPPORTED_PAYMENT_METHODS:
        raise UnsupportedWebhook("Unsupported payment method")
    if payment.currency != "INR":
        raise UnsupportedWebhook("Unsupported payment currency")

    expected_nested_status = "captured" if webhook.event == "payment.captured" else "failed"
    if payment.status != expected_nested_status:
        raise MalformedSupportedWebhook("Webhook event and payment status contradict")

    issuer, issuer_availability, provider, provider_availability = _method_dimensions(payment)
    error_code = _nonblank(payment.error_code) if webhook.event == "payment.failed" else None
    error_code_availability = (
        FieldAvailability.PROVIDED
        if error_code is not None
        else FieldAvailability.MISSING
        if webhook.event == "payment.failed"
        else FieldAvailability.NOT_APPLICABLE
    )
    try:
        occurred_at = datetime.fromtimestamp(webhook.created_at, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise MalformedSupportedWebhook(
            "Webhook timestamp is outside the supported range"
        ) from None

    return OperationalPaymentEvent(
        schema_version=2,
        source=RAZORPAY_SOURCE,
        source_event_id=source_event_id,
        source_event_type=webhook.event,
        identity_scope=SourceIdentityScope.PROVIDER,
        payment_id=payment.id,
        merchant_id=webhook.account_id,
        method=payment.method,
        issuer=issuer,
        issuer_availability=issuer_availability,
        provider=provider,
        provider_availability=provider_availability,
        amount_paise=payment.amount,
        status=(
            PaymentStatus.SUCCESS.value
            if webhook.event == "payment.captured"
            else PaymentStatus.FAILED.value
        ),
        error_code=error_code,
        error_code_availability=error_code_availability,
        error_source=(
            _nonblank(payment.error_source) if webhook.event == "payment.failed" else None
        ),
        error_step=_nonblank(payment.error_step) if webhook.event == "payment.failed" else None,
        error_reason=(
            _nonblank(payment.error_reason) if webhook.event == "payment.failed" else None
        ),
        latency_ms=None,
        latency_availability=FieldAvailability.MISSING,
        cohort_key=_cohort_key(
            payment,
            issuer,
            issuer_availability,
            provider,
            provider_availability,
        ),
        occurred_at=occurred_at,
    )
