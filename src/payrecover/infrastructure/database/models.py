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
        CheckConstraint("latency_ms >= 0", name="latency_ms_nonnegative"),
        CheckConstraint("status IN ('success', 'failed')", name="status_allowed"),
        CheckConstraint(
            "status <> 'failed' OR NULLIF(BTRIM(error_code), '') IS NOT NULL",
            name="failed_event_has_error_code",
        ),
        CheckConstraint("CHAR_LENGTH(payload_sha256) = 64", name="payload_sha256_length"),
        CheckConstraint("CHAR_LENGTH(BTRIM(source)) > 0", name="source_nonblank"),
        CheckConstraint("CHAR_LENGTH(BTRIM(method)) > 0", name="method_nonblank"),
        CheckConstraint("CHAR_LENGTH(BTRIM(issuer)) > 0", name="issuer_nonblank"),
        CheckConstraint("CHAR_LENGTH(BTRIM(provider)) > 0", name="provider_nonblank"),
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
    issuer: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(100))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(100))
    latency_ms: Mapped[int] = mapped_column(Integer)
    cohort_key: Mapped[str] = mapped_column(String(255))
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
