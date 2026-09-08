from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, Session, mapped_column

from payrecover.infrastructure.database.base import Base

AUDIT_SUBJECT_CHECK = """
(event_type IN ('incident.opened','incident.observation_updated','incident.resolved')
 AND incident_id IS NOT NULL AND payment_event_id IS NULL
 AND diagnosis_id IS NULL AND recovery_plan_id IS NULL) OR
(event_type = 'incident.diagnosed' AND incident_id IS NOT NULL AND payment_event_id IS NULL
 AND diagnosis_id IS NOT NULL AND recovery_plan_id IS NULL) OR
(event_type = 'recovery_plan.created' AND incident_id IS NOT NULL AND payment_event_id IS NULL
 AND diagnosis_id IS NOT NULL AND recovery_plan_id IS NOT NULL) OR
(event_type NOT IN ('incident.opened','incident.observation_updated','incident.resolved',
 'incident.diagnosed','recovery_plan.created') AND incident_id IS NULL
 AND diagnosis_id IS NULL AND recovery_plan_id IS NULL)
"""


class DiagnosisRow(Base):
    __tablename__ = "incident_diagnoses"
    __table_args__ = (
        UniqueConstraint(
            "merchant_id", "observation_id", "diagnosis_version", name="uq_diagnosis_version"
        ),
        UniqueConstraint(
            "id", "incident_id", "observation_id", "merchant_id", name="uq_diagnosis_subject"
        ),
        UniqueConstraint("id", "incident_id", name="uq_diagnosis_audit_subject"),
        ForeignKeyConstraint(
            ["observation_id", "incident_id", "merchant_id"],
            [
                "incident_observations.id",
                "incident_observations.incident_id",
                "incident_observations.merchant_id",
            ],
            ondelete="RESTRICT",
            name="fk_diagnosis_observation",
        ),
        CheckConstraint("diagnosis_version = 'diagnosis-v1'", name="version"),
        CheckConstraint("btrim(merchant_id) <> ''", name="merchant"),
        CheckConstraint(
            "outcome IN ('probable_transient_network_failure',"
            "'probable_issuer_unavailability','probable_customer_funding_issue',"
            "'probable_authentication_failure','unknown','insufficient_evidence')",
            name="outcome",
        ),
        CheckConstraint(
            "reason_code IN ('strict_majority','mixed_or_unmapped_failures',"
            "'insufficient_failures')",
            name="reason",
        ),
        CheckConstraint(
            "supporting_failed_count >= 0 AND total_failed_count >= supporting_failed_count",
            name="counts",
        ),
        CheckConstraint("jsonb_typeof(evidence) = 'object'", name="evidence_object"),
        CheckConstraint("evidence_sha256 ~ '^[0-9a-f]{64}$'", name="digest"),
        Index("ix_diagnosis_history", "merchant_id", "incident_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(100))
    incident_id: Mapped[int] = mapped_column(BigInteger)
    observation_id: Mapped[int] = mapped_column(BigInteger)
    diagnosis_version: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(64))
    reason_code: Mapped[str] = mapped_column(String(64))
    supporting_failed_count: Mapped[int] = mapped_column(BigInteger)
    total_failed_count: Mapped[int] = mapped_column(BigInteger)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    evidence_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PlanRow(Base):
    __tablename__ = "recovery_plans"
    __table_args__ = (
        UniqueConstraint("id", "merchant_id", "source", "payment_id",
                         name="uq_sim_plan_subject"),
        UniqueConstraint(
            "merchant_id", "source", "payment_id", "policy_version", name="uq_plan_payment_policy"
        ),
        UniqueConstraint("id", "diagnosis_id", "incident_id", name="uq_plan_audit_subject"),
        ForeignKeyConstraint(
            ["diagnosis_id", "incident_id", "observation_id", "merchant_id"],
            [
                "incident_diagnoses.id",
                "incident_diagnoses.incident_id",
                "incident_diagnoses.observation_id",
                "incident_diagnoses.merchant_id",
            ],
            ondelete="RESTRICT",
            name="fk_plan_diagnosis",
        ),
        ForeignKeyConstraint(
            ["payment_event_id", "merchant_id", "source", "payment_id"],
            [
                "payment_events.id",
                "payment_events.merchant_id",
                "payment_events.source",
                "payment_events.payment_id",
            ],
            ondelete="RESTRICT",
            name="fk_plan_payment",
        ),
        CheckConstraint("policy_version = 'recovery-planning-v1'", name="version"),
        CheckConstraint(
            "btrim(merchant_id) <> '' AND btrim(source) <> '' AND btrim(payment_id) <> ''",
            name="identity",
        ),
        CheckConstraint(
            "action IN ('retry_now','wait_and_retry','notify_customer','escalate','no_action')",
            name="action",
        ),
        CheckConstraint("decision IN ('blocked','manual_review','no_action')", name="decision"),
        CheckConstraint(
            "reason_code IN ('stored_success','contradictory_history','unknown_signal',"
            "'authentication_failure','customer_funding_action','transient_network',"
            "'issuer_health_wait')",
            name="reason",
        ),
        CheckConstraint(
            "max_attempts BETWEEN 0 AND 3 AND "
            "(retry_after_seconds IS NULL OR retry_after_seconds >= 0)",
            name="bounds",
        ),
        CheckConstraint(
            "(action IN ('retry_now','wait_and_retry') AND decision = 'blocked' "
            "AND max_attempts > 0 AND retry_after_seconds IS NOT NULL) OR "
            "(action NOT IN ('retry_now','wait_and_retry') AND max_attempts = 0 "
            "AND retry_after_seconds IS NULL)",
            name="terminal",
        ),
        CheckConstraint("execution_authorized = false", name="no_execution"),
        CheckConstraint(
            "jsonb_typeof(evidence) = 'object' AND jsonb_typeof(prerequisites) = 'object'",
            name="objects",
        ),
        CheckConstraint("evidence_sha256 ~ '^[0-9a-f]{64}$'", name="digest"),
        CheckConstraint("association_evaluated_at <= created_at", name="association_time"),
        Index("ix_plan_history", "merchant_id", "incident_id", "created_at", "id"),
        Index("ix_plan_diagnosis", "diagnosis_id"),
        Index("ix_plan_payment_event", "payment_event_id"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    merchant_id: Mapped[str] = mapped_column(String(100))
    incident_id: Mapped[int] = mapped_column(BigInteger)
    observation_id: Mapped[int] = mapped_column(BigInteger)
    diagnosis_id: Mapped[UUID]
    payment_event_id: Mapped[UUID]
    source: Mapped[str] = mapped_column(String(32))
    payment_id: Mapped[str] = mapped_column(String(100))
    policy_version: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    decision: Mapped[str] = mapped_column(String(32))
    reason_code: Mapped[str] = mapped_column(String(64))
    retry_after_seconds: Mapped[int | None] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(SmallInteger)
    execution_authorized: Mapped[bool] = mapped_column(Boolean)
    prerequisites: Mapped[dict[str, Any]] = mapped_column(JSONB)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB)
    evidence_sha256: Mapped[str] = mapped_column(String(64))
    association_evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@event.listens_for(Session, "before_flush")
def protect_planning_records(session: Session, flush_context: object, instances: object) -> None:
    if any(
        isinstance(row, (DiagnosisRow, PlanRow)) for row in session.dirty.union(session.deleted)
    ):
        raise ValueError("Planning records are append-only through the ORM")
