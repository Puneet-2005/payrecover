from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from payrecover.infrastructure.database.base import Base


class AuditRecordMutationError(RuntimeError):
    pass


class PaymentEventRow(Base):
    __tablename__ = "payment_events"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "merchant_id",
            "source_event_id",
            name="uq_payment_events_source_merchant_event",
        ),
        CheckConstraint("schema_version > 0", name="schema_version_positive"),
        CheckConstraint("amount_paise > 0", name="amount_paise_positive"),
        CheckConstraint("status IN ('success', 'failed')", name="status_allowed"),
        CheckConstraint(
            "issuer_availability IN "
            "('provided', 'missing', 'not_applicable', 'redacted')",
            name="issuer_availability_allowed",
        ),
        CheckConstraint(
            "(issuer_availability = 'provided' AND "
            "issuer IS NOT NULL AND CHAR_LENGTH(BTRIM(issuer)) > 0) OR "
            "(issuer_availability <> 'provided' AND issuer IS NULL)",
            name="issuer_matches_availability",
        ),
        CheckConstraint(
            "provider_availability IN "
            "('provided', 'missing', 'not_applicable', 'redacted')",
            name="provider_availability_allowed",
        ),
        CheckConstraint(
            "(provider_availability = 'provided' AND "
            "provider IS NOT NULL AND CHAR_LENGTH(BTRIM(provider)) > 0) OR "
            "(provider_availability <> 'provided' AND provider IS NULL)",
            name="provider_matches_availability",
        ),
        CheckConstraint(
            "latency_availability IN "
            "('provided', 'missing', 'not_applicable', 'redacted')",
            name="latency_availability_allowed",
        ),
        CheckConstraint(
            "(latency_availability = 'provided' AND "
            "latency_ms IS NOT NULL AND latency_ms >= 0) OR "
            "(latency_availability <> 'provided' AND latency_ms IS NULL)",
            name="latency_matches_availability",
        ),
        CheckConstraint(
            "error_code_availability IN "
            "('provided', 'missing', 'not_applicable', 'redacted')",
            name="error_code_availability_allowed",
        ),
        CheckConstraint(
            "(error_code_availability = 'provided' AND error_code IS NOT NULL "
            "AND CHAR_LENGTH(BTRIM(error_code)) > 0) OR "
            "(error_code_availability <> 'provided' AND error_code IS NULL)",
            name="error_code_matches_availability",
        ),
        CheckConstraint(
            "(status = 'failed' AND error_code_availability IN ('provided', 'missing')) OR "
            "(status = 'success' AND "
            "error_code_availability IN ('provided', 'not_applicable'))",
            name="status_matches_error_code_availability",
        ),
        CheckConstraint("CHAR_LENGTH(payload_sha256) = 64", name="payload_sha256_length"),
        CheckConstraint("CHAR_LENGTH(BTRIM(source)) > 0", name="source_nonblank"),
        CheckConstraint("CHAR_LENGTH(BTRIM(method)) > 0", name="method_nonblank"),
        CheckConstraint(
            "error_source IS NULL OR CHAR_LENGTH(BTRIM(error_source)) > 0",
            name="error_source_nonblank",
        ),
        CheckConstraint(
            "error_step IS NULL OR CHAR_LENGTH(BTRIM(error_step)) > 0",
            name="error_step_nonblank",
        ),
        CheckConstraint(
            "error_reason IS NULL OR CHAR_LENGTH(BTRIM(error_reason)) > 0",
            name="error_reason_nonblank",
        ),
        Index(
            "ix_payment_events_cohort_time",
            "merchant_id",
            "cohort_key",
            "occurred_at",
        ),
        Index(
            "ix_payment_events_payment_time",
            "merchant_id",
            "payment_id",
            "occurred_at",
        ),
        Index(
            "ix_payment_events_method_time",
            "merchant_id",
            "method",
            "occurred_at",
        ),
        Index(
            "uq_payment_events_razorpay_event_id",
            "source_event_id",
            unique=True,
            postgresql_where=text("source = 'razorpay_webhook'"),
        ),
        Index(
            "ix_payment_events_failed_error_time",
            "merchant_id",
            "error_code",
            "occurred_at",
            postgresql_where=text("status = 'failed'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    schema_version: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    source: Mapped[str] = mapped_column(String(32))
    source_event_id: Mapped[str] = mapped_column(String(128))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    merchant_id: Mapped[str] = mapped_column(String(100))
    payment_id: Mapped[str] = mapped_column(String(100))
    method: Mapped[str] = mapped_column(String(32))
    issuer: Mapped[str | None] = mapped_column(String(100))
    issuer_availability: Mapped[str] = mapped_column(String(20))
    provider: Mapped[str | None] = mapped_column(String(100))
    provider_availability: Mapped[str] = mapped_column(String(20))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_code_availability: Mapped[str] = mapped_column(String(20))
    error_source: Mapped[str | None] = mapped_column(String(64))
    error_step: Mapped[str | None] = mapped_column(String(100))
    error_reason: Mapped[str | None] = mapped_column(String(100))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    latency_availability: Mapped[str] = mapped_column(String(20))
    cohort_key: Mapped[str | None] = mapped_column(String(255))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


class AuditRecordRow(Base):
    __tablename__ = "audit_records"
    __table_args__ = (
        CheckConstraint("CHAR_LENGTH(BTRIM(event_type)) > 0", name="event_type_nonblank"),
        CheckConstraint("CHAR_LENGTH(BTRIM(actor_type)) > 0", name="actor_type_nonblank"),
        CheckConstraint(
            "actor_ref_digest IS NULL OR CHAR_LENGTH(actor_ref_digest) = 64",
            name="actor_ref_digest_length",
        ),
        Index("ix_audit_records_correlation", "correlation_id", "id"),
        Index(
            "ix_audit_records_payment_event",
            "payment_event_id",
            "recorded_at",
            "id",
            postgresql_where=text("payment_event_id IS NOT NULL"),
        ),
        Index("ix_audit_records_recorded", "recorded_at", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    correlation_id: Mapped[UUID]
    payment_event_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("payment_events.id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(64))
    actor_type: Mapped[str] = mapped_column(String(32))
    actor_ref_digest: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )


@event.listens_for(Session, "before_flush")
def prevent_audit_record_changes(
    session: Session, flush_context: object, instances: object
) -> None:
    del flush_context, instances
    changed_audit_records = {
        record
        for record in session.dirty.union(session.deleted)
        if isinstance(record, AuditRecordRow) and record not in session.new
    }
    if changed_audit_records:
        raise AuditRecordMutationError("Audit records are append-only through the ORM")
